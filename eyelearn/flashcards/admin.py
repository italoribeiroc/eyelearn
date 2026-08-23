from django.contrib import admin

from .models import Collection, Flashcard, ReviewState


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'user', 'parent', 'created_at']
    list_filter = ['created_at']
    search_fields = ['name', 'user__username', 'user__email']


@admin.register(Flashcard)
class FlashcardAdmin(admin.ModelAdmin):
    list_display = ['id', 'collection', 'card_type', 'created_at']
    list_filter = ['card_type', 'created_at']
    search_fields = ['prompt', 'answer']


@admin.register(ReviewState)
class ReviewStateAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'flashcard', 'state', 'due', 'reps', 'lapses']
    list_filter = ['state']
    search_fields = ['user__username', 'user__email']
    readonly_fields = [
        'due', 'step', 'stability', 'difficulty', 'elapsed_days', 'scheduled_days',
        'reps', 'lapses', 'state', 'last_review',
    ]
