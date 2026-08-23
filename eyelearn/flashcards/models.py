from django.conf import settings
from django.db import models


class Collection(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='flashcard_collections',
    )
    parent = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='children',
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'parent'])]
        ordering = ['name']

    def __str__(self):
        return self.name


class Flashcard(models.Model):
    class CardType(models.TextChoices):
        BASIC = 'basic', 'Basic'
        MULTIPLE_CHOICE = 'multiple_choice', 'Multiple choice'
        TYPED_ANSWER = 'typed_answer', 'Typed answer'

    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name='flashcards')
    card_type = models.CharField(max_length=20, choices=CardType.choices)
    prompt = models.TextField()
    # BASIC: back text shown on flip. TYPED_ANSWER: canonical answer. Unused for MULTIPLE_CHOICE.
    answer = models.TextField(blank=True, default='')
    # MULTIPLE_CHOICE only: [{"text": str, "is_correct": bool}, ...]
    options = models.JSONField(blank=True, default=list)
    # TYPED_ANSWER only: extra accepted alternates besides `answer`
    accepted_answers = models.JSONField(blank=True, default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return self.prompt[:50]


class FlashcardMedia(models.Model):
    class MediaType(models.TextChoices):
        IMAGE = 'image', 'Image'
        AUDIO = 'audio', 'Audio'
        VIDEO = 'video', 'Video'

    class Side(models.TextChoices):
        PROMPT = 'prompt', 'Prompt'
        ANSWER = 'answer', 'Answer'

    flashcard = models.ForeignKey(Flashcard, on_delete=models.CASCADE, related_name='media')
    media_type = models.CharField(max_length=10, choices=MediaType.choices)
    side = models.CharField(max_length=10, choices=Side.choices)
    storage_key = models.CharField(max_length=512)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['storage_key'], name='unique_flashcard_media_storage_key'),
        ]

    def __str__(self):
        return self.storage_key


class ReviewState(models.Model):
    """FSRS scheduling state for a (user, flashcard) pair.

    Kept separate from Flashcard (rather than fields on the card itself) so a
    collection could be studied by multiple users later without a schema change.
    """

    class State(models.TextChoices):
        NEW = 'new', 'New'
        LEARNING = 'learning', 'Learning'
        REVIEW = 'review', 'Review'
        RELEARNING = 'relearning', 'Relearning'

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='review_states')
    flashcard = models.ForeignKey(Flashcard, on_delete=models.CASCADE, related_name='review_states')
    due = models.DateTimeField()
    # step: fsrs's learning/relearning step index, needed to resume a card's
    # in-progress learning sequence correctly between reviews.
    step = models.PositiveIntegerField(null=True, blank=True, default=None)
    stability = models.FloatField(null=True, blank=True, default=None)
    difficulty = models.FloatField(null=True, blank=True, default=None)
    elapsed_days = models.PositiveIntegerField(default=0)
    scheduled_days = models.PositiveIntegerField(default=0)
    reps = models.PositiveIntegerField(default=0)
    lapses = models.PositiveIntegerField(default=0)
    state = models.CharField(max_length=12, choices=State.choices, default=State.NEW)
    last_review = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'flashcard'], name='unique_user_flashcard_review_state'),
        ]
        # Backs the study-queue query: WHERE user_id=? AND due<=? ORDER BY due
        indexes = [models.Index(fields=['user', 'due'])]

    def __str__(self):
        return f'{self.user_id}:{self.flashcard_id}'


class ReviewLog(models.Model):
    class Rating(models.IntegerChoices):
        AGAIN = 1, 'Again'
        HARD = 2, 'Hard'
        GOOD = 3, 'Good'
        EASY = 4, 'Easy'

    review_state = models.ForeignKey(ReviewState, on_delete=models.CASCADE, related_name='logs')
    rating = models.PositiveSmallIntegerField(choices=Rating.choices)
    reviewed_at = models.DateTimeField(auto_now_add=True)
    # Snapshots of the fsrs Card (via Card.to_dict()) before/after this review,
    # kept for future FSRS parameter re-optimization.
    state_before = models.JSONField()
    state_after = models.JSONField()

    class Meta:
        ordering = ['reviewed_at']
        indexes = [models.Index(fields=['review_state', 'reviewed_at'])]

    def __str__(self):
        return f'{self.review_state_id} rating={self.rating} at={self.reviewed_at}'
