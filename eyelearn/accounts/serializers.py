from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

User = get_user_model()


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    first_name = serializers.CharField(required=True, max_length=150)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'password', 'first_name']
        read_only_fields = ['id']

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class GoogleAuthSerializer(serializers.Serializer):
    id_token = serializers.CharField()


class UpdateProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['username', 'email', 'first_name']


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
