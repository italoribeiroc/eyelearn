from datetime import timedelta

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Collection, CollectionGoal, Flashcard, FlashcardMedia
from .serializers import (
    CollectionGoalSerializer,
    CollectionSerializer,
    FlashcardMediaSerializer,
    FlashcardSerializer,
    MediaConfirmSerializer,
    MediaUploadURLRequestSerializer,
    ReviewSubmissionSerializer,
)
from .services import (
    CollectionCycleError,
    CollectionLimitError,
    CollectionService,
    CrossOwnerParentError,
    GoalService,
    MediaService,
    ReviewService,
    StreakService,
    UnsupportedMediaError,
)

STREAK_CALENDAR_MAX_RANGE_DAYS = 366


def _user_collection_or_404(user, collection_id):
    return get_object_or_404(Collection, pk=collection_id, user=user)


def _user_flashcard_or_404(user, flashcard_id):
    return get_object_or_404(Flashcard, pk=flashcard_id, collection__user=user)


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
    flashcard = serializer.save(collection=collection)
    return Response(FlashcardSerializer(flashcard).data, status=status.HTTP_201_CREATED)


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
