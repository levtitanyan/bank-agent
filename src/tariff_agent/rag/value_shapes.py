"""Recognising that a passage *states a value*, not merely mentions a field.

BM25 answers "does this passage talk about interest rates?". On the real
consumer-loan page four chunks do, and the one it ranks first is a marketing
banner; the rate table it ranks third. The difference between them is not
vocabulary, it is that one contains a rate and the other contains a slogan.

So a third signal is derived from something the project already knows: every
field in the registry declares a :class:`~tariff_agent.fields.ValueKind`, and
each kind has a recognisable written form. A chunk that mentions the field *and*
contains a value of the right shape is far more likely to be the one worth
quoting.

This stays deterministic and explainable - it is a regular expression per kind,
not a model - and it is fused as a *ranking*, so it can promote a chunk without
being weighed against scores on a different scale.
"""

from __future__ import annotations

import re
from typing import Final

from tariff_agent.fields import ValueKind

_PERCENT: Final = re.compile(r"\d{1,3}(?:[.,]\d{1,2})?\s?%")
_AMOUNT: Final = re.compile(r"\d{1,3}(?:[ ,.]\d{3})+|\d{4,}")
_CURRENCY: Final = re.compile(
    r"ՀՀ\s*դրամ|դրամ|AMD|USD|EUR|ԱՄՆ\s*դոլար|դոլար|եվրո|արտարժույթ", re.IGNORECASE
)
_TERM: Final = re.compile(r"\d{1,3}\s*(?:ամիս|ամսվա|տարի|տարվա|months?|years?)", re.IGNORECASE)
_FREE: Final = re.compile(r"անվճար|չի\s*գանձվում|չի\s*կիրառվում|0\s?%", re.IGNORECASE)
_COLLATERAL: Final = re.compile(
    r"գրավ|երաշխավոր|ապահովվածություն|ապահովում|collateral|pledge", re.IGNORECASE
)
_SALARY: Final = re.compile(
    r"աշխատավարձ|աշխատավարձային|salary|payroll", re.IGNORECASE
)


def has_value_shape(text: str, kind: ValueKind) -> bool:
    """Report whether a passage contains a value of the expected kind.

    Args:
        text: The chunk text.
        kind: The field's value kind, from the registry.

    Returns:
        True when the text contains something written like a value of that kind.
        ``TEXT`` fields have no numeric shape, so their own vocabulary is used
        instead - collateral and salary privileges are recognised by the words
        that always accompany them.
    """
    if kind is ValueKind.PERCENT:
        return bool(_PERCENT.search(text))
    if kind is ValueKind.AMOUNT:
        return bool(_AMOUNT.search(text)) and bool(_CURRENCY.search(text))
    if kind is ValueKind.CURRENCY:
        return bool(_CURRENCY.search(text))
    if kind is ValueKind.TERM:
        return bool(_TERM.search(text))
    if kind is ValueKind.FEE:
        return bool(_PERCENT.search(text) or _AMOUNT.search(text) or _FREE.search(text))
    return bool(_COLLATERAL.search(text) or _SALARY.search(text))
