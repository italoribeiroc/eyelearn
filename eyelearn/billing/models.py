from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


class PaymentCustomer(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='billing_customer',
    )
    provider = models.CharField(max_length=20, default='stripe')
    provider_customer_id = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_customer_id'], name='unique_provider_customer',
            ),
        ]

    def __str__(self):
        return f'{self.provider}:{self.provider_customer_id}'


class Subscription(models.Model):
    class Plan(models.TextChoices):
        MONTHLY = 'monthly', 'Monthly'
        ANNUAL = 'annual', 'Annual'

    class Status(models.TextChoices):
        INCOMPLETE = 'incomplete', 'Incomplete'
        TRIALING = 'trialing', 'Trialing'
        ACTIVE = 'active', 'Active'
        PAST_DUE = 'past_due', 'Past due'
        CANCELED = 'canceled', 'Canceled'
        UNPAID = 'unpaid', 'Unpaid'
        PAUSED = 'paused', 'Paused'

    ACTIVE_STATUSES = (Status.ACTIVE, Status.TRIALING, Status.PAST_DUE)

    customer = models.ForeignKey(PaymentCustomer, on_delete=models.CASCADE, related_name='subscriptions')
    provider = models.CharField(max_length=20, default='stripe')
    provider_subscription_id = models.CharField(max_length=255)
    provider_price_id = models.CharField(max_length=255)
    plan = models.CharField(max_length=20, choices=Plan.choices)
    currency = models.CharField(max_length=3)
    status = models.CharField(max_length=32, choices=Status.choices)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_subscription_id'], name='unique_provider_subscription',
            ),
        ]

    def __str__(self):
        return f'{self.provider}:{self.provider_subscription_id} ({self.status})'


def get_active_subscription(user):
    return (
        Subscription.objects
        .filter(customer__user=user, status__in=Subscription.ACTIVE_STATUSES)
        .order_by('-created_at')
        .first()
    )


# Universal 7-day right-of-withdrawal refund window, applied to every
# customer regardless of jurisdiction (CDC Art. 49-inspired -- Brazil's
# consumer protection law grants this specifically, but being more
# generous everywhere is simpler than branching on currency/locale and is
# never a compliance problem).
WITHDRAWAL_WINDOW_DAYS = 7


def is_within_withdrawal_window(subscription) -> bool:
    """Whether `subscription` is still within the 7-day refund window,
    measured from when the local Subscription row was first created (set
    by _apply_checkout_completed's update_or_create, moments after the
    checkout.session.completed webhook fires) -- effectively the same
    moment as Stripe's own subscription.created, without a second Stripe
    round-trip just to check eligibility."""
    return timezone.now() - subscription.created_at <= timedelta(days=WITHDRAWAL_WINDOW_DAYS)


class ProcessedWebhookEvent(models.Model):
    """Idempotency guard: providers deliver webhooks at-least-once."""

    provider = models.CharField(max_length=20, default='stripe')
    provider_event_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['provider', 'provider_event_id'], name='unique_provider_event',
            ),
        ]

    def __str__(self):
        return f'{self.provider}:{self.provider_event_id}'
