"""The only file in this app that imports `groq`. Second fallback provider
(tried after Claude, then Gemini both report themselves unavailable) -- see
ai_providers/__init__.py's fallback order.

Groq has no equivalent to Claude's `messages.parse(output_format=...)` or
Gemini's `response_schema=`: its "json_schema" structured-output response
format is documented as "only available on certain models" (verified against
the installed `groq` SDK's real type hints, not guessed), so rather than
gamble on a specific model's support, this uses the universally supported
"json_object" mode (plain JSON mode) plus an explicit schema instruction
appended to the system prompt, and validates the result with Pydantic --
the same "trust but verify" fallback path GeminiProvider already uses for
its `.parsed is None` case.

MODEL: verified live against a real GROQ_API_KEY, not just the SDK's type
hints -- 'llama-3.3-70b-versatile' (the model the type hints suggested first)
404'd as decommissioned/inaccessible on this key, along with most other
listed models; 'openai/gpt-oss-120b' was the one confirmed actually
reachable and tested end-to-end producing valid structured JSON. Groq's
available model lineup changes over time, so if this ever 404s again, re-run
the same live probe rather than guessing from the SDK's type hints alone.
"""

import json

import groq
from django.conf import settings
from pydantic import ValidationError

from .base import AiGenerationError, AiProvider, ProviderUnavailableError

MODEL = 'openai/gpt-oss-120b'
# gpt-oss models spend some of their completion budget on internal
# reasoning before emitting the visible JSON answer -- a low cap here (seen
# live: 5 tokens produced an empty response) silently truncates to nothing,
# which would otherwise look like a valid-but-empty response rather than an
# error. Matches Claude's MAX_TOKENS.
MAX_COMPLETION_TOKENS = 4096

# 429 rate limit, 500/502/503 server-side issues -- same "try the next
# provider" signal as Claude/Gemini's equivalent status codes.
_UNAVAILABLE_STATUS_CODES = {429, 500, 502, 503}


def _error_message(exc):
    body = getattr(exc, 'body', None)
    if isinstance(body, dict):
        return str(body.get('error', {}).get('message') or body.get('message') or exc)
    return str(exc)


class GroqProvider(AiProvider):
    def name(self):
        return 'groq'

    def generate(self, *, system, user_content, response_model):
        if not settings.GROQ_API_KEY:
            raise ProviderUnavailableError('GROQ_API_KEY is not configured.')

        # json_object mode won't enforce a shape by itself (unlike Claude's
        # output_format or Gemini's response_schema), and Groq requires the
        # word "JSON" to appear in the prompt for this mode at all -- so the
        # schema is spelled out explicitly here, provider-specific glue that
        # has no business living in the shared, provider-agnostic prompt
        # builder in ai_generation.py.
        schema_system = (
            f'{system}\n\nRespond with a single JSON object only (no prose, no markdown fences), '
            f'matching this JSON schema exactly: {json.dumps(response_model.model_json_schema())}'
        )

        try:
            response = groq.Groq(api_key=settings.GROQ_API_KEY).chat.completions.create(
                model=MODEL,
                messages=[
                    {'role': 'system', 'content': schema_system},
                    {'role': 'user', 'content': user_content},
                ],
                response_format={'type': 'json_object'},
                max_completion_tokens=MAX_COMPLETION_TOKENS,
            )
        except groq.RateLimitError as exc:
            raise ProviderUnavailableError('Groq rate limit exceeded.') from exc
        except groq.APIStatusError as exc:
            message = _error_message(exc)
            if exc.status_code in _UNAVAILABLE_STATUS_CODES:
                raise ProviderUnavailableError(f'Groq unavailable ({exc.status_code}): {message}') from exc
            raise AiGenerationError(f'Groq API error ({exc.status_code}): {message}') from exc
        except groq.APIConnectionError as exc:
            raise ProviderUnavailableError('Could not reach Groq.') from exc

        content = response.choices[0].message.content
        if not content:
            raise AiGenerationError('Groq did not return any content.')
        try:
            return response_model.model_validate_json(content)
        except ValidationError as exc:
            raise AiGenerationError(f'Groq response failed schema validation: {exc}') from exc
