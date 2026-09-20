from django.utils import timezone
from rest_framework import serializers

from . import storage
from .models import (
    Collection,
    CollectionGoal,
    Exam,
    ExamQuestion,
    Flashcard,
    FlashcardMedia,
    GenerationSourceDocument,
    ReviewLog,
)
from .services import ExamService, ReviewService, build_exam_summary


class CollectionSerializer(serializers.ModelSerializer):
    flashcard_count = serializers.SerializerMethodField()
    due_count = serializers.SerializerMethodField()

    class Meta:
        model = Collection
        fields = [
            'id', 'name', 'description', 'parent', 'flashcard_count', 'due_count', 'created_at', 'updated_at',
        ]

    def get_flashcard_count(self, collection):
        return collection.flashcards.count()

    def get_due_count(self, collection):
        request = self.context.get('request')
        if request is None:
            return 0
        return ReviewService().count_due(user=request.user, collection=collection)


class FlashcardMediaSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()

    class Meta:
        model = FlashcardMedia
        fields = ['id', 'media_type', 'side', 'content_type', 'size_bytes', 'url', 'created_at']
        read_only_fields = fields

    def get_url(self, media):
        return storage.generate_download_url(key=media.storage_key)


class FlashcardSerializer(serializers.ModelSerializer):
    media = FlashcardMediaSerializer(many=True, read_only=True)

    class Meta:
        model = Flashcard
        fields = [
            'id', 'collection', 'card_type', 'prompt', 'answer', 'options', 'accepted_answers',
            'media', 'created_at', 'updated_at',
        ]
        # collection is set from the URL (see views.flashcard_list), not the request body.
        read_only_fields = ['collection']

    def validate(self, attrs):
        card_type = attrs.get('card_type', getattr(self.instance, 'card_type', None))

        if card_type == Flashcard.CardType.MULTIPLE_CHOICE:
            options = attrs.get('options', getattr(self.instance, 'options', None))
            if not options or len(options) < 2:
                raise serializers.ValidationError({'options': 'Multiple-choice cards need at least 2 options.'})
            correct_count = sum(1 for option in options if option.get('is_correct'))
            if correct_count != 1:
                raise serializers.ValidationError(
                    {'options': 'Multiple-choice cards need exactly one correct option.'},
                )
        elif card_type == Flashcard.CardType.TYPED_ANSWER:
            answer = attrs.get('answer', getattr(self.instance, 'answer', ''))
            if not answer:
                raise serializers.ValidationError({'answer': 'Typed-answer cards need a canonical answer.'})

        return attrs


class MediaUploadURLRequestSerializer(serializers.Serializer):
    media_type = serializers.ChoiceField(choices=FlashcardMedia.MediaType.choices)
    side = serializers.ChoiceField(choices=FlashcardMedia.Side.choices)
    content_type = serializers.CharField()
    filename = serializers.CharField()
    size_bytes = serializers.IntegerField(min_value=1)


class MediaConfirmSerializer(serializers.Serializer):
    storage_key = serializers.CharField()
    media_type = serializers.ChoiceField(choices=FlashcardMedia.MediaType.choices)
    side = serializers.ChoiceField(choices=FlashcardMedia.Side.choices)
    content_type = serializers.CharField()
    size_bytes = serializers.IntegerField(min_value=1)


class CollectionGoalSerializer(serializers.ModelSerializer):
    class Meta:
        model = CollectionGoal
        fields = ['target_date']


class AiGenerationRequestSerializer(serializers.Serializer):
    card_type = serializers.ChoiceField(choices=Flashcard.CardType.choices)
    # Required unless auto=true, in which case the AI itself decides the
    # total -- validated in AiFlashcardGenerationService.generate, not here,
    # since "required" is conditional on another field.
    count = serializers.IntegerField(required=False, allow_null=True)
    auto = serializers.BooleanField(required=False, default=False)
    # Optional: a generation can now rely entirely on attached source
    # documents (see source_document_ids) instead of typed text -- "at least
    # one of the two" is enforced in AiFlashcardGenerationService.generate.
    learning_request = serializers.CharField(required=False, allow_blank=True, default='')
    source_document_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )


class SourceDocumentUploadURLRequestSerializer(serializers.Serializer):
    content_type = serializers.CharField()
    filename = serializers.CharField()
    size_bytes = serializers.IntegerField(min_value=1)


class SourceDocumentConfirmSerializer(serializers.Serializer):
    storage_key = serializers.CharField()
    content_type = serializers.CharField()
    filename = serializers.CharField()
    size_bytes = serializers.IntegerField(min_value=1)


class SourceDocumentUpdateTextSerializer(serializers.Serializer):
    extracted_text = serializers.CharField(allow_blank=True)


class GenerationSourceDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = GenerationSourceDocument
        fields = ['id', 'filename', 'content_type', 'size_bytes', 'char_count', 'created_at']
        read_only_fields = fields


class GenerationSourceDocumentDetailSerializer(GenerationSourceDocumentSerializer):
    """Adds the actual extracted/transcribed text, so the uploader can check
    (and, via source_document_update, correct) what the server read from
    their file -- most useful for a photo or scanned page, where the vision
    fallback can occasionally misread something. Deliberately not used in
    _serialize_draft (views.py), which is polled repeatedly while a
    generation is running: only the upload-confirm and update responses
    need the full text, not every draft-status poll."""

    class Meta(GenerationSourceDocumentSerializer.Meta):
        fields = GenerationSourceDocumentSerializer.Meta.fields + ['extracted_text']


class AiRegenerateRequestSerializer(serializers.Serializer):
    selected_ids = serializers.ListField(child=serializers.IntegerField())
    instruction = serializers.CharField()


class AiRemoveCardsRequestSerializer(serializers.Serializer):
    card_ids = serializers.ListField(child=serializers.IntegerField())


class ReviewSubmissionSerializer(serializers.Serializer):
    rating = serializers.ChoiceField(choices=ReviewLog.Rating.choices, required=False)
    selected_option = serializers.IntegerField(required=False, min_value=0)
    submitted_answer = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        flashcard = self.context['flashcard']

        if flashcard.card_type == Flashcard.CardType.BASIC and 'rating' not in attrs:
            raise serializers.ValidationError({'rating': 'This field is required for basic cards.'})
        elif flashcard.card_type == Flashcard.CardType.MULTIPLE_CHOICE and 'selected_option' not in attrs:
            raise serializers.ValidationError({'selected_option': 'This field is required for multiple-choice cards.'})
        elif flashcard.card_type == Flashcard.CardType.TYPED_ANSWER and 'submitted_answer' not in attrs:
            raise serializers.ValidationError({'submitted_answer': 'This field is required for typed-answer cards.'})

        return attrs


class ExamCreateSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=Exam.Mode.choices)
    # random
    collection_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    count = serializers.IntegerField(required=False, allow_null=True)
    # selected
    card_ids = serializers.ListField(child=serializers.IntegerField(), required=False)
    # retake
    exam_id = serializers.IntegerField(required=False)
    # null/omitted means untimed; range-checked in ExamService so the error code is stable.
    time_limit_minutes = serializers.IntegerField(required=False, allow_null=True)


class ExamAnswerSerializer(serializers.Serializer):
    question_id = serializers.IntegerField()
    selected_option = serializers.IntegerField(required=False, allow_null=True)
    # Length/blank rules are enforced in ExamService with a stable error code.
    submitted_answer = serializers.CharField(required=False, allow_null=True, allow_blank=True, trim_whitespace=False)
    self_correct = serializers.BooleanField(required=False, allow_null=True)


def _live_media(question, *, sides):
    """Media comes from the live card (it isn't snapshotted), so it disappears
    if the card was deleted. Uses the prefetched `flashcard.media` cache."""
    if question.flashcard is None:
        return []
    return FlashcardMediaSerializer(
        [media for media in question.flashcard.media.all() if media.side in sides], many=True,
    ).data


class ExamQuestionPlayerSerializer(serializers.ModelSerializer):
    """A question as shown while the exam is running. Must never reveal which
    answer is right: no `is_correct`, no `accepted_answers`, options as bare
    text, and `answer` (plus answer-side media) only for basic flip cards,
    which have to show it for self-grading."""

    options = serializers.SerializerMethodField()
    answer = serializers.SerializerMethodField()
    media = serializers.SerializerMethodField()
    answered = serializers.SerializerMethodField()

    class Meta:
        model = ExamQuestion
        fields = [
            'id', 'position', 'card_type', 'prompt', 'options', 'answer', 'media',
            'selected_option', 'submitted_answer', 'self_correct', 'answered',
        ]
        read_only_fields = fields

    def get_options(self, question):
        return [option.get('text', '') for option in (question.options or [])]

    def get_answer(self, question):
        return question.answer if question.card_type == Flashcard.CardType.BASIC else None

    def get_media(self, question):
        if question.card_type == Flashcard.CardType.BASIC:
            return _live_media(question, sides={FlashcardMedia.Side.PROMPT, FlashcardMedia.Side.ANSWER})
        return _live_media(question, sides={FlashcardMedia.Side.PROMPT})

    def get_answered(self, question):
        return question.answered_at is not None


class ExamQuestionResultSerializer(serializers.ModelSerializer):
    """A question after the exam is finished: everything, from the snapshot."""

    media = serializers.SerializerMethodField()
    card_deleted = serializers.SerializerMethodField()
    answered = serializers.SerializerMethodField()

    class Meta:
        model = ExamQuestion
        fields = [
            'id', 'position', 'card_type', 'prompt', 'answer', 'options', 'accepted_answers', 'media',
            'selected_option', 'submitted_answer', 'self_correct', 'is_correct', 'answered',
            'answered_after_time', 'card_deleted',
        ]
        read_only_fields = fields

    def get_media(self, question):
        return _live_media(question, sides={FlashcardMedia.Side.PROMPT, FlashcardMedia.Side.ANSWER})

    def get_card_deleted(self, question):
        return question.flashcard_id is None

    def get_answered(self, question):
        return question.answered_at is not None


def _exam_header(exam):
    ends_at = exam.ends_at
    return {
        'id': exam.id,
        'status': exam.status,
        'mode': exam.mode,
        'time_limit_seconds': exam.time_limit_seconds,
        'started_at': exam.started_at,
        'ends_at': ends_at,
        'finished_at': exam.finished_at,
        'source_labels': exam.source_labels,
        'server_now': timezone.now(),
    }


def serialize_exam_detail(exam):
    """In-progress exams get the answer-free player shape; completed ones get
    the full result shape plus a summary. Expects `exam.questions` to be
    loaded with select_related('flashcard') + prefetch_related('flashcard__media')."""
    questions = list(exam.questions.all())
    data = _exam_header(exam)
    data['total'] = len(questions)
    data['answered_count'] = sum(1 for question in questions if question.answered_at is not None)
    if exam.status == Exam.Status.IN_PROGRESS:
        data['questions'] = ExamQuestionPlayerSerializer(questions, many=True).data
        data['summary'] = None
    else:
        data['questions'] = ExamQuestionResultSerializer(questions, many=True).data
        data['summary'] = ExamService().build_summary(exam=exam, questions=questions)
    return data


def serialize_exam_row(exam):
    """A history-list row. Expects the queryset annotated by
    views._annotated_exams (counts only, no per-exam queries)."""
    data = _exam_header(exam)
    data['total'] = exam.total_questions
    data['answered_count'] = exam.answered_questions
    data['summary'] = None
    if exam.status == Exam.Status.COMPLETED:
        data['summary'] = build_exam_summary(
            exam,
            total=exam.total_questions,
            correct=exam.correct_questions,
            answered=exam.answered_questions,
            late=exam.late_questions,
            late_correct=exam.late_correct_questions,
        )
    return data
