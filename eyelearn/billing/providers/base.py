from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Optional


class InvalidWebhookSignature(Exception):
    """Raised when a webhook payload fails provider signature verification."""


class CustomerNotFound(Exception):
    """Raised when a stored customer_ref no longer exists with the provider.

    Providers deliberately don't validate customer_ref before every call --
    this is raised lazily by create_checkout_session/create_portal_session
    when the provider rejects it, so BillingService can recreate the
    customer and retry once instead of leaving the user stuck.
    """


@dataclass(frozen=True)
class CheckoutSession:
    url: str


@dataclass(frozen=True)
class PortalSession:
    url: str


class NormalizedEventType(str, Enum):
    CHECKOUT_COMPLETED = 'checkout_completed'
    SUBSCRIPTION_UPDATED = 'subscription_updated'
    SUBSCRIPTION_CANCELED = 'subscription_canceled'
    PAYMENT_FAILED = 'payment_failed'
    IGNORED = 'ignored'


@dataclass(frozen=True)
class NormalizedEvent:
    """Provider-agnostic shape a webhook payload is translated into.

    Every PaymentProvider implementation is responsible for mapping its own
    vocabulary (event types, status strings, price identifiers) onto this
    shape. Callers (BillingService) never see provider-specific data.
    """

    provider_event_id: str
    type: NormalizedEventType
    customer_ref: Optional[str] = None
    subscription_ref: Optional[str] = None
    price_ref: Optional[str] = None
    plan: Optional[str] = None
    currency: Optional[str] = None
    status: Optional[str] = None
    current_period_end: Optional[datetime] = None
    cancel_at_period_end: bool = False
    user_id: Optional[int] = None


class PaymentProvider(ABC):
    """Interface every payment connector (Stripe, and future providers) must implement.

    BillingService talks to providers only through this interface and the
    normalized dataclasses above, so swapping the provider never requires
    changing business logic, views, or models.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short slug identifying this provider, e.g. 'stripe'. Stored on every DB row it writes."""

    @abstractmethod
    def create_customer(self, *, email: str, user_id: int) -> str:
        """Create a customer with the provider and return its provider-side reference."""

    @abstractmethod
    def create_checkout_session(
        self, *, customer_ref: str, plan: str, currency: str,
        success_url: str, cancel_url: str, user_id: int,
    ) -> CheckoutSession:
        """Create a hosted checkout session for a subscription purchase.

        Raises CustomerNotFound if customer_ref no longer exists with the provider.
        """

    @abstractmethod
    def create_portal_session(
        self, *, customer_ref: str, return_url: str, subscription_ref: Optional[str] = None,
    ) -> PortalSession:
        """Create a hosted self-serve billing management session.

        If subscription_ref is given, deep-links directly to that
        subscription's plan-change screen instead of the general billing menu.
        Raises CustomerNotFound if customer_ref no longer exists with the provider.
        """

    @abstractmethod
    def parse_webhook_event(self, *, payload: bytes, headers: Mapping[str, str]) -> NormalizedEvent:
        """Verify and translate a raw webhook payload into a NormalizedEvent.

        Raises InvalidWebhookSignature if the payload fails verification.
        """
