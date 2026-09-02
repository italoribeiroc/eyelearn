"""The only file in this app that imports `anthropic`, mirroring
billing/providers/stripe_provider.py's "one file owns the SDK" rule."""

import anthropic
from django.conf import settings
from pydantic import ValidationError

from .base import AiGenerationError, AiProvider, ProviderUnavailableError

MODEL = 'claude-sonnet-5'
MAX_TOKENS = 4096

# Anthropic status codes that mean "temporarily can't serve this" rather
# than "this request is wrong" -- worth falling back to the next provider
# for. 429 rate limit, 500/502/503 server-side issues, 529 = overloaded.
_UNAVAILABLE_STATUS_CODES = {429, 500, 502, 503, 529}

# Anthropic reports an exhausted/insufficient credit balance as a plain 400
# invalid_request_error (not 402/429), indistinguishable from a genuine bad
# request by status code alone -- this is a resource constraint, not a
# malformed request, so it's detected by message content instead and still
# triggers the Gemini fallback.
_INSUFFICIENT_CREDIT_MARKERS = ('credit balance', 'insufficient credit')


def _error_message(exc):
    body = getattr(exc, 'body', None)
    if isinstance(body, dict):
        return str(body.get('error', {}).get('message') or body.get('message') or exc)
    return str(exc)


class ClaudeProvider(AiProvider):
    def name(self):
        return 'claude'

    def _client(self):
        return anthropic.Anthropic(api_key=settings.CLAUDE_API_KEY)

    def generate(self, *, system, user_content, response_model):
        if not settings.CLAUDE_API_KEY:
            raise ProviderUnavailableError('CLAUDE_API_KEY is not configured.')

        try:
            response = self._client().messages.parse(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{'role': 'user', 'content': user_content}],
                output_format=response_model,
            )
        except anthropic.RateLimitError as exc:
            raise ProviderUnavailableError('Claude rate limit exceeded.') from exc
        except anthropic.APIStatusError as exc:
            message = _error_message(exc)
            if exc.status_code in _UNAVAILABLE_STATUS_CODES or any(
                marker in message.lower() for marker in _INSUFFICIENT_CREDIT_MARKERS
            ):
                raise ProviderUnavailableError(f'Claude unavailable ({exc.status_code}): {message}') from exc
            raise AiGenerationError(f'Claude API error ({exc.status_code}): {message}') from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailableError('Could not reach Claude.') from exc
        except ValidationError as exc:
            raise AiGenerationError(f'Claude response failed schema validation: {exc}') from exc

        parsed = response.parsed_output
        if parsed is None:
            raise AiGenerationError('Claude did not return a structured response.')
        return parsed
