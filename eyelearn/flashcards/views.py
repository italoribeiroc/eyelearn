from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Collection, Flashcard, FlashcardMedia
from .serializers import (
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
    MediaService,
    ReviewService,
    UnsupportedMediaError,
)


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
        return Response(CollectionSerializer(queryset, many=True).data)

    serializer = CollectionSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        collection = CollectionService().create_collection(user=request.user, **serializer.validated_data)
    except CrossOwnerParentError:
        return Response({'detail': 'Parent collection not found.'}, status=status.HTTP_404_NOT_FOUND)
    except CollectionLimitError as exc:
        return Response({'detail': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)

    return Response(CollectionSerializer(collection).data, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def collection_detail(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    if request.method == 'GET':
        return Response(CollectionSerializer(collection).data)

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

    return Response(CollectionSerializer(collection).data)


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


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def study_queue(request, collection_id):
    collection = _user_collection_or_404(request.user, collection_id)

    limit = request.query_params.get('limit')
    review_states = ReviewService().build_study_queue(
        user=request.user, collection=collection, limit=int(limit) if limit else None,
    )

    return Response([
        {
            'flashcard': FlashcardSerializer(review_state.flashcard).data,
            'due': review_state.due,
            'state': review_state.state,
            'reps': review_state.reps,
        }
        for review_state in review_states
    ])


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
