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


class CollectionGoal(models.Model):
    """A user's target date to master (see ReviewState.State.REVIEW) every
    flashcard in a collection's subtree."""

    collection = models.OneToOneField(
        Collection, on_delete=models.CASCADE, primary_key=True, related_name='goal',
    )
    target_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.collection_id} due {self.target_date}'


class StudyDay(models.Model):
    """Denormalized per-user daily review count, upserted from submit_review.

    ReviewLog has no direct `user` FK (only via review_state.user) and grows
    unboundedly, so streak/calendar queries read this table instead of
    scanning ReviewLog.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='study_days')
    date = models.DateField()
    cards_reviewed = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['user', 'date'], name='unique_user_study_day'),
        ]
        indexes = [models.Index(fields=['user', 'date'])]

    def __str__(self):
        return f'{self.user_id}:{self.date}'


class FlashcardGenerationDraft(models.Model):
    """A pending batch of AI-generated flashcards awaiting user review.

    Persisted (rather than kept only in frontend state) because review lives
    on its own page (see eyelearn-ui's ai-generate/<draftId> route) that must
    survive a refresh or back-navigation. `cards` is a single JSON blob, not
    a child row-per-card table, since nothing else in the schema ever needs
    to query an individual draft card in isolation.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        CONFIRMED = 'confirmed', 'Confirmed'
        DISCARDED = 'discarded', 'Discarded'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='flashcard_generation_drafts',
    )
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name='generation_drafts')
    card_type = models.CharField(max_length=20, choices=Flashcard.CardType.choices)
    learning_request = models.TextField()
    # Total the user asked for. A single Claude call is capped (see
    # AI_GENERATION_BATCH_SIZE in services.py) so large requests are filled
    # by repeated POST .../generate-next-batch/ calls, each appending a
    # bounded chunk to `cards` -- generation is "complete" once
    # len(cards) >= target_count.
    target_count = models.PositiveIntegerField(default=0)
    # Each item: {"id": int, "prompt": str, "answer": str, "options": [...], "accepted_answers": [...]}.
    # "id" is a small per-draft counter (1, 2, 3, ...) assigned at generation time -- a stable
    # identity for regenerate/remove requests that doesn't depend on array position.
    cards = models.JSONField(default=list)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=['user', 'collection', 'status'])]

    def __str__(self):
        return f'{self.user_id}:{self.collection_id} ({self.status}, {len(self.cards)} cards)'


class GenerationSourceDocument(models.Model):
    """Extracted text from a user-uploaded document (PDF/TXT/MD/image), fed
    into AI flashcard generation the same way learning_request is. The raw
    file is never persisted -- storage.delete_object() runs right after
    extraction (see SourceDocumentService.confirm_upload).

    `draft` starts null: a document is uploaded from AiGenerateDialog before
    any draft exists. AiFlashcardGenerationService.generate() links it to the
    new draft, so generate_next_batch/regenerate can re-read the same
    extracted_text later without re-uploading.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='generation_source_documents',
    )
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name='generation_source_documents')
    draft = models.ForeignKey(
        FlashcardGenerationDraft, on_delete=models.CASCADE, null=True, blank=True, related_name='source_documents',
    )
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField()
    # Cleared (set to '') once the draft this document was used for reaches a
    # terminal state -- see AiFlashcardGenerationService.confirm/discard.
    extracted_text = models.TextField(blank=True, default='')
    char_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.filename


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
