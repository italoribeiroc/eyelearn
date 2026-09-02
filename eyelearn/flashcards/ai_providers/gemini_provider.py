"""The only file in this app that imports `google.genai`. Fallback provider,
used only when Claude reports itself unavailable (see ClaudeProvider /
ai_providers/__init__.py's fallback order).

NOTE: model name (MODEL below) is set from Google's published SDK docs as of
this writing, not verified against a live API key -- confirm/adjust once a
real GEMINI_API_KEY is available.
"""

from django.conf import settings
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import ValidationError

from .base import AiGenerationError, AiProvider, ProviderUnavailableError

MODEL = 'gemini-3.5-flash'

# 429 rate limit, 500/503 server-side issues -- same "try the next
# provider" signal as Claude's equivalent status codes.
_UNAVAILABLE_CODES = {429, 500, 503}


class GeminiProvider(AiProvider):
    def name(self):
        return 'gemini'

    def generate(self, *, system, user_content, response_model):
        if not settings.GEMINI_API_KEY:
            raise ProviderUnavailableError('GEMINI_API_KEY is not configured.')

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        try:
            response = client.models.generate_content(
                model=MODEL,
                contents=user_content,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type='application/json',
                    response_schema=response_model,
                ),
            )
        except genai_errors.APIError as exc:
            if exc.code in _UNAVAILABLE_CODES:
                raise ProviderUnavailableError(f'Gemini unavailable ({exc.code}).') from exc
            raise AiGenerationError(f'Gemini API error ({exc.code}).') from exc
        except Exception as exc:
            # Network/connection failures from the underlying HTTP client
            # don't raise genai_errors.APIError -- treat any other
            # exception here as "couldn't reach Gemini", not a hard failure.
            raise ProviderUnavailableError(f'Could not reach Gemini: {exc}') from exc

        parsed = getattr(response, 'parsed', None)
        if parsed is not None:
            return parsed
        try:
            return response_model.model_validate_json(response.text)
        except ValidationError as exc:
            raise AiGenerationError(f'Gemini response failed schema validation: {exc}') from exc
