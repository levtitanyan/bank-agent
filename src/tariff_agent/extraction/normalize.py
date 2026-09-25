"""Turning what the bank wrote into something a computer can compare.

Every normalizer here is driven by the field's declared
:class:`~tariff_agent.fields.ValueKind`, and every one of them returns ``None``
rather than guessing. A wrong normalization is worse than none: it would make
the Phase 7 diff report a change that did not happen, or hide one that did.

The formats are taken from the real documents, not invented:

* «20.1-21.6%», «13,5 %», «17.5 - 21.6%» - ranges and comma decimals
* «50,000-10,000,000 ՀՀ դրամ», «50.000-10.000.000», «10 000 000»
* «9-60 ամիս», «մինչև 240 ամիս», «12 - 240 ամիս»
* «ՀՀ դրամ և արտարժույթ», «ՀՀ դրամ, ԱՄՆ դոլար»
* «անվճար», «չի գանձվում», «առկա չէ», «0%»
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Final

from tariff_agent.fields import ValueKind

_PERCENT_VALUE: Final = re.compile(r"(\d{1,3}(?:[.,]\d{1,3})?)\s*%?")
# «9-ից 60 ամիս» is how ACBA writes a range: the lower bound carries the
# ablative «-ից» before the upper one. Without allowing that suffix between the
# numbers, the range collapsed to its upper bound and a 9-60 month term was
# reported as 60-60.
_TERM_UNIT: Final = re.compile(
    r"(\d{1,4})\s*(?:-|–|—)?\s*(?:ից|-ից)?\s*(?:-|–|—)?\s*(\d{1,4})?\s*"
    r"(ամիս|ամսվա|տարի|տարվա|months?|years?)",
    re.IGNORECASE,
)
# A money amount: digit groups separated by comma, dot or space - ACBA uses all
# three, sometimes in the same document.
_AMOUNT_VALUE: Final = re.compile(r"\d{1,3}(?:[ ,. ]\d{3})+|\d{4,}")
_RANGE_SPLIT: Final = re.compile(r"\s*(?:-|–|—|to|մինչև)\s*")

_FREE: Final = re.compile(
    r"անվճար|չի\s*գանձվում|չի\s*կիրառվում|առկա\s*չէ|չկա|free\b|not\s+applicable", re.IGNORECASE
)
# «մինչն» is not a word: it is «մինչև» after Tesseract reads «և» as «ն». The
# OCR'd form has to be recognised here, because missing it turned «up to 120
# months» into «exactly 120 months» and manufactured a conflict with a 9-60
# month term that the two documents did not actually have.
_UP_TO: Final = re.compile(
    r"մինչև|մինչն|առավելագույն|max(?:imum)?|up\s+to", re.IGNORECASE
)
_FROM: Final = re.compile(r"նվազագույն|սկսած|min(?:imum)?|from", re.IGNORECASE)
_RANGE_MARKER: Final = re.compile(r"-ից|—|–|-")

_CURRENCIES: Final[tuple[tuple[str, str], ...]] = (
    ("ՀՀ դրամ", "AMD"), ("դրամ", "AMD"), ("AMD", "AMD"),
    ("ԱՄՆ դոլար", "USD"), ("դոլար", "USD"), ("USD", "USD"),
    ("եվրո", "EUR"), ("EUR", "EUR"),
    ("ռուբլի", "RUB"), ("RUB", "RUB"),
)

MONTHS_PER_YEAR: Final = 12


def normalize(value: str, kind: ValueKind) -> dict[str, Any] | None:
    """Normalize a raw value according to its field's kind.

    Args:
        value: The text exactly as the bank wrote it.
        kind: The field's declared value kind.

    Returns:
        A comparable mapping, or ``None`` when the text cannot be parsed with
        confidence. Returning None is a normal outcome, not a failure: the
        verbatim value is still reported, and the diff simply falls back to
        comparing text for that field.
    """
    text = unicodedata.normalize("NFC", value).strip()
    if not text:
        return None
    if kind is ValueKind.PERCENT:
        return _percent(text)
    if kind is ValueKind.AMOUNT:
        return _amount(text)
    if kind is ValueKind.TERM:
        return _term(text)
    if kind is ValueKind.CURRENCY:
        return _currency(text)
    if kind is ValueKind.FEE:
        return _fee(text)
    return _text(text)


def _numbers(text: str, pattern: re.Pattern[str]) -> list[float]:
    """Extract the numeric values a pattern finds.

    Args:
        text: The text to scan.
        pattern: The pattern whose first group holds a number.

    Returns:
        The numbers, in order of appearance.
    """
    found: list[float] = []
    for match in pattern.finditer(text):
        raw = match.group(1) if match.groups() else match.group(0)
        cleaned = raw.replace(",", ".").replace(" ", "").replace(" ", "")
        try:
            found.append(float(cleaned))
        except ValueError:
            continue
    return found


def _percent(text: str) -> dict[str, Any] | None:
    """Normalize a rate or rate range.

    Args:
        text: e.g. «20.1-21.6%», «13,5 %».

    Returns:
        ``{"min": .., "max": .., "unit": "percent"}``, or None. Values above
        100 are rejected: a tariff rate is not 250%, so that is a misparse.
    """
    if "%" not in text and not re.search(r"տոկոս", text, re.IGNORECASE):
        return None
    values = [value for value in _numbers(text, _PERCENT_VALUE) if 0 <= value <= 100]
    if not values:
        return None
    return {"min": min(values), "max": max(values), "unit": "percent"}


def _amount(text: str) -> dict[str, Any] | None:
    """Normalize a money amount or range.

    Separators differ even within one ACBA document - «50,000-10,000,000» on the
    page, «50.000-10.000.000» in the offer - so grouping is stripped rather than
    interpreted.

    Args:
        text: The raw amount.

    Returns:
        ``{"min": .., "max": .., "currency": ..}``, or None.
    """
    raw = _AMOUNT_VALUE.findall(text)
    values: list[float] = []
    for item in raw:
        digits = re.sub(r"[^\d]", "", item)
        if digits:
            values.append(float(digits))
    if not values:
        return None
    currency = _currency(text)
    result: dict[str, Any] = {"min": min(values), "max": max(values), "unit": "amount"}
    if currency:
        result["currency"] = currency["codes"][0]
    return result


def _term(text: str) -> dict[str, Any] | None:
    """Normalize a duration or duration range, always into months.

    Args:
        text: e.g. «9-60 ամիս», «մինչև 240 ամիս», «3 տարի».

    Returns:
        ``{"min_months": .., "max_months": .., "unit": "months"}``, or None.
    """
    match = _TERM_UNIT.search(text)
    if not match:
        return None
    first = float(match.group(1))
    second = float(match.group(2)) if match.group(2) else None
    unit = match.group(3).lower()
    factor = MONTHS_PER_YEAR if unit.startswith(("տարի", "տարվա", "year")) else 1
    values = [first * factor] + ([second * factor] if second is not None else [])

    low, high = min(values), max(values)
    if second is None and _UP_TO.search(text):
        low = 0.0
    return {"min_months": low, "max_months": high, "unit": "months"}


def _currency(text: str) -> dict[str, Any] | None:
    """Normalize currency mentions into ISO codes.

    Args:
        text: e.g. «ՀՀ դրամ և արտարժույթ».

    Returns:
        ``{"codes": [..]}`` in order of appearance, or None. «արտարժույթ»
        (foreign currency) is recorded as a flag rather than invented as a
        specific code - the document does not say which.
    """
    codes: list[str] = []
    for needle, code in _CURRENCIES:
        if re.search(re.escape(needle), text, re.IGNORECASE) and code not in codes:
            codes.append(code)
    foreign = bool(re.search(r"արտարժույթ|foreign currency", text, re.IGNORECASE))
    if not codes and not foreign:
        return None
    result: dict[str, Any] = {"codes": codes or ["UNSPECIFIED"]}
    if foreign:
        result["includes_foreign"] = True
    return result


def _fee(text: str) -> dict[str, Any] | None:
    """Normalize a fee, which may be a percent, an amount, or nothing at all.

    Args:
        text: e.g. «0.5% ամսական», «5,000 ՀՀ դրամ», «անվճար», «առկա չէ».

    Returns:
        A mapping describing the fee, or None when it cannot be read. «անվճար»
        and «չի գանձվում» become an explicit zero: the bank stating that no fee
        applies is information, and quite different from not stating one.
    """
    if _FREE.search(text) and not _PERCENT_VALUE.search(text.replace("0", "")):
        return {"kind": "none", "value": 0.0, "unit": "free"}
    percent = _percent(text)
    if percent:
        return {"kind": "percent", **percent}
    amount = _amount(text)
    if amount:
        return {"kind": "amount", **amount}
    if _FREE.search(text):
        return {"kind": "none", "value": 0.0, "unit": "free"}
    return None


def _text(text: str) -> dict[str, Any] | None:
    """Normalize free text for comparison only.

    Args:
        text: The raw text.

    Returns:
        ``{"text": ..}`` with whitespace collapsed and case folded, so that a
        reformatted sentence does not register as a tariff change.
    """
    collapsed = " ".join(text.split())
    return {"text": collapsed.casefold(), "length": len(collapsed)}
