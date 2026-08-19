from typing import Optional

from django.conf import settings

from .base import PaymentProvider


def get_provider(name: Optional[str] = None) -> PaymentProvider:
    name = name or settings.PAYMENT_PROVIDER
    if name == 'stripe':
        from .stripe_provider import StripeProvider
        return StripeProvider()
    raise ValueError(f'Unknown payment provider: {name!r}')
