from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional

from django.contrib.auth import get_user_model
from django.db import transaction

from .models import PaymentCustomer, ProcessedWebhookEvent, Subscription, get_active_subscription
from .providers import get_provider
from .providers.base import CustomerNotFound, NormalizedEvent, NormalizedEventType, PaymentProvider


class AlreadySubscribedError(Exception):
    """Raised when a user with an active subscription attempts to start another checkout."""


class NoPaymentCustomerError(Exception):
    """Raised when a portal session is requested for a user with no billing history."""


@dataclass(frozen=True)
class SubscriptionStatusDTO:
    plan: str
    status: Optional[str]
    current_period_end: Optional[datetime]
    cancel_at_period_end: bool


class BillingService:
    """Provider-agnostic billing logic. Views call this; it never imports a specific SDK.

    All provider interaction goes through the PaymentProvider interface
    (`providers/base.py`), so swapping payment providers means adding a new
    connector and changing PAYMENT_PROVIDER, not touching this file.
    """

    def __init__(self, provider: Optional[PaymentProvider] = None):
        self.provider = provider or get_provider()

    def start_checkout(self, *, user, plan: str, currency: str, success_url: str, cancel_url: str) -> str:
        if get_active_subscription(user) is not None:
            raise AlreadySubscribedError('User already has an active subscription.')

        customer = self._get_or_create_customer(user)
        try:
            session = self.provider.create_checkout_session(
                customer_ref=customer.provider_customer_id,
                plan=plan,
                currency=currency,
                success_url=success_url,
                cancel_url=cancel_url,
                user_id=user.id,
            )
        except CustomerNotFound:
            customer = self._recreate_customer(user, customer)
            session = self.provider.create_checkout_session(
                customer_ref=customer.provider_customer_id,
                plan=plan,
                currency=currency,
                success_url=success_url,
                cancel_url=cancel_url,
                user_id=user.id,
            )
        return session.url

    def open_portal(self, *, user, return_url: str, change_plan: bool = False) -> str:
        try:
            customer = PaymentCustomer.objects.get(user=user, provider=self.provider.name)
        except PaymentCustomer.DoesNotExist:
            raise NoPaymentCustomerError('User has no billing customer yet.')

        subscription_ref = None
        if change_plan:
            active_subscription = get_active_subscription(user)
            if active_subscription:
                subscription_ref = active_subscription.provider_subscription_id

        try:
            session = self.provider.create_portal_session(
                customer_ref=customer.provider_customer_id,
                return_url=return_url,
                subscription_ref=subscription_ref,
            )
        except CustomerNotFound:
            customer = self._recreate_customer(user, customer)
            session = self.provider.create_portal_session(
                customer_ref=customer.provider_customer_id,
                return_url=return_url,
                subscription_ref=subscription_ref,
            )
        return session.url

    def get_status(self, *, user) -> SubscriptionStatusDTO:
        subscription = get_active_subscription(user)
        if subscription is None:
            return SubscriptionStatusDTO(
                plan='free', status=None, current_period_end=None, cancel_at_period_end=False,
            )

        return SubscriptionStatusDTO(
            plan=subscription.plan,
            status=subscription.status,
            current_period_end=subscription.current_period_end,
            cancel_at_period_end=subscription.cancel_at_period_end,
        )

    def handle_webhook(self, *, payload: bytes, headers: Mapping[str, str], provider_name: str) -> None:
        provider = get_provider(provider_name)
        event = provider.parse_webhook_event(payload=payload, headers=headers)

        if event.type == NormalizedEventType.IGNORED:
            return

        _, created = ProcessedWebhookEvent.objects.get_or_create(
            provider=provider_name,
            provider_event_id=event.provider_event_id,
            defaults={'event_type': event.type.value},
        )
        if not created:
            return

        with transaction.atomic():
            self._apply_event(provider_name, event)

    def _get_or_create_customer(self, user) -> PaymentCustomer:
        try:
            return PaymentCustomer.objects.get(user=user, provider=self.provider.name)
        except PaymentCustomer.DoesNotExist:
            provider_customer_id = self.provider.create_customer(email=user.email, user_id=user.id)
            return PaymentCustomer.objects.create(
                user=user, provider=self.provider.name, provider_customer_id=provider_customer_id,
            )

    def _recreate_customer(self, user, stale_customer: PaymentCustomer) -> PaymentCustomer:
        """Replaces a PaymentCustomer's provider_customer_id after the provider reports it missing.

        Keeps the same row (and therefore any Subscription rows FK'd to it)
        rather than deleting and recreating, since only the external
        reference is stale.
        """
        provider_customer_id = self.provider.create_customer(email=user.email, user_id=user.id)
        stale_customer.provider_customer_id = provider_customer_id
        stale_customer.save(update_fields=['provider_customer_id'])
        return stale_customer

    def _apply_event(self, provider_name: str, event: NormalizedEvent) -> None:
        handlers = {
            NormalizedEventType.CHECKOUT_COMPLETED: self._apply_checkout_completed,
            NormalizedEventType.SUBSCRIPTION_UPDATED: self._apply_subscription_updated,
            NormalizedEventType.SUBSCRIPTION_CANCELED: self._apply_subscription_canceled,
            NormalizedEventType.PAYMENT_FAILED: self._apply_payment_failed,
        }
        handlers[event.type](provider_name, event)

    def _apply_checkout_completed(self, provider_name: str, event: NormalizedEvent) -> None:
        User = get_user_model()
        customer, _ = PaymentCustomer.objects.get_or_create(
            provider=provider_name,
            provider_customer_id=event.customer_ref,
            defaults={'user': User.objects.get(id=event.user_id)},
        )

        Subscription.objects.update_or_create(
            provider=provider_name,
            provider_subscription_id=event.subscription_ref,
            defaults={
                'customer': customer,
                'provider_price_id': event.price_ref,
                'plan': event.plan,
                'currency': event.currency,
                'status': event.status,
                'current_period_end': event.current_period_end,
                'cancel_at_period_end': event.cancel_at_period_end,
            },
        )

    def _apply_subscription_updated(self, provider_name: str, event: NormalizedEvent) -> None:
        updates = {
            'status': event.status,
            'current_period_end': event.current_period_end,
            'cancel_at_period_end': event.cancel_at_period_end,
        }
        if event.plan:
            updates.update(plan=event.plan, currency=event.currency, provider_price_id=event.price_ref)

        Subscription.objects.filter(
            provider=provider_name, provider_subscription_id=event.subscription_ref,
        ).update(**updates)

    def _apply_subscription_canceled(self, provider_name: str, event: NormalizedEvent) -> None:
        Subscription.objects.filter(
            provider=provider_name, provider_subscription_id=event.subscription_ref,
        ).update(status=Subscription.Status.CANCELED)

    def _apply_payment_failed(self, provider_name: str, event: NormalizedEvent) -> None:
        Subscription.objects.filter(
            provider=provider_name, provider_subscription_id=event.subscription_ref,
        ).update(status=Subscription.Status.PAST_DUE)
