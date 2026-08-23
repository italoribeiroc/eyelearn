import logging
import unicodedata

import fsrs
from billing.models import get_active_subscription
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import storage
from .models import Collection, Flashcard, FlashcardMedia, ReviewLog, ReviewState

logger = logging.getLogger(__name__)

FREE_COLLECTION_LIMIT = 3


class CollectionCycleError(Exception):
    """Raised when reparenting a collection would create a cycle in the tree."""


class CrossOwnerParentError(Exception):
    """Raised when a collection's parent would belong to a different user."""


class CollectionLimitError(Exception):
    """Raised when a free-plan user tries to exceed FREE_COLLECTION_LIMIT collections."""


class UnsupportedMediaError(Exception):
    """Raised when a requested content type/size isn't allowed for its media type."""


ALLOWED_CONTENT_TYPES = {
    FlashcardMedia.MediaType.IMAGE: {'image/png', 'image/jpeg', 'image/webp', 'image/gif'},
    FlashcardMedia.MediaType.AUDIO: {'audio/mpeg', 'audio/mp4', 'audio/ogg', 'audio/wav'},
    FlashcardMedia.MediaType.VIDEO: {'video/mp4', 'video/webm', 'video/quicktime'},
}

MAX_SIZE_BYTES = {
    FlashcardMedia.MediaType.IMAGE: 10 * 1024 * 1024,
    FlashcardMedia.MediaType.AUDIO: 25 * 1024 * 1024,
    FlashcardMedia.MediaType.VIDEO: 200 * 1024 * 1024,
}


def normalize_answer(text):
    text = unicodedata.normalize('NFKC', text or '')
    return ' '.join(text.strip().lower().split())


def check_typed_answer(flashcard, submitted):
    candidates = [flashcard.answer, *flashcard.accepted_answers]
    submitted_normalized = normalize_answer(submitted)
    return any(normalize_answer(candidate) == submitted_normalized for candidate in candidates if candidate)


class CollectionService:
    def create_collection(self, *, user, name, description='', parent=None):
        if parent is not None:
            self._assert_valid_parent(user=user, parent=parent)
        if get_active_subscription(user) is None:
            existing_count = Collection.objects.filter(user=user).count()
            if existing_count >= FREE_COLLECTION_LIMIT:
                raise CollectionLimitError(
                    f'Free plan is limited to {FREE_COLLECTION_LIMIT} collections. Upgrade to Pro for unlimited collections.',
                )
        return Collection.objects.create(user=user, name=name, description=description, parent=parent)

    def update_collection(self, *, collection, **fields):
        if 'parent' in fields and fields['parent'] is not None:
            self._assert_valid_parent(user=collection.user, parent=fields['parent'], collection=collection)
        for field, value in fields.items():
            setattr(collection, field, value)
        collection.save()
        return collection

    def delete_collection(self, *, collection):
        subtree_ids = self.get_subtree_ids(collection=collection)
        storage_keys = list(
            FlashcardMedia.objects
            .filter(flashcard__collection_id__in=subtree_ids)
            .values_list('storage_key', flat=True)
        )
        with transaction.atomic():
            collection.delete()
            if storage_keys:
                transaction.on_commit(lambda: _cleanup_storage_keys(storage_keys))

    def get_subtree_ids(self, *, collection):
        """Self + all descendant collection ids, via in-memory adjacency-list walk.

        Works identically on SQLite (CI) and Postgres (prod), no recursive-CTE
        dependency. Fine at expected per-user collection tree sizes.
        """
        rows = Collection.objects.filter(user=collection.user).values('id', 'parent_id')
        children_map = {}
        for row in rows:
            children_map.setdefault(row['parent_id'], []).append(row['id'])

        subtree = {collection.id}
        queue = [collection.id]
        while queue:
            current = queue.pop()
            for child_id in children_map.get(current, []):
                if child_id not in subtree:
                    subtree.add(child_id)
                    queue.append(child_id)
        return subtree

    def _assert_valid_parent(self, *, user, parent, collection=None):
        if parent.user_id != user.id:
            raise CrossOwnerParentError('Parent collection belongs to a different user.')
        if collection is not None:
            if parent.id == collection.id:
                raise CollectionCycleError('A collection cannot be its own parent.')
            if parent.id in self.get_subtree_ids(collection=collection):
                raise CollectionCycleError('Cannot move a collection under its own descendant.')


def _cleanup_storage_keys(storage_keys):
    for key in storage_keys:
        try:
            storage.delete_object(key=key)
        except Exception:
            logger.exception('Failed to delete storage object %s', key)


class MediaService:
    def create_upload_url(self, *, user, flashcard, media_type, side, content_type, filename, size_bytes):
        self._validate(media_type=media_type, content_type=content_type, size_bytes=size_bytes)
        key = storage.build_storage_key(
            user_id=user.id, flashcard_id=flashcard.id, media_type=media_type, filename=filename,
        )
        upload_url = storage.generate_upload_url(key=key, content_type=content_type)
        return key, upload_url

    def confirm_upload(self, *, flashcard, storage_key, media_type, side, content_type, size_bytes):
        self._validate(media_type=media_type, content_type=content_type, size_bytes=size_bytes)
        return FlashcardMedia.objects.create(
            flashcard=flashcard,
            media_type=media_type,
            side=side,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=size_bytes,
        )

    def delete_media(self, *, media):
        storage_key = media.storage_key
        with transaction.atomic():
            media.delete()
            transaction.on_commit(lambda: _cleanup_storage_keys([storage_key]))

    def _validate(self, *, media_type, content_type, size_bytes):
        if content_type not in ALLOWED_CONTENT_TYPES.get(media_type, set()):
            raise UnsupportedMediaError(f'{content_type!r} is not allowed for media type {media_type!r}.')
        max_size = MAX_SIZE_BYTES.get(media_type)
        if max_size and size_bytes > max_size:
            raise UnsupportedMediaError(f'File exceeds the {max_size} byte limit for media type {media_type!r}.')


_FSRS_STATE_TO_OURS = {
    fsrs.State.Learning: ReviewState.State.LEARNING,
    fsrs.State.Review: ReviewState.State.REVIEW,
    fsrs.State.Relearning: ReviewState.State.RELEARNING,
}
_OURS_TO_FSRS_STATE = {v: k for k, v in _FSRS_STATE_TO_OURS.items()}


class ReviewService:
    """Wraps fsrs.Scheduler. All fsrs.* translation is private to this class, so a
    future version bump of the `fsrs` package only touches this file.
    """

    def get_or_create_state(self, *, user, flashcard):
        review_state, _ = ReviewState.objects.get_or_create(
            user=user, flashcard=flashcard, defaults={'due': timezone.now()},
        )
        return review_state

    def build_study_queue(self, *, user, collection, limit=None, now=None):
        now = now or timezone.now()
        subtree_ids = CollectionService().get_subtree_ids(collection=collection)
        flashcard_ids = list(Flashcard.objects.filter(collection_id__in=subtree_ids).values_list('id', flat=True))

        existing_ids = set(
            ReviewState.objects
            .filter(user=user, flashcard_id__in=flashcard_ids)
            .values_list('flashcard_id', flat=True)
        )
        missing_ids = [fid for fid in flashcard_ids if fid not in existing_ids]
        if missing_ids:
            ReviewState.objects.bulk_create(
                [ReviewState(user=user, flashcard_id=fid, due=now) for fid in missing_ids],
                ignore_conflicts=True,
            )

        queryset = (
            ReviewState.objects
            .filter(user=user, flashcard_id__in=flashcard_ids, due__lte=now)
            .select_related('flashcard')
            .order_by('due')
        )
        if limit:
            queryset = queryset[:limit]
        return list(queryset)

    def submit_review(self, *, user, flashcard, rating=None, selected_option=None, submitted_answer=None,
                       reviewed_at=None):
        correct = None
        if flashcard.card_type == Flashcard.CardType.MULTIPLE_CHOICE:
            correct = self._check_multiple_choice(flashcard, selected_option)
            rating = ReviewLog.Rating.GOOD if correct else ReviewLog.Rating.AGAIN
        elif flashcard.card_type == Flashcard.CardType.TYPED_ANSWER:
            correct = check_typed_answer(flashcard, submitted_answer)
            rating = ReviewLog.Rating.GOOD if correct else ReviewLog.Rating.AGAIN
        # BASIC: rating is the caller's own self-assessment, used as-is.

        now = reviewed_at or timezone.now()

        with transaction.atomic():
            review_state, created = ReviewState.objects.get_or_create(
                user=user, flashcard=flashcard, defaults={'due': now},
            )
            if not created:
                review_state = ReviewState.objects.select_for_update().get(pk=review_state.pk)

            previous_state = review_state.state
            previous_last_review = review_state.last_review

            card_before = self._to_fsrs_card(review_state)
            state_before_snapshot = card_before.to_dict()

            rating_enum = fsrs.Rating(rating)
            card_after, _log = self._scheduler().review_card(card_before, rating_enum, review_datetime=now)

            self._apply_card_to_state(review_state, card_after)
            review_state.elapsed_days = max((now - previous_last_review).days, 0) if previous_last_review else 0
            review_state.scheduled_days = max((card_after.due - now).days, 0)
            if previous_state == ReviewState.State.REVIEW and rating_enum == fsrs.Rating.Again:
                review_state.lapses += 1
            review_state.save()

            ReviewLog.objects.create(
                review_state=review_state,
                rating=rating,
                state_before=state_before_snapshot,
                state_after=card_after.to_dict(),
            )

        return review_state, correct

    def _check_multiple_choice(self, flashcard, selected_option):
        options = flashcard.options or []
        if selected_option is None or not (0 <= selected_option < len(options)):
            return False
        return bool(options[selected_option].get('is_correct'))

    def _scheduler(self):
        return fsrs.Scheduler(
            desired_retention=settings.FSRS_DESIRED_RETENTION,
            enable_fuzzing=settings.FSRS_ENABLE_FUZZING,
        )

    def _to_fsrs_card(self, review_state):
        if review_state.reps == 0:
            return fsrs.Card()
        return fsrs.Card(
            card_id=review_state.id,
            state=_OURS_TO_FSRS_STATE[review_state.state],
            step=review_state.step,
            stability=review_state.stability,
            difficulty=review_state.difficulty,
            due=review_state.due,
            last_review=review_state.last_review,
        )

    def _apply_card_to_state(self, review_state, card):
        review_state.step = card.step
        review_state.stability = card.stability
        review_state.difficulty = card.difficulty
        review_state.due = card.due
        review_state.last_review = card.last_review
        review_state.state = _FSRS_STATE_TO_OURS[card.state]
        review_state.reps += 1
        return review_state
