import logging

import requests
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from .emails import send_contact_confirmation_email, send_contact_notification_email
from .serializers import ContactMessageSerializer

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([AllowAny])
def home(request):
    return Response({
        'message': 'API is running',
        'hello_url': '/api/hello/Italo/'
    })

@api_view(['GET'])
@permission_classes([AllowAny])
def hello_user(request, username):
    return Response({
        'message': f'Hello, {username}!'
    })


class ContactFormRateThrottle(UserRateThrottle):
    scope = 'contact_form'


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([ContactFormRateThrottle])
def contact(request):
    serializer = ContactMessageSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    # Save first so the message is never lost even if both emails below
    # fail to send -- mirrors the try/except-log-swallow pattern already
    # used for password-reset/verification emails elsewhere in this repo.
    contact_message = serializer.save(user=request.user)

    locale = request.data.get('locale')
    locale = locale if locale in ('en', 'pt-BR') else 'en'

    try:
        send_contact_confirmation_email(contact_message, locale=locale)
    except requests.RequestException:
        logger.exception('Failed to send contact confirmation email for message %s', contact_message.id)

    try:
        send_contact_notification_email(contact_message)
    except requests.RequestException:
        logger.exception('Failed to send contact notification email for message %s', contact_message.id)

    return Response({'detail': "Thanks for reaching out. We'll get back to you soon."})
