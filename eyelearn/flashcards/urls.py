from django.urls import path

from .views import (
    collection_detail,
    collection_list,
    flashcard_detail,
    flashcard_list,
    media_confirm,
    media_delete,
    media_upload_url,
    study_queue,
    submit_review,
)

urlpatterns = [
    path('collections/', collection_list, name='collection_list'),
    path('collections/<int:collection_id>/', collection_detail, name='collection_detail'),
    path('collections/<int:collection_id>/flashcards/', flashcard_list, name='flashcard_list'),
    path('collections/<int:collection_id>/study-queue/', study_queue, name='study_queue'),
    path('flashcards/<int:flashcard_id>/', flashcard_detail, name='flashcard_detail'),
    path('flashcards/<int:flashcard_id>/media/upload-url/', media_upload_url, name='media_upload_url'),
    path('flashcards/<int:flashcard_id>/media/confirm/', media_confirm, name='media_confirm'),
    path('flashcards/<int:flashcard_id>/review/', submit_review, name='submit_review'),
    path('media/<int:media_id>/', media_delete, name='media_delete'),
]
