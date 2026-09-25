"""The shape Gemini must return.

Deliberately narrow. The model reports what a passage says and where it said it;
it reports no confidence (an LLM's own estimate is not calibrated - P1.5-D5), no
explanation, and no normalized form. Everything derived belongs to deterministic
code that can be tested.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from tariff_agent.models import NOT_FOUND


class ExtractedVariant(BaseModel):
    """One channel's value, when a document states several.

    Attributes:
        label: How the document distinguishes this one - «acba digital»,
            «Մասնաճյուղ», «աշխատավարձային».
        value: The value for this channel, verbatim.
        quote: The sentence or row stating it, copied exactly.
        chunk_id: Which passage the quote came from.
    """

    model_config = ConfigDict(extra="ignore")

    label: str
    value: str
    quote: str
    chunk_id: str


class ExtractedField(BaseModel):
    """One field, as read from the passages.

    Attributes:
        field_id: Which tariff field this answers.
        value: The value exactly as written, or the NOT_FOUND sentinel.
        quote: The text supporting it, copied verbatim from a passage. Verified
            afterwards against the chunk it claims to come from.
        chunk_id: The passage the quote was copied from.
        variants: Per-channel breakdown when the document gives more than one
            value, e.g. a different rate in the app and at a branch.
    """

    model_config = ConfigDict(extra="ignore")

    field_id: str
    value: str = NOT_FOUND
    quote: str = ""
    chunk_id: str = ""
    variants: list[ExtractedVariant] = Field(default_factory=list)

    @property
    def is_not_found(self) -> bool:
        """Whether the model reported the field as absent."""
        return self.value.strip().upper() == NOT_FOUND or not self.value.strip()


class ExtractionResponse(BaseModel):
    """What one call returns: an answer for each field in the group.

    Attributes:
        fields: One entry per requested field.
    """

    model_config = ConfigDict(extra="ignore")

    fields: list[ExtractedField] = Field(default_factory=list)
