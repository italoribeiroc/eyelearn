from urllib.parse import urlparse

from django.conf import settings
from rest_framework import serializers

from .models import Subscription


def _validate_allowed_origin(url):
    origin = f'{urlparse(url).scheme}://{urlparse(url).netloc}'
    if origin not in settings.FRONTEND_ALLOWED_ORIGINS:
        raise serializers.ValidationError(f'{origin} is not an allowed redirect origin.')
    return url


class CheckoutSessionRequestSerializer(serializers.Serializer):
    plan = serializers.ChoiceField(choices=Subscription.Plan.choices)
    currency = serializers.ChoiceField(choices=['usd', 'brl'])
    success_url = serializers.URLField(validators=[_validate_allowed_origin])
    cancel_url = serializers.URLField(validators=[_validate_allowed_origin])


class PortalSessionRequestSerializer(serializers.Serializer):
    return_url = serializers.URLField(validators=[_validate_allowed_origin])
    change_plan = serializers.BooleanField(required=False, default=False)
