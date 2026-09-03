from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from rest_framework import serializers

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    first_name = serializers.CharField(required=True, max_length=150)
    locale = serializers.ChoiceField(choices=['en', 'pt-BR'], required=False, default='en')
    terms_accepted = serializers.BooleanField(write_only=True, required=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'password', 'first_name', 'locale', 'terms_accepted']
        read_only_fields = ['id']

    def validate_terms_accepted(self, value):
        if not value:
            raise serializers.ValidationError('You must accept the Terms of Service and Privacy Policy.')
        return value

    def create(self, validated_data):
        validated_data.pop('locale', None)  # not a model field, only carries the verification email's language
        validated_data.pop('terms_accepted', None)  # not a model field, checked in validate_terms_accepted
        return User.objects.create_user(**validated_data, is_active=False, terms_accepted_at=timezone.now())


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField()


class UpdateProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['username', 'email', 'first_name', 'has_seen_onboarding']


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    locale = serializers.ChoiceField(choices=['en', 'pt-BR'], required=False, default='en')


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(validators=[validate_password])


class AccountDeletionSerializer(serializers.Serializer):
    username = serializers.CharField()

    def validate_username(self, value):
        if value != self.context['request'].user.username:
            raise serializers.ValidationError('Username does not match.')
        return value


class EmailVerificationConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()


class ResendVerificationSerializer(serializers.Serializer):
    email = serializers.EmailField()
    locale = serializers.ChoiceField(choices=['en', 'pt-BR'], required=False, default='en')
