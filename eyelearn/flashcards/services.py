import logging
import math
import unicodedata
from datetime import timedelta

import fsrs
from billing.models import get_active_subscription
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from . import ai_generation, storage
from .models import (
    Collection,
    CollectionGoal,
    Flashcard,
    FlashcardGenerationDraft,
    FlashcardMedia,
    ReviewLog,
    ReviewState,
    StudyDay,
)

logger = logging.getLogger(__name__)

FREE_COLLECTION_LIMIT = 3
FREE_FLASHCARD_LIMIT = 300

MAX_AI_GENERATE_COUNT = 500
MAX_AI_REGENERATE_COUNT = 10
MAX_LEARNING_REQUEST_LENGTH = 2000
MAX_REGENERATE_INSTRUCTION_LENGTH = 1000
MAX_EXISTING_CARDS_CONTEXT = 150
# Cards requested per Claude/Gemini call. A single call can't safely produce
# hundreds of cards' worth of output in one Vercel serverless request (see
# AiFlashcardGenerationService.generate_next_batch), so anything above this
# is filled by repeated, individually-bounded continuation calls instead of
# one huge one.
AI_GENERATION_BATCH_SIZE = 25


class CollectionCycleError(Exception):
    """Raised when reparenting a collection would create a cycle in the tree."""


class CrossOwnerParentError(Exception):
    """Raised when a collection's parent would belong to a different user."""


class CollectionLimitError(Exception):
    """Raised when a free-plan user tries to exceed FREE_COLLECTION_LIMIT collections."""


class FlashcardLimitError(Exception):
    """Raised when a free-plan user tries to exceed FREE_FLASHCARD_LIMIT flashcards."""


class UnsupportedMediaError(Exception):
    """Raised when a requested content type/size isn't allowed for its media type."""


class AiGenerationNotAllowedError(Exception):
    """Raised when a non-Pro user calls generate/regenerate."""


class AiGenerationValidationError(Exception):
    """Raised for out-of-range count/length, or an invalid selected_ids/card_ids list."""


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


class FlashcardService:
    def assert_can_create(self, *, user):
        if get_active_subscription(user) is None:
            existing_count = Flashcard.objects.filter(collection__user=user).count()
            if existing_count >= FREE_FLASHCARD_LIMIT:
                raise FlashcardLimitError(
                    f'Free plan is limited to {FREE_FLASHCARD_LIMIT} flashcards. Upgrade to Pro for unlimited flashcards.',
                )


class AiFlashcardGenerationService:
    """AI flashcard generation is Pro-only, enforced here (not just hidden in
    the UI) the same way FlashcardService.assert_can_create gates the free
    flashcard limit -- a plain inline check, no dedicated permission class.
    """

    def assert_pro(self, *, user):
        if get_active_subscription(user) is None:
            raise AiGenerationNotAllowedError('AI flashcard generation is a Pro feature.')

    def generate(self, *, user, collection, card_type, learning_request, count=None, auto=False):
        self.assert_pro(user=user)
        if not auto:
            if not count or not (1 <= count <= MAX_AI_GENERATE_COUNT):
                raise AiGenerationValidationError(f'count must be between 1 and {MAX_AI_GENERATE_COUNT}.')
        learning_request = (learning_request or '').strip()
        if not learning_request:
            raise AiGenerationValidationError('learning_request is required.')
        if len(learning_request) > MAX_LEARNING_REQUEST_LENGTH:
            raise AiGenerationValidationError(
                f'learning_request must be at most {MAX_LEARNING_REQUEST_LENGTH} characters.',
            )

        # Clean up any abandoned draft before starting a new one, so drafts
        # don't pile up -- same "reclaim stale state" idea as
        # accounts.services.EmailVerificationService.reclaim_stale_signup.
        FlashcardGenerationDraft.objects.filter(
            user=user, collection=collection, status=FlashcardGenerationDraft.Status.PENDING,
        ).delete()

        existing_prompts = self._existing_card_prompts(collection)
        learning_context = self._build_learning_context(user=user, collection=collection)
        collection_context = {'name': collection.name, 'description': collection.description}

        if auto:
            # The model decides the total; we only bound its first batch,
            # not the total itself (that's clamped after the fact below).
            result = ai_generation.generate_auto_cards(
                card_type=card_type,
                max_first_batch=AI_GENERATION_BATCH_SIZE,
                learning_request=learning_request,
                collection_context=collection_context,
                existing_card_prompts=existing_prompts,
                learning_context=learning_context,
            )
            cards = result.cards
            # Never let the target be smaller than what was already
            # generated, and never let a model-chosen total exceed our own
            # hard ceiling.
            target_count = max(len(cards), min(result.recommended_total, MAX_AI_GENERATE_COUNT))
        else:
            first_batch_count = min(count, AI_GENERATION_BATCH_SIZE)
            cards = ai_generation.generate_cards(
                card_type=card_type,
                count=first_batch_count,
                learning_request=learning_request,
                collection_context=collection_context,
                existing_card_prompts=existing_prompts,
                learning_context=learning_context,
            )
            target_count = count

        draft = FlashcardGenerationDraft.objects.create(
            user=user,
            collection=collection,
            card_type=card_type,
            learning_request=learning_request,
            target_count=target_count,
            cards=[self._draft_dict(index + 1, card_type, card) for index, card in enumerate(cards)],
        )
        return draft

    def generate_next_batch(self, *, user, draft):
        """Continues a generation that's larger than one Claude/Gemini call
        can safely produce -- appends up to AI_GENERATION_BATCH_SIZE more
        cards toward draft.target_count. Called repeatedly by the frontend
        (not looped server-side) so each individual request stays short
        regardless of how large the overall target is -- this app has no
        background-job infrastructure to hand a long-running loop off to.

        Runs the slow AI call with NO row lock held, so a concurrent
        remove/regenerate/confirm/discard on the same draft (the user
        interacting with cards already on the review page while this batch
        is still generating in the background) is never blocked waiting on
        it. Only the final splice-and-save is a short locked transaction.
        """
        self.assert_pro(user=user)
        self._assert_pending(draft)

        remaining = draft.target_count - len(draft.cards)
        if remaining <= 0:
            raise AiGenerationValidationError('This generation is already complete.')
        batch_count = min(remaining, AI_GENERATION_BATCH_SIZE)

        # Dedup context includes both this collection's persisted cards and
        # everything generated in this draft so far, so later batches don't
        # repeat earlier ones. A snapshot from just before the (slow) call is
        # fine here -- staleness only means possibly-imperfect dedup context,
        # never a lost write (the save below is what's locked).
        existing_prompts = self._existing_card_prompts(draft.collection) + [
            card['prompt'] for card in draft.cards
        ]
        learning_context = self._build_learning_context(user=user, collection=draft.collection)

        cards = ai_generation.generate_cards(
            card_type=draft.card_type,
            count=batch_count,
            learning_request=draft.learning_request,
            collection_context={'name': draft.collection.name, 'description': draft.collection.description},
            existing_card_prompts=existing_prompts,
            learning_context=learning_context,
            already_generated=len(draft.cards),
            target_count=draft.target_count,
        )

        with transaction.atomic():
            fresh = FlashcardGenerationDraft.objects.select_for_update().get(pk=draft.pk)
            if fresh.status != FlashcardGenerationDraft.Status.PENDING:
                # Confirmed or discarded while this batch was generating --
                # drop the results rather than append to a draft that's no
                # longer live.
                return fresh
            next_id = (max((card['id'] for card in fresh.cards), default=0)) + 1
            new_dicts = [self._draft_dict(next_id + i, fresh.card_type, card) for i, card in enumerate(cards)]
            fresh.cards = fresh.cards + new_dicts
            fresh.save(update_fields=['cards', 'updated_at'])
            return fresh

    def regenerate(self, *, user, draft, selected_ids, instruction):
        """Same "slow call unlocked, final write locked" shape as
        generate_next_batch -- see its docstring."""
        self.assert_pro(user=user)
        self._assert_pending(draft)

        if not selected_ids:
            raise AiGenerationValidationError('Select at least one card to regenerate.')
        if len(selected_ids) > MAX_AI_REGENERATE_COUNT:
            raise AiGenerationValidationError(f'Select at most {MAX_AI_REGENERATE_COUNT} cards at once.')
        instruction = (instruction or '').strip()
        if not instruction:
            raise AiGenerationValidationError('instruction is required.')
        if len(instruction) > MAX_REGENERATE_INSTRUCTION_LENGTH:
            raise AiGenerationValidationError(
                f'instruction must be at most {MAX_REGENERATE_INSTRUCTION_LENGTH} characters.',
            )

        by_id = {card['id']: card for card in draft.cards}
        if not set(selected_ids).issubset(by_id.keys()):
            raise AiGenerationValidationError('selected_ids must refer to cards in this draft.')

        cards_to_replace = [by_id[card_id] for card_id in selected_ids]
        existing_prompts = self._existing_card_prompts(draft.collection)

        new_cards = ai_generation.regenerate_cards(
            card_type=draft.card_type,
            learning_request=draft.learning_request,
            current_draft_cards=draft.cards,
            cards_to_replace=cards_to_replace,
            existing_card_prompts=existing_prompts,
            instruction=instruction,
        )

        with transaction.atomic():
            fresh = FlashcardGenerationDraft.objects.select_for_update().get(pk=draft.pk)
            if fresh.status != FlashcardGenerationDraft.Status.PENDING:
                return fresh
            fresh_by_id = {card['id']: card for card in fresh.cards}
            # Splice replacements onto exactly the requested ids, in Python
            # -- this (not the model) is what guarantees every other card is
            # left completely untouched. A selected id that's since been
            # removed (a concurrent remove-cards call) is simply skipped --
            # nothing left to replace.
            for card_id, new_card in zip(selected_ids, new_cards):
                if card_id in fresh_by_id:
                    fresh_by_id[card_id] = self._draft_dict(card_id, fresh.card_type, new_card)
            fresh.cards = list(fresh_by_id.values())
            fresh.save(update_fields=['cards', 'updated_at'])
            return fresh

    def remove_cards(self, *, user, draft, card_ids):
        self._assert_pending(draft)
        if not card_ids:
            raise AiGenerationValidationError('card_ids is required.')
        draft.cards = [card for card in draft.cards if card['id'] not in card_ids]
        draft.save(update_fields=['cards', 'updated_at'])
        return draft

    def confirm(self, *, user, draft):
        self._assert_pending(draft)
        if not draft.cards:
            raise AiGenerationValidationError('No cards left to save.')

        # Deferred import: serializers.py imports ReviewService from this
        # module, so a top-level import here would be circular.
        from .serializers import FlashcardSerializer

        created = []
        errors = []
        for card in draft.cards:
            payload = {
                'card_type': draft.card_type,
                'prompt': card['prompt'],
                'answer': card['answer'],
                'options': card['options'],
                'accepted_answers': card['accepted_answers'],
            }
            serializer = FlashcardSerializer(data=payload)
            if not serializer.is_valid():
                errors.append({'id': card['id'], 'errors': serializer.errors})
                continue
            try:
                # Defensive: Pro always passes this, but a subscription could
                # in principle lapse between generating and confirming.
                FlashcardService().assert_can_create(user=user)
            except FlashcardLimitError as exc:
                errors.append({'id': card['id'], 'errors': {'detail': str(exc)}})
                continue
            created.append(serializer.save(collection=draft.collection))

        draft.status = FlashcardGenerationDraft.Status.CONFIRMED
        draft.save(update_fields=['status', 'updated_at'])
        return created, errors

    def discard(self, *, user, draft):
        if draft.status == FlashcardGenerationDraft.Status.CONFIRMED:
            raise AiGenerationValidationError('This generation was already saved.')
        draft.status = FlashcardGenerationDraft.Status.DISCARDED
        draft.save(update_fields=['status', 'updated_at'])
        return draft

    def _assert_pending(self, draft):
        if draft.status != FlashcardGenerationDraft.Status.PENDING:
            raise AiGenerationValidationError('This generation has already been saved or discarded.')

    def _draft_dict(self, card_id, card_type, card):
        if card_type == Flashcard.CardType.MULTIPLE_CHOICE:
            options = [
                {'text': option, 'is_correct': index == card.correct_option_index}
                for index, option in enumerate(card.options)
            ]
            return {'id': card_id, 'prompt': card.prompt, 'answer': '', 'options': options, 'accepted_answers': []}
        if card_type == Flashcard.CardType.TYPED_ANSWER:
            return {
                'id': card_id, 'prompt': card.prompt, 'answer': card.answer,
                'options': [], 'accepted_answers': card.accepted_answers,
            }
        return {'id': card_id, 'prompt': card.prompt, 'answer': card.answer, 'options': [], 'accepted_answers': []}

    def _existing_card_prompts(self, collection):
        return list(
            Flashcard.objects.filter(collection=collection)
            .order_by('-created_at')
            .values_list('prompt', flat=True)[:MAX_EXISTING_CARDS_CONTEXT]
        )

    def _build_learning_context(self, *, user, collection):
        subtree_ids = CollectionService().get_subtree_ids(collection=collection)
        total_cards = Flashcard.objects.filter(collection_id__in=subtree_ids).count()
        if total_cards == 0:
            return 'This collection has no existing flashcards yet.'

        states = ReviewState.objects.filter(user=user, flashcard__collection_id__in=subtree_ids)
        counts = {row['state']: row['n'] for row in states.values('state').annotate(n=Count('id'))}
        reviewed_count = sum(counts.values())
        if reviewed_count == 0:
            return f'This collection has {total_cards} existing flashcards, none of which the user has studied yet.'

        lapses_total = states.aggregate(total_lapses=Sum('lapses'))['total_lapses'] or 0
        never_reviewed = max(total_cards - reviewed_count, 0)
        learning = counts.get(ReviewState.State.NEW, 0) + counts.get(ReviewState.State.LEARNING, 0)
        review = counts.get(ReviewState.State.REVIEW, 0)
        relearning = counts.get(ReviewState.State.RELEARNING, 0)

        return (
            f'Of {total_cards} existing flashcards: {never_reviewed} never studied, {learning} still being '
            f'learned, {review} mastered/in regular review, {relearning} being relearned after being '
            f'forgotten ({lapses_total} total lapses across all reviews).'
        )


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
