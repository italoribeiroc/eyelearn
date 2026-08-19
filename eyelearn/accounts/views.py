import re

from django.conf import settings
from django.contrib.auth import get_user_model
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import GoogleAuthSerializer, RegisterSerializer, UpdateProfileSerializer

User = get_user_model()


@api_view(['POST'])
@permission_classes([AllowAny])
def register(request):
    serializer = RegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.save()

    refresh = RefreshToken.for_user(user)
    return Response({
        'user': {
            'id': user.id,
            'username': user.username,
            'email': user.email,
        },
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    }, status=status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def me(request):
    user = request.user
    if request.method == 'PATCH':
        serializer = UpdateProfileSerializer(user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()

    return Response({
        'id': user.id,
        'username': user.username,
        'email': user.email,
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
            defaults={'username': _generate_unique_username(email)},
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
        },
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    }, status=status.HTTP_200_OK)
