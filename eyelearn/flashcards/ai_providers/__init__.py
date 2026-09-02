import logging
from contextvars import ContextVar

from .base import AiGenerationError, ProviderUnavailableError
from .claude_provider import ClaudeProvider
from .gemini_provider import GeminiProvider
from .groq_provider import GroqProvider

logger = logging.getLogger(__name__)

# Claude first, then Gemini, then Groq -- see module docstrings on each
# provider and ProviderUnavailableError for exactly what triggers a fallback
# (rate limits/overload/unreachable/not-configured, never a content/
# validation failure, which would likely fail identically on the next
# provider too). Groq is last: it's the only one without native
# structured-output enforcement (see groq_provider.py), so it's the least
# reliable of the three when it does have to run.
_PROVIDERS = [ClaudeProvider(), GeminiProvider(), GroqProvider()]

# Records which provider actually served the most recent successful
# generate_with_fallback() call, so a view can report it (e.g. as a
# response header, for developers to check which provider is live without
# it ever being surfaced to the user) without threading a return value
# through every layer in between (services.py, ai_generation.py). A
# ContextVar rather than a module global so it can't leak across concurrent
# requests under async/greenlet execution; read it with
# pop_last_provider_used(), which also clears it -- see there for why
# popping (not just reading) matters even under Django's normal synchronous,
# one-request-at-a-time-per-thread execution.
_last_provider_used = ContextVar('_last_provider_used', default=None)


def pop_last_provider_used():
    """Returns and clears the provider name ('claude'/'gemini'/'groq') that
    served the most recent successful generate_with_fallback() call in this
    request, or None if none has run yet. Popping instead of just reading
    means a request whose code path never actually calls an AI provider can
    never accidentally report a stale value left over from something else
    that happened to run earlier on the same thread."""
    value = _last_provider_used.get()
    _last_provider_used.set(None)
    return value


def generate_with_fallback(*, system, user_content, response_model):
    last_error = None
    for provider in _PROVIDERS:
        try:
            result = provider.generate(system=system, user_content=user_content, response_model=response_model)
        except ProviderUnavailableError as exc:
            logger.warning('AI provider %s unavailable, trying next: %s', provider.name(), exc)
            last_error = exc
            continue
        logger.info('AI provider %s served this generation.', provider.name())
        _last_provider_used.set(provider.name())
        return result

    raise AiGenerationError(f'All AI providers are currently unavailable: {last_error}')
