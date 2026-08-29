from unittest import mock

from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings
from rest_framework.throttling import SimpleRateThrottle

# DRF's throttle classes read DEFAULT_THROTTLE_CLASSES/DEFAULT_THROTTLE_RATES
# once at import time (module-level class attributes), so override_settings
# on REST_FRAMEWORK in a test doesn't reach already-imported throttle
# classes. Patching allow_request directly is the reliable way to disable
# throttling for the whole test run (deterministic, order-independent).
_throttle_patcher = mock.patch.object(SimpleRateThrottle, 'allow_request', return_value=True)
_throttle_patcher.start()


class InternalApiClient(Client):
    """Test client that auto-attaches the same shared secret header
    django-client.ts sends on every real request, so existing view tests
    keep exercising the normal (authorized) request path."""

    def generic(self, method, path, data='', content_type='application/octet-stream', secure=False, **extra):
        extra.setdefault('HTTP_X_INTERNAL_API_KEY', settings.INTERNAL_API_KEY)
        return super().generic(method, path, data, content_type, secure, **extra)


@override_settings(INTERNAL_API_KEY='test-internal-api-key')
class ApiTestCase(TestCase):
    """Base for DB-backed view tests -- supplies the internal API key."""
    client_class = InternalApiClient


@override_settings(INTERNAL_API_KEY='test-internal-api-key')
class ApiSimpleTestCase(SimpleTestCase):
    client_class = InternalApiClient
