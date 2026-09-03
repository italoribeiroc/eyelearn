import logging

from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from .providers.base import InvalidWebhookSignature, RefundTargetNotFoundError
from .serializers import CheckoutSessionRequestSerializer, PortalSessionRequestSerializer
from .services import (
    AlreadySubscribedError,
    BillingService,
    NoActiveSubscriptionError,
    NoPaymentCustomerError,
    RefundWindowExpiredError,
)

logger = logging.getLogger(__name__)


class CheckoutSessionRateThrottle(UserRateThrottle):
    scope = 'checkout_session'


class CancelWithRefundRateThrottle(UserRateThrottle):
    scope = 'cancel_with_refund'


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([CheckoutSessionRateThrottle])
def create_checkout_session(request):
    serializer = CheckoutSessionRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        checkout_url = BillingService().start_checkout(
            user=request.user,
            plan=serializer.validated_data['plan'],
            currency=serializer.validated_data['currency'],
            success_url=serializer.validated_data['success_url'],
            cancel_url=serializer.validated_data['cancel_url'],
        )
    except AlreadySubscribedError:
        return Response(
            {'detail': 'You already have an active subscription.'}, status=status.HTTP_409_CONFLICT,
        )

    return Response({'checkout_url': checkout_url})


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def create_portal_session(request):
    serializer = PortalSessionRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    try:
        portal_url = BillingService().open_portal(
            user=request.user,
            return_url=serializer.validated_data['return_url'],
            change_plan=serializer.validated_data['change_plan'],
        )
    except NoPaymentCustomerError:
        return Response({'detail': 'No billing account found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response({'portal_url': portal_url})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def subscription_status(request):
    result = BillingService().get_status(user=request.user)
    return Response({
        'plan': result.plan,
        'status': result.status,
        'current_period_end': result.current_period_end,
        'cancel_at_period_end': result.cancel_at_period_end,
        'refund_eligible_until': result.refund_eligible_until,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([CancelWithRefundRateThrottle])
def cancel_with_refund(request):
    try:
        subscription = BillingService().cancel_with_refund(user=request.user)
    except NoActiveSubscriptionError:
        return Response({'detail': 'No active subscription found.'}, status=status.HTTP_404_NOT_FOUND)
    except RefundWindowExpiredError:
        return Response(
            {'detail': 'The 7-day refund window has passed.'}, status=status.HTTP_400_BAD_REQUEST,
        )
    except RefundTargetNotFoundError:
        return Response(
            {'detail': 'No refundable payment was found for this subscription.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    except Exception:
        # Any other provider/Stripe failure -- matches the existing
        # "upstream provider failure -> 502" convention used elsewhere
        # (e.g. AI flashcard generation).
        logger.exception('Failed to refund and cancel subscription for user %s', request.user.id)
        return Response(
            {'detail': 'Failed to process the refund. Please try again or contact support.'},
            status=status.HTTP_502_BAD_GATEWAY,
        )

    return Response({
        'plan': subscription.plan,
        'status': subscription.status,
        'current_period_end': subscription.current_period_end,
        'cancel_at_period_end': subscription.cancel_at_period_end,
        'refund_eligible_until': None,
    })


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([AllowAny])
def webhook(request, provider):
    # Must read request.body (raw bytes) directly -- DRF's request.data parsing
    # would consume the stream before signature verification can see the raw payload.
    try:
        BillingService().handle_webhook(
            payload=request.body, headers=request.headers, provider_name=provider,
        )
    except InvalidWebhookSignature:
        return Response(status=status.HTTP_400_BAD_REQUEST)
    except ValueError:
        return Response(status=status.HTTP_404_NOT_FOUND)

    return Response(status=status.HTTP_200_OK)
