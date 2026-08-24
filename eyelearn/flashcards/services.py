import logging
import math
import unicodedata
from datetime import timedelta

import fsrs
from billing.models import get_active_subscription
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import storage
from .models import Collection, CollectionGoal, Flashcard, FlashcardMedia, ReviewLog, ReviewState, StudyDay

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

    def _ensure_review_states(self, *, user, flashcard_ids, now):
        """Lazily backfills ReviewState rows (due=now) for any flashcard the
        user has never reviewed, so newly-added cards count as due immediately.
        """
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

    def build_study_queue(self, *, user, collection, limit=None, now=None):
        now = now or timezone.now()
        subtree_ids = CollectionService().get_subtree_ids(collection=collection)
        flashcard_ids = list(Flashcard.objects.filter(collection_id__in=subtree_ids).values_list('id', flat=True))
        self._ensure_review_states(user=user, flashcard_ids=flashcard_ids, now=now)

        queryset = (
            ReviewState.objects
            .filter(user=user, flashcard_id__in=flashcard_ids, due__lte=now)
            .select_related('flashcard')
            .order_by('due')
        )
        if limit:
            queryset = queryset[:limit]
        return list(queryset)

    def count_due(self, *, user, collection, now=None):
        """Due-card count across `collection`'s subtree, without materializing
        full ReviewState/Flashcard rows -- used for the collection list's "due
        now" badge, where only the number is needed.
        """
        now = now or timezone.now()
        subtree_ids = CollectionService().get_subtree_ids(collection=collection)
        flashcard_ids = list(Flashcard.objects.filter(collection_id__in=subtree_ids).values_list('id', flat=True))
        self._ensure_review_states(user=user, flashcard_ids=flashcard_ids, now=now)
        return ReviewState.objects.filter(user=user, flashcard_id__in=flashcard_ids, due__lte=now).count()

    def build_multi_collection_queue(self, *, user, collection_ids, now=None, limit=None):
        """Union of due cards across several user-owned collections, deduped by
        flashcard id (a card due under two selected collections is studied once).
        """
        now = now or timezone.now()
        collections = Collection.objects.filter(user=user, id__in=collection_ids)

        merged = {}
        for collection in collections:
            for review_state in self.build_study_queue(user=user, collection=collection, now=now):
                merged.setdefault(review_state.flashcard_id, review_state)

        items = sorted(merged.values(), key=lambda review_state: review_state.due)
        return items[:limit] if limit else items

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

            study_day, day_created = StudyDay.objects.get_or_create(
                user=user, date=now.date(), defaults={'cards_reviewed': 1},
            )
            if not day_created:
                StudyDay.objects.filter(pk=study_day.pk).update(cards_reviewed=F('cards_reviewed') + 1)

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


class GoalService:
    """Per-collection daily study targets, paced from a CollectionGoal.target_date.

    A flashcard counts as "mastered" once its ReviewState reaches the REVIEW
    state (graduated out of learning/relearning) -- interval length isn't
    considered, so the pace stays achievable.
    """

    def get_goal_progress(self, *, user, collection, goal, today=None):
        today = today or timezone.now().date()
        subtree_ids = CollectionService().get_subtree_ids(collection=collection)

        total = Flashcard.objects.filter(collection_id__in=subtree_ids).count()
        mastered = ReviewState.objects.filter(
            user=user, flashcard__collection_id__in=subtree_ids, state=ReviewState.State.REVIEW,
        ).count()
        remaining = max(total - mastered, 0)

        days_until = (goal.target_date - today).days
        # max(days_until, 1) folds "due today" (0) and "overdue" (<0) into the
        # same "study everything remaining now" result -- no separate branch
        # needed for the number itself; `overdue` below is purely a UI flag.
        paced_target = 0 if remaining == 0 else math.ceil(remaining / max(days_until, 1))

        # Subtract cards already reviewed today so the quota counts down as
        # the user makes progress, instead of re-demanding the same total
        # every time the page reloads (e.g. after navigating away mid-session).
        reviewed_today = ReviewLog.objects.filter(
            review_state__user=user,
            review_state__flashcard__collection_id__in=subtree_ids,
            reviewed_at__date=today,
        ).values('review_state__flashcard_id').distinct().count()
        today_target = max(paced_target - reviewed_today, 0)

        return {
            'collection': collection.id,
            'target_date': goal.target_date,
            'total': total,
            'mastered': mastered,
            'remaining': remaining,
            'today_target': today_target,
            'reviewed_today': reviewed_today,
            'overdue': days_until < 0,
            'days_until': days_until,
        }

    def build_daily_study_queue(self, *, user, now=None, limit=None):
        now = now or timezone.now()
        today = now.date()

        merged = {}
        goals = CollectionGoal.objects.filter(collection__user=user).select_related('collection')
        for goal in goals:
            progress = self.get_goal_progress(user=user, collection=goal.collection, goal=goal, today=today)
            if progress['today_target'] <= 0:
                continue
            queue = ReviewService().build_study_queue(user=user, collection=goal.collection, now=now)
            for review_state in queue[:progress['today_target']]:
                merged.setdefault(review_state.flashcard_id, review_state)

        items = sorted(merged.values(), key=lambda review_state: review_state.due)
        return items[:limit] if limit else items


class StreakService:
    """Consecutive-day study streak, derived from the StudyDay table."""

    STREAK_LOOKBACK_DAYS = 1000

    def get_cards_studied_today(self, *, user, today=None):
        today = today or timezone.now().date()
        count = StudyDay.objects.filter(user=user, date=today).values_list('cards_reviewed', flat=True).first()
        return count or 0

    def get_current_streak(self, *, user, today=None):
        today = today or timezone.now().date()
        study_dates = set(
            StudyDay.objects
            .filter(user=user, date__lte=today)
            .order_by('-date')
            .values_list('date', flat=True)[:self.STREAK_LOOKBACK_DAYS]
        )

        cursor = today if today in study_dates else today - timedelta(days=1)
        streak = 0
        while cursor in study_dates:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    def get_calendar(self, *, user, start_date, end_date):
        rows = dict(
            StudyDay.objects
            .filter(user=user, date__range=(start_date, end_date))
            .values_list('date', 'cards_reviewed')
        )

        days = []
        cursor = start_date
        while cursor <= end_date:
            days.append({
                'date': cursor,
                'studied': cursor in rows,
                'cards_reviewed': rows.get(cursor, 0),
            })
            cursor += timedelta(days=1)
        return days
