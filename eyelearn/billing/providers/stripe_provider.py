from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Mapping, Optional

import stripe
from django.conf import settings

from .base import (
    CheckoutSession,
    CustomerNotFound,
    InvalidWebhookSignature,
    NormalizedEvent,
    NormalizedEventType,
    PaymentProvider,
    PortalSession,
)

_STRIPE_STATUS_MAP = {
    'incomplete': 'incomplete',
    'incomplete_expired': 'incomplete_expired',
    'trialing': 'trialing',
    'active': 'active',
    'past_due': 'past_due',
    'canceled': 'canceled',
    'unpaid': 'unpaid',
    'paused': 'paused',
}


class StripeProvider(PaymentProvider):
    """The only file in `billing` that imports the `stripe` SDK.

    Everything Stripe-specific -- API calls, price-ID mapping, webhook
    signature verification, status vocabulary -- lives here and nowhere
    else, so BillingService and the views never need to change if a
    payment provider is added or swapped later.
    """

    def __init__(self):
        stripe.api_key = settings.STRIPE_SECRET_KEY

    @property
    def name(self) -> str:
        return 'stripe'

    def _price_id_map(self) -> dict:
        return {
            ('monthly', 'usd'): settings.STRIPE_PRICE_ID_MONTHLY_USD,
            ('monthly', 'brl'): settings.STRIPE_PRICE_ID_MONTHLY_BRL,
            ('annual', 'usd'): settings.STRIPE_PRICE_ID_ANNUAL_USD,
            ('annual', 'brl'): settings.STRIPE_PRICE_ID_ANNUAL_BRL,
        }

    def _reverse_price_id_map(self) -> dict:
        return {price_id: key for key, price_id in self._price_id_map().items()}

    def create_customer(self, *, email: str, user_id: int) -> str:
        customer = stripe.Customer.create(email=email, metadata={'user_id': user_id})
        return customer.id

    def create_checkout_session(
        self, *, customer_ref: str, plan: str, currency: str,
        success_url: str, cancel_url: str, user_id: int,
    ) -> CheckoutSession:
        price_id = self._price_id_map().get((plan, currency))
        if not price_id:
            raise ValueError(f'No Stripe price configured for plan={plan!r} currency={currency!r}.')

        with _translate_missing_customer():
            session = stripe.checkout.Session.create(
                mode='subscription',
                customer=customer_ref,
                line_items=[{'price': price_id, 'quantity': 1}],
                success_url=success_url,
                cancel_url=cancel_url,
                client_reference_id=str(user_id),
                metadata={'user_id': user_id},
            )
        return CheckoutSession(url=session.url)

    def create_portal_session(
        self, *, customer_ref: str, return_url: str, subscription_ref: Optional[str] = None,
    ) -> PortalSession:
        kwargs = {'customer': customer_ref, 'return_url': return_url}
        if subscription_ref:
            # Deep-links straight to the plan-picker step instead of the
            # general billing menu. Requires "Customers can switch plans"
            # enabled in the Stripe Dashboard's Portal configuration.
            kwargs['flow_data'] = {
                'type': 'subscription_update',
                'subscription_update': {'subscription': subscription_ref},
            }

        with _translate_missing_customer():
            session = stripe.billing_portal.Session.create(**kwargs)
        return PortalSession(url=session.url)

    def parse_webhook_event(self, *, payload: bytes, headers: Mapping[str, str]) -> NormalizedEvent:
        signature = headers.get('Stripe-Signature') or headers.get('stripe-signature')
        try:
            event = stripe.Webhook.construct_event(
                payload=payload, sig_header=signature, secret=settings.STRIPE_WEBHOOK_SECRET,
            )
        except (ValueError, stripe.error.SignatureVerificationError) as exc:
            raise InvalidWebhookSignature(str(exc)) from exc

        handler_name = f'_normalize_{event.type.replace(".", "_")}'
        handler = getattr(self, handler_name, None)
        if handler is None:
            return NormalizedEvent(provider_event_id=event.id, type=NormalizedEventType.IGNORED)
        return handler(event)

    def _normalize_checkout_session_completed(self, event) -> NormalizedEvent:
        session = event.data.object
        if session['mode'] != 'subscription':
            return NormalizedEvent(provider_event_id=event.id, type=NormalizedEventType.IGNORED)

        subscription = stripe.Subscription.retrieve(session['subscription'])
        item = subscription['items']['data'][0]
        price_id = item['price']['id']
        plan, currency = self._reverse_price_id_map().get(price_id, (None, None))
        user_id = session['client_reference_id'] or session['metadata'].get('user_id')

        return NormalizedEvent(
            provider_event_id=event.id,
            type=NormalizedEventType.CHECKOUT_COMPLETED,
            customer_ref=session['customer'],
            subscription_ref=subscription['id'],
            price_ref=price_id,
            plan=plan,
            currency=currency,
            status=_STRIPE_STATUS_MAP.get(subscription['status'], subscription['status']),
            current_period_end=_to_datetime(item['current_period_end']),
            cancel_at_period_end=_is_canceling(subscription),
            user_id=int(user_id) if user_id else None,
        )

    def _normalize_customer_subscription_updated(self, event) -> NormalizedEvent:
        subscription = event.data.object
        item = subscription['items']['data'][0]
        price_id = item['price']['id']
        plan, currency = self._reverse_price_id_map().get(price_id, (None, None))

        return NormalizedEvent(
            provider_event_id=event.id,
            type=NormalizedEventType.SUBSCRIPTION_UPDATED,
            customer_ref=subscription['customer'],
            subscription_ref=subscription['id'],
            price_ref=price_id,
            plan=plan,
            currency=currency,
            status=_STRIPE_STATUS_MAP.get(subscription['status'], subscription['status']),
            current_period_end=_to_datetime(item['current_period_end']),
            cancel_at_period_end=_is_canceling(subscription),
        )

    def _normalize_customer_subscription_deleted(self, event) -> NormalizedEvent:
        subscription = event.data.object
        return NormalizedEvent(
            provider_event_id=event.id,
            type=NormalizedEventType.SUBSCRIPTION_CANCELED,
            customer_ref=subscription['customer'],
            subscription_ref=subscription['id'],
        )

    def _normalize_invoice_payment_failed(self, event) -> NormalizedEvent:
        invoice = event.data.object
        return NormalizedEvent(
            provider_event_id=event.id,
            type=NormalizedEventType.PAYMENT_FAILED,
            customer_ref=invoice['customer'],
            subscription_ref=invoice['subscription'],
        )


def _to_datetime(unix_timestamp):
    if unix_timestamp is None:
        return None
    return datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)


@contextmanager
def _translate_missing_customer():
    """Turns Stripe's "the customer doesn't exist" error into CustomerNotFound.

    Local PaymentCustomer rows are never revalidated against Stripe before
    use (see BillingService), so a customer deleted/reset on Stripe's side
    (e.g. clearing test-mode data) surfaces here first. Stripe reports this
    as an InvalidRequestError with code='resource_missing', param='customer'.
    """
    try:
        yield
    except stripe.error.InvalidRequestError as exc:
        if getattr(exc, 'code', None) == 'resource_missing' and getattr(exc, 'param', None) == 'customer':
            raise CustomerNotFound(str(exc)) from exc
        raise


def _is_canceling(subscription) -> bool:
    """Whether the subscription is scheduled to end rather than renew.

    Newer Stripe API versions represent "cancel at period end" via a
    `cancel_at` timestamp rather than the older `cancel_at_period_end`
    boolean, which stays false even when a cancellation is scheduled. Check
    both so this keeps working regardless of which the account's pinned
    API version emits.
    """
    return bool(subscription['cancel_at_period_end']) or subscription['cancel_at'] is not None
