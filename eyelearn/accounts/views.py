import re

from django.conf import settings
from django.contrib.auth import get_user_model
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import (
    AccountDeletionSerializer,
    EmailVerificationConfirmSerializer,
    GoogleAuthSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    ResendVerificationSerializer,
    UpdateProfileSerializer,
)
from .services import (
    AccountDeletionService,
    EmailVerificationService,
    InvalidResetTokenError,
    InvalidVerificationTokenError,
    PasswordResetService,
)

User = get_user_model()


class RegisterRateThrottle(AnonRateThrottle):
    scope = 'register'


class LoginRateThrottle(AnonRateThrottle):
    scope = 'login'


class RefreshRateThrottle(AnonRateThrottle):
    scope = 'token_refresh'


class PasswordResetRequestRateThrottle(AnonRateThrottle):
    scope = 'password_reset_request'


class PasswordResetConfirmRateThrottle(AnonRateThrottle):
    scope = 'password_reset_confirm'


class EmailVerificationResendRateThrottle(AnonRateThrottle):
    scope = 'email_verification_resend'


class EmailVerificationConfirmRateThrottle(AnonRateThrottle):
    scope = 'email_verification_confirm'


class ThrottledTokenObtainPairView(TokenObtainPairView):
    throttle_classes = [LoginRateThrottle]


class ThrottledTokenRefreshView(TokenRefreshView):
    throttle_classes = [RefreshRateThrottle]


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([RegisterRateThrottle])
def register(request):
    # Clear out any abandoned (never-verified) signup squatting this
    # username/email before validating, so a typo'd registration attempt
    # doesn't permanently block a retry.
    EmailVerificationService().reclaim_stale_signup(
        username=request.data.get('username'), email=request.data.get('email'),
    )

    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()

    plan = request.data.get('plan')
    EmailVerificationService().send_verification_email(
        user=user,
        locale=serializer.validated_data.get('locale', 'en'),
        plan=plan if plan in ('monthly', 'annual') else None,
    )

    return Response(
        {'detail': 'Account created. Check your email to verify it.'}, status=status.HTTP_201_CREATED,
    )


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def me(request):
    user = request.user

    if request.method == 'DELETE':
        serializer = AccountDeletionSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        AccountDeletionService().delete_account(user=user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    if request.method == 'PATCH':
        serializer = UpdateProfileSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

    return Response({
        'id': user.id,
        'username': user.username,
        'email': user.email,
        'first_name': user.first_name,
        'has_seen_onboarding': user.has_seen_onboarding,
    })


def _generate_unique_username(email):
    base = re.sub(r'[^a-zA-Z0-9_]', '', email.split('@')[0]).lower() or 'user'
    username = base
    suffix = 1
    while User.objects.filter(username=username).exists():
        suffix += 1
        username = f'{base}{suffix}'
    return username


@api_view(['POST'])
@permission_classes([AllowAny])
def google_auth(request):
    serializer = GoogleAuthSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        idinfo = google_id_token.verify_oauth2_token(
            serializer.validated_data['id_token'],
            google_requests.Request(),
            settings.GOOGLE_OAUTH_CLIENT_ID,
        )
    except ValueError:
        return Response({'detail': 'Invalid Google token.'}, status=status.HTTP_401_UNAUTHORIZED)

    if not idinfo.get('email_verified'):
        return Response({'detail': 'Google email is not verified.'}, status=status.HTTP_401_UNAUTHORIZED)

    google_sub = idinfo['sub']
    email = idinfo['email']

    user = User.objects.filter(google_id=google_sub).first()
    if user is None:
        user, created = User.objects.get_or_create(
            email=email,
            defaults={
                'username': _generate_unique_username(email),
                'first_name': idinfo.get('given_name', ''),
            },
        )
        if created:
            user.set_unusable_password()
        if not user.google_id:
            user.google_id = google_sub
            user.save(update_fields=['google_id', 'password'])

    refresh = RefreshToken.for_user(user)
    return Response({
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
            'first_name': user.first_name,
            'has_seen_onboarding': user.has_seen_onboarding,
        },
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    }, status=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetRequestRateThrottle])
def password_reset_request(request):
    serializer = PasswordResetRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    PasswordResetService().request_reset(
        email=serializer.validated_data['email'],
        locale=serializer.validated_data['locale'],
    )

    return Response({'detail': 'If that email exists, a reset link has been sent.'})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetConfirmRateThrottle])
def password_reset_confirm(request):
    serializer = PasswordResetConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        PasswordResetService().confirm_reset(**serializer.validated_data)
    except InvalidResetTokenError:
        return Response(
            {'detail': 'This reset link is invalid or has expired.'}, status=status.HTTP_400_BAD_REQUEST,
        )

    return Response({'detail': 'Password has been reset.'})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([EmailVerificationConfirmRateThrottle])
def verify_email(request):
    serializer = EmailVerificationConfirmSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        EmailVerificationService().confirm(**serializer.validated_data)
    except InvalidVerificationTokenError:
        return Response(
            {'detail': 'This verification link is invalid or has expired.'}, status=status.HTTP_400_BAD_REQUEST,
        )

    return Response({'detail': 'Email verified. You can now log in.'})


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([EmailVerificationResendRateThrottle])
def resend_verification(request):
    serializer = ResendVerificationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    EmailVerificationService().resend(
        email=serializer.validated_data['email'],
        locale=serializer.validated_data['locale'],
    )

    return Response({'detail': 'If that email exists and needs verification, a new link has been sent.'})
