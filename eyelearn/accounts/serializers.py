from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    first_name = serializers.CharField(required=True, max_length=150)
    locale = serializers.ChoiceField(choices=['en', 'pt-BR'], required=False, default='en')

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'password', 'first_name', 'locale']
        read_only_fields = ['id']

    def create(self, validated_data):
        validated_data.pop('locale', None)  # not a model field, only carries the verification email's language
        return User.objects.create_user(**validated_data, is_active=False)


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
