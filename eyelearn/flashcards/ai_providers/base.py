"""Provider-agnostic interface + shared response schemas for AI flashcard
generation. Mirrors billing/providers/base.py's shape (an ABC + normalized
exceptions), which this app's own CLAUDE.md ties specifically to "genuine
polymorphism across differently-behaved providers reachable via fallback" --
that now applies here too (Claude, with a Gemini fallback), unlike the
single-vendor storage.py case.
"""

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field, model_validator


class AiGenerationError(Exception):
    """Raised for any terminal AI generation failure: every configured
    provider was unavailable, or a provider returned a response that fails
    schema/business-rule validation."""


class ProviderUnavailableError(Exception):
    """Raised by a provider when it can't currently serve the request --
    rate limited, overloaded, unreachable, or simply not configured (no API
    key set). Signals the orchestrator to try the next provider in the
    fallback chain; this is never raised for a genuine content/validation
    failure, which would likely fail identically on every provider."""


class AiProvider(ABC):
    @abstractmethod
    def name(self):
        ...

    @abstractmethod
    def generate(self, *, system, user_content, response_model):
        """Runs one structured-output request and returns a validated
        instance of `response_model` (any Pydantic model -- a plain card
        batch or the auto-count batch, see below). Raises
        ProviderUnavailableError or AiGenerationError; never returns None."""
        ...


# --- Card draft schemas, shared by every provider -----------------------
# A single generation batch is always one card type (the user picks the
# type before generating), so only one of these is used per request.

class BasicCardDraft(BaseModel):
    prompt: str
    answer: str


class BasicCardBatch(BaseModel):
    cards: list[BasicCardDraft]


class BasicCardAutoBatch(BaseModel):
    # How many total cards the model judges are needed to reasonably cover
    # the learning request -- the caller treats this as the draft's
    # target_count and fills the rest via further batches.
    recommended_total: int = Field(ge=5, le=500)
    cards: list[BasicCardDraft]


class MultipleChoiceCardDraft(BaseModel):
    prompt: str
    options: list[str] = Field(min_length=2, max_length=6)
    correct_option_index: int

    @model_validator(mode='after')
    def _correct_index_in_range(self):
        if not (0 <= self.correct_option_index < len(self.options)):
            raise ValueError('correct_option_index out of range for options')
        return self


class MultipleChoiceCardBatch(BaseModel):
    cards: list[MultipleChoiceCardDraft]


class MultipleChoiceCardAutoBatch(BaseModel):
    recommended_total: int = Field(ge=5, le=500)
    cards: list[MultipleChoiceCardDraft]


class TypedAnswerCardDraft(BaseModel):
    prompt: str
    answer: str
    accepted_answers: list[str] = Field(default_factory=list)


class TypedAnswerCardBatch(BaseModel):
    cards: list[TypedAnswerCardDraft]


class TypedAnswerCardAutoBatch(BaseModel):
    recommended_total: int = Field(ge=5, le=500)
    cards: list[TypedAnswerCardDraft]
