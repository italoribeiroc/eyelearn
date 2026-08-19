from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .providers.base import InvalidWebhookSignature
from .serializers import CheckoutSessionRequestSerializer, PortalSessionRequestSerializer
from .services import AlreadySubscribedError, BillingService, NoPaymentCustomerError


@api_view(['POST'])
@permission_classes([IsAuthenticated])
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
