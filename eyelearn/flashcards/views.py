from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from .ai_generation import AiGenerationError
from .ai_providers import pop_last_provider_used
from .models import (
    Collection,
    CollectionGoal,
    Flashcard,
    FlashcardGenerationDraft,
    FlashcardMedia,
    GenerationSourceDocument,
)
from .serializers import (
    AiGenerationRequestSerializer,
    AiRegenerateRequestSerializer,
    AiRemoveCardsRequestSerializer,
    CollectionGoalSerializer,
    CollectionSerializer,
    FlashcardMediaSerializer,
    FlashcardSerializer,
    GenerationSourceDocumentDetailSerializer,
    GenerationSourceDocumentSerializer,
    MediaConfirmSerializer,
    MediaUploadURLRequestSerializer,
    ReviewSubmissionSerializer,
    SourceDocumentConfirmSerializer,
    SourceDocumentUpdateTextSerializer,
    SourceDocumentUploadURLRequestSerializer,
)
from .services import (
    AiFlashcardGenerationService,
    AiGenerationNotAllowedError,
    AiGenerationValidationError,
    CollectionCycleError,
    CollectionLimitError,
    CollectionService,
    CrossOwnerParentError,
    DocumentExtractionFailedError,
    FlashcardLimitError,
    FlashcardService,
    GoalService,
    MediaService,
    ReviewService,
    SourceDocumentService,
    StreakService,
    TooManySourceDocumentsError,
    UnsupportedDocumentError,
    UnsupportedMediaError,
)

STREAK_CALENDAR_MAX_RANGE_DAYS = 366


class AiGenerationRateThrottle(UserRateThrottle):
    scope = 'ai_flashcard_generation'


class AiGenerationBatchRateThrottle(UserRateThrottle):
    """Separate, more generous scope for generate-next-batch -- a single
    large (up to 500-card) generation can legitimately need up to
    MAX_AI_GENERATE_COUNT / AI_GENERATION_BATCH_SIZE calls (20), which would
    exhaust the regular ai_flashcard_generation budget in one go."""
    scope = 'ai_flashcard_generation_batch'


class SourceDocumentThrottle(UserRateThrottle):
    scope = 'ai_source_document'


def _user_collection_or_404(user, collection_id):
    return get_object_or_404(Collection, pk=collection_id, user=user)


def _user_flashcard_or_404(user, flashcard_id):
    return get_object_or_404(Flashcard, pk=flashcard_id, collection__user=user)


def _user_draft_or_404(user, draft_id):
    return get_object_or_404(FlashcardGenerationDraft, pk=draft_id, user=user)


def _user_draft_or_404_locked(user, draft_id):
    """Same as _user_draft_or_404, but row-locked (must be called inside
    transaction.atomic()). Background generation means generate-next-batch,
    regenerate, remove-cards, confirm, and discard can now genuinely race
    against each other for the same draft (e.g. the user removes a card
    while a background batch is still landing) -- locking makes each
    mutation's read-modify-write of `cards` atomic instead of last-write-wins.
    """
    return get_object_or_404(
        FlashcardGenerationDraft.objects.select_for_update(), pk=draft_id, user=user,
    )


def _serialize_draft(draft):
    return {
        'id': draft.id,
        'collection': draft.collection_id,
        'card_type': draft.card_type,
        'learning_request': draft.learning_request,
        'status': draft.status,
        'target_count': draft.target_count,
        'cards': draft.cards,
        'source_documents': GenerationSourceDocumentSerializer(draft.source_documents.all(), many=True).data,
    }


def _draft_response(draft, *, status_code=status.HTTP_200_OK):
    """Wraps a successful draft response with an X-AI-Provider header
    reporting which provider (claude/gemini/groq) actually served the AI
    call this view just made -- a way for us to check which one is live in
    a given environment (via curl/devtools, or a real Vercel request's
    response headers in the logs) without adding anything a normal user
    would ever notice; nothing in the UI reads or displays this header."""
    response = Response(_serialize_draft(draft), status=status_code)
    response['X-AI-Provider'] = pop_last_provider_used() or 'none'
    return response


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def collection_list(request):
    if request.method == 'GET':
        queryset = Collection.objects.filter(user=request.user)
        parent = request.query_params.get('parent')
        if parent == 'null':
            queryset = queryset.filter(parent__isnull=True)
        elif parent is not None:
            queryset = queryset.filter(parent_id=parent)
        return Response(CollectionSerializer(queryset, many=True, context={'request': request}).data)

    serializer = CollectionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        collection = CollectionService().create_collection(user=request.user, **serializer.validated_data)
    except CrossOwnerParentError:
        return Response({'detail': 'Parent collection not found.'}, status=status.HTTP_404_NOT_FOUND)
    except CollectionLimitError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)

    return Response(
        CollectionSerializer(collection, context={'request': request}).data, status=status.HTTP_201_CREATED,
    )


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def collection_detail(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    if request.method == 'GET':
        return Response(CollectionSerializer(collection, context={'request': request}).data)

    if request.method == 'DELETE':
        CollectionService().delete_collection(collection=collection)
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = CollectionSerializer(collection, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)

    try:
        collection = CollectionService().update_collection(collection=collection, **serializer.validated_data)
    except CrossOwnerParentError:
        return Response({'detail': 'Parent collection not found.'}, status=status.HTTP_404_NOT_FOUND)
    except CollectionCycleError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(CollectionSerializer(collection, context={'request': request}).data)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def flashcard_list(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    if request.method == 'GET':
        return Response(FlashcardSerializer(collection.flashcards.all(), many=True).data)

    serializer = FlashcardSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        FlashcardService().assert_can_create(user=request.user)
    except FlashcardLimitError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)

    flashcard = serializer.save(collection=collection)
    return Response(FlashcardSerializer(flashcard).data, status=status.HTTP_201_CREATED)


# Matches the frontend's own MAX_IMPORT_BATCH_SIZE (src/lib/flashcards/import.ts)
# -- that's the only caller today (see its docstring for why importing in
# batches exists at all), but this cap is enforced independently here too.
MAX_BULK_CREATE_SIZE = 100


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def flashcard_bulk_create(request, collection_id):
    """Creates up to MAX_BULK_CREATE_SIZE flashcards in one request.

    Exists specifically for importing a large deck (see eyelearn-ui's
    import route): looping flashcard_list's single-create endpoint once per
    card means hundreds or thousands of requests for one big import, which
    blows through DEFAULT_THROTTLE_RATES's 'user' scope (120/min, the
    fallback flashcard_list itself sits under -- no throttle_classes of its
    own) long before the deck finishes. Batching many cards into few
    requests keeps a large import's total request count sane regardless of
    deck size, the same way generate-next-batch exists so a large AI
    generation doesn't need one request per card either.

    Still validates and creates one card at a time (not a raw bulk_create)
    so each card gets the same FlashcardSerializer validation and
    assert_can_create gating as the single-create endpoint, and a bad card
    doesn't prevent the valid ones around it from saving.
    """
    collection = _user_collection_or_404(request.user, collection_id)

    cards_data = request.data.get('cards')
    if not isinstance(cards_data, list) or not cards_data:
        return Response({'detail': 'cards must be a non-empty list.'}, status=status.HTTP_400_BAD_REQUEST)
    if len(cards_data) > MAX_BULK_CREATE_SIZE:
        return Response(
            {'detail': f'A bulk request can contain at most {MAX_BULK_CREATE_SIZE} cards.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    created = []
    errors = []
    limit_reached = False

    for index, card_data in enumerate(cards_data):
        if limit_reached:
            # Every remaining card would hit the same cap -- report them as
            # skipped without bothering to validate/attempt each one.
            errors.append({'index': index, 'errors': {'detail': 'Flashcard limit reached.'}})
            continue

        serializer = FlashcardSerializer(data=card_data)
        if not serializer.is_valid():
            errors.append({'index': index, 'errors': serializer.errors})
            continue

        try:
            FlashcardService().assert_can_create(user=request.user)
        except FlashcardLimitError:
            limit_reached = True
            errors.append({'index': index, 'errors': {'detail': 'Flashcard limit reached.'}})
            continue

        created.append(serializer.save(collection=collection))

    return Response({
        'created': FlashcardSerializer(created, many=True).data,
        'errors': errors,
        'limit_reached': limit_reached,
    })


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def flashcard_detail(request, flashcard_id):
    flashcard = _user_flashcard_or_404(request.user, flashcard_id)

    if request.method == 'GET':
        return Response(FlashcardSerializer(flashcard).data)

    if request.method == 'DELETE':
        flashcard.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = FlashcardSerializer(flashcard, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    flashcard = serializer.save()
    return Response(FlashcardSerializer(flashcard).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def media_upload_url(request, flashcard_id):
    flashcard = _user_flashcard_or_404(request.user, flashcard_id)

    serializer = MediaUploadURLRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        storage_key, upload_url = MediaService().create_upload_url(
            user=request.user, flashcard=flashcard, **serializer.validated_data,
        )
    except UnsupportedMediaError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'storage_key': storage_key, 'upload_url': upload_url})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def media_confirm(request, flashcard_id):
    flashcard = _user_flashcard_or_404(request.user, flashcard_id)

    serializer = MediaConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        media = MediaService().confirm_upload(flashcard=flashcard, **serializer.validated_data)
    except UnsupportedMediaError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(FlashcardMediaSerializer(media).data, status=status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def media_delete(request, media_id):
    media = get_object_or_404(FlashcardMedia, pk=media_id, flashcard__collection__user=request.user)
    MediaService().delete_media(media=media)
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([SourceDocumentThrottle])
def source_document_upload_url(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    serializer = SourceDocumentUploadURLRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        storage_key, upload_url = SourceDocumentService().create_upload_url(
            user=request.user, collection=collection, **serializer.validated_data,
        )
    except AiGenerationNotAllowedError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except UnsupportedDocumentError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'storage_key': storage_key, 'upload_url': upload_url})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([SourceDocumentThrottle])
def source_document_confirm(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    serializer = SourceDocumentConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        document = SourceDocumentService().confirm_upload(
            user=request.user, collection=collection, **serializer.validated_data,
        )
    except AiGenerationNotAllowedError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except (UnsupportedDocumentError, DocumentExtractionFailedError, TooManySourceDocumentsError) as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(GenerationSourceDocumentDetailSerializer(document).data, status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def source_document_detail(request, document_id):
    # Only reachable while unlinked (not yet used by a generation) -- same
    # scope as delete: once a draft has consumed a document's text, editing
    # it here would have no effect on cards already generated from it.
    document = get_object_or_404(
        GenerationSourceDocument, pk=document_id, user=request.user, draft__isnull=True,
    )

    if request.method == 'DELETE':
        SourceDocumentService().delete_document(document=document)
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = SourceDocumentUpdateTextSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    document = SourceDocumentService().update_text(document=document, **serializer.validated_data)
    return Response(GenerationSourceDocumentDetailSerializer(document).data)


def _serialize_queue_items(review_states):
    return [
        {
            'flashcard': FlashcardSerializer(review_state.flashcard).data,
            'due': review_state.due,
            'state': review_state.state,
            'reps': review_state.reps,
        }
        for review_state in review_states
    ]


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def study_queue(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    limit = request.query_params.get('limit')
    review_states = ReviewService().build_study_queue(
        user=request.user, collection=collection, limit=int(limit) if limit else None,
    )

    return Response(_serialize_queue_items(review_states))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def daily_study_queue(request):
    limit = request.query_params.get('limit')
    review_states = GoalService().build_daily_study_queue(
        user=request.user, limit=int(limit) if limit else None,
    )
    return Response(_serialize_queue_items(review_states))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def custom_study_queue(request):
    raw_ids = request.query_params.get('collections', '')
    collection_ids = [int(value) for value in raw_ids.split(',') if value.strip().isdigit()]
    if not collection_ids:
        return Response({'detail': 'At least one collection id is required.'}, status=status.HTTP_400_BAD_REQUEST)

    limit = request.query_params.get('limit')
    review_states = ReviewService().build_multi_collection_queue(
        user=request.user, collection_ids=collection_ids, limit=int(limit) if limit else None,
    )
    return Response(_serialize_queue_items(review_states))


@api_view(['GET', 'PUT', 'DELETE'])
@permission_classes([IsAuthenticated])
def collection_goal(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    if request.method == 'DELETE':
        CollectionGoal.objects.filter(collection=collection).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method == 'PUT':
        serializer = CollectionGoalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        goal, _created = CollectionGoal.objects.update_or_create(
            collection=collection, defaults=serializer.validated_data,
        )
        return Response(GoalService().get_goal_progress(user=request.user, collection=collection, goal=goal))

    goal = get_object_or_404(CollectionGoal, collection=collection)
    return Response(GoalService().get_goal_progress(user=request.user, collection=collection, goal=goal))


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def goals_summary(request):
    today = timezone.now().date()
    streak_service = StreakService()
    goal_service = GoalService()

    goals = CollectionGoal.objects.filter(collection__user=request.user).select_related('collection')
    active_goals = []
    daily_target_total = 0
    for goal in goals:
        progress = goal_service.get_goal_progress(user=request.user, collection=goal.collection, goal=goal, today=today)
        active_goals.append({**progress, 'collection_name': goal.collection.name})
        daily_target_total += progress['today_target']

    daily_due_count = len(goal_service.build_daily_study_queue(user=request.user))

    return Response({
        'streak': streak_service.get_current_streak(user=request.user, today=today),
        'cards_studied_today': streak_service.get_cards_studied_today(user=request.user, today=today),
        'active_goals': active_goals,
        'daily_target_total': daily_target_total,
        'daily_due_count': daily_due_count,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def streak_calendar(request):
    start_date = parse_date(request.query_params.get('start', ''))
    end_date = parse_date(request.query_params.get('end', ''))
    if not start_date or not end_date or end_date < start_date:
        return Response({'detail': 'Valid start and end dates are required.'}, status=status.HTTP_400_BAD_REQUEST)
    if (end_date - start_date) > timedelta(days=STREAK_CALENDAR_MAX_RANGE_DAYS):
        return Response(
            {'detail': f'Range cannot exceed {STREAK_CALENDAR_MAX_RANGE_DAYS} days.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    streak_service = StreakService()
    return Response({
        'current_streak': streak_service.get_current_streak(user=request.user),
        'days': streak_service.get_calendar(user=request.user, start_date=start_date, end_date=end_date),
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_review(request, flashcard_id):
    flashcard = _user_flashcard_or_404(request.user, flashcard_id)

    serializer = ReviewSubmissionSerializer(data=request.data, context={'flashcard': flashcard})
    serializer.is_valid(raise_exception=True)

    review_state, correct = ReviewService().submit_review(
        user=request.user, flashcard=flashcard, **serializer.validated_data,
    )

    return Response({
        'correct': correct,
        'due': review_state.due,
        'state': review_state.state,
        'reps': review_state.reps,
        'lapses': review_state.lapses,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([AiGenerationRateThrottle])
def generate_flashcards(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    serializer = AiGenerationRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        draft = AiFlashcardGenerationService().generate(
            user=request.user, collection=collection, **serializer.validated_data,
        )
    except AiGenerationNotAllowedError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except AiGenerationValidationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except AiGenerationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    return _draft_response(draft, status_code=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([AiGenerationBatchRateThrottle])
def generate_next_batch(request, draft_id):
    draft = _user_draft_or_404(request.user, draft_id)

    try:
        draft = AiFlashcardGenerationService().generate_next_batch(user=request.user, draft=draft)
    except AiGenerationNotAllowedError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except AiGenerationValidationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except AiGenerationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    return _draft_response(draft)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def ai_generation_draft_detail(request, draft_id):
    draft = _user_draft_or_404(request.user, draft_id)
    return Response(_serialize_draft(draft))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([AiGenerationRateThrottle])
def regenerate_draft_cards(request, draft_id):
    draft = _user_draft_or_404(request.user, draft_id)

    serializer = AiRegenerateRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        draft = AiFlashcardGenerationService().regenerate(
            user=request.user, draft=draft, **serializer.validated_data,
        )
    except AiGenerationNotAllowedError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except AiGenerationValidationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except AiGenerationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    return _draft_response(draft)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def remove_draft_cards(request, draft_id):
    serializer = AiRemoveCardsRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        with transaction.atomic():
            draft = _user_draft_or_404_locked(request.user, draft_id)
            draft = AiFlashcardGenerationService().remove_cards(
                user=request.user, draft=draft, **serializer.validated_data,
            )
    except AiGenerationValidationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(_serialize_draft(draft))


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def confirm_draft(request, draft_id):
    try:
        with transaction.atomic():
            draft = _user_draft_or_404_locked(request.user, draft_id)
            created, errors = AiFlashcardGenerationService().confirm(user=request.user, draft=draft)
    except AiGenerationValidationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        'created': FlashcardSerializer(created, many=True).data,
        'errors': errors,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def discard_draft(request, draft_id):
    try:
        with transaction.atomic():
            draft = _user_draft_or_404_locked(request.user, draft_id)
            draft = AiFlashcardGenerationService().discard(user=request.user, draft=draft)
    except AiGenerationValidationError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({'status': draft.status})
