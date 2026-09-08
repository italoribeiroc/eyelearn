from rest_framework import serializers

from . import storage
from .models import Collection, CollectionGoal, Flashcard, FlashcardMedia, GenerationSourceDocument, ReviewLog
from .services import ReviewService


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
