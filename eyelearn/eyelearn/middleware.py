import secrets

from django.conf import settings
from django.http import JsonResponse

# Stripe calls this path directly and can't know our shared secret -- it's
# already gated by signature verification in billing/providers/stripe_provider.py.
_EXEMPT_PREFIXES = ('/api/billing/webhook/',)


class InternalApiKeyMiddleware:
    """Rejects any request that doesn't carry the shared secret the Next.js
    BFF (src/lib/api/django-client.ts) attaches to every server-to-server
    call, before any URL resolution/DRF auth/permission logic runs.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(_EXEMPT_PREFIXES):
            return self.get_response(request)

        expected = settings.INTERNAL_API_KEY
        provided = request.headers.get('X-Internal-Api-Key', '')
        if not expected or not secrets.compare_digest(provided, expected):
            return JsonResponse({'detail': 'Forbidden.'}, status=403)

        return self.get_response(request)
