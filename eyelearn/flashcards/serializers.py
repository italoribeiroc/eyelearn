from rest_framework import serializers

from . import storage
from .models import Collection, Flashcard, FlashcardMedia, ReviewLog


class CollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Collection
        fields = ['id', 'name', 'description', 'parent', 'created_at', 'updated_at']


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
