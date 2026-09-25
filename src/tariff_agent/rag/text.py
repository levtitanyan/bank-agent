"""Armenian-aware text handling for retrieval.

Three rules do most of the work on this corpus, and each exists because of
something measured in the real documents rather than assumed:

1. **Morphology.** Armenian is agglutinative: «տոկոսադրույք», «տոկոսադրույքը»,
   «տոկոսադրույքի», «տոկոսադրույքին» and «տոկոսադրույքների» are one concept
   written five ways. Exact-token matching finds the one the document happens to
   use and misses the rest, so a light suffix stripper runs on both sides.
2. **OCR damage.** Tesseract's Armenian model reads «և» as «ն», so the mortgage
   summary contains «Տնողություն» where the document says «Տևողություն». Folding
   «և» to «ն» *for matching only* makes the two forms meet. Stored text keeps
   «և» exactly (see P4-D7): this is a comparison key, not a rewrite.
3. **Numbers are content.** «13,5%» and «1,000,000» are the values being looked
   for. A tokenizer that splits them into digits destroys the strongest signal
   that a chunk is about a rate or an amount.

None of this is a morphological analyser. It is a set of rules whose failure
modes are known, tested and written down.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Final

# Longest first: at most one suffix is stripped, so the order decides which.
_SUFFIXES: Final[tuple[str, ...]] = (
    "ներիս",
    "ներիդ",
    "ներին",
    "ներից",
    "ներով",
    "ներում",
    "ները",
    "ների",
    "ներ",
    "ությամբ",
    "ությունից",
    "ությունը",
    "ություն",
    "ին",
    "ից",
    "ով",
    "ում",
    "ու",
    "ը",
    "ն",
    "ի",
)

MIN_STEM_LENGTH: Final[int] = 3
"""Below this, stripping destroys the word rather than normalising it."""

# Armenian function words carry no retrieval signal and inflate document length,
# which BM25 penalises. Deliberately short: a stoplist that removes a real term
# is far more expensive than one that keeps a common word.
_STOPWORD_SOURCE: Final[frozenset[str]] = frozenset(
    {
        "և", "եւ", "որ", "որը", "է", "են", "էր", "այլ", "կամ", "նաև", "իսկ",
        "այս", "այդ", "այն", "ու", "թե", "ին", "մեջ", "համար", "հետ", "առ", "ն",
        "the", "and", "or", "of", "for", "in", "on", "to", "a", "an", "is", "are",
    }
)

# A number keeps its separators and percent sign; a word is any run of letters.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[0-9][0-9.,]*%?|[^\W\d_]+", re.UNICODE)


def fold_for_matching(text: str) -> str:
    """Return the form used for comparison, never for storage.

    Applies NFC, folds case, and maps «և» to «ն» so that OCR output matches the
    parser's reading of the same word. The mapping is one-directional: a real
    «ն» is left alone, and only «և» moves, which limits the collisions this can
    create.

    Args:
        text: Any text.

    Returns:
        The folded form.
    """
    return unicodedata.normalize("NFC", text).replace("և", "ն").casefold()


_STOPWORDS: Final[frozenset[str]] = frozenset(
    fold_for_matching(word) for word in _STOPWORD_SOURCE
)
"""The stoplist, folded.

It has to be folded: «և» folds to «ն», so a stoplist compared against unfolded
words lets the conjunction through as a bare «ն» token in every document.
"""


def stem_hy(token: str) -> str:
    """Strip one Armenian inflectional suffix, if it is safe to do so.

    At most one suffix is removed, and never when the result would be shorter
    than :data:`MIN_STEM_LENGTH`. Iterative stripping was rejected: it turns
    «ներում» into fragments and gains nothing on this vocabulary.

    Args:
        token: A single folded token.

    Returns:
        The stem, or the token unchanged when no suffix applies.

    Note:
        This does not model vowel alternation. Armenian drops a vowel in some
        genitives - «ամփոփագիր» becomes «ամփոփագրի» - and those two forms will
        not meet. Handling it needs a real analyser, and guessing would damage
        more words than it repairs.
    """
    if token.isdigit() or any(ch.isdigit() for ch in token):
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= MIN_STEM_LENGTH:
            return token[: -len(suffix)]
    return token


def tokenize(text: str, *, stem: bool = True, drop_stopwords: bool = True) -> list[str]:
    """Split text into the tokens used for lexical matching.

    Args:
        text: Any text.
        stem: Whether to strip Armenian suffixes.
        drop_stopwords: Whether to remove function words.

    Returns:
        Folded tokens, numbers kept whole.
    """
    folded = fold_for_matching(text)
    tokens = _TOKEN_RE.findall(folded)
    if drop_stopwords:
        tokens = [token for token in tokens if token not in _STOPWORDS]
    if stem:
        tokens = [stem_hy(token) for token in tokens]
    return [token for token in tokens if token]


MIN_TERM_COVERAGE: Final[float] = 0.6
"""How much of a query term must occur before it counts as present.

Not all of it: real passages paraphrase. «աշխատավարձը բանկի միջոցով ստացող
հաճախորդ» is written in the document as «աշխատավարձը բանկի միջոցով ստանալու
դեպքում» - a different participle, unmistakably the same thing.
"""


def has_lexical_hit(chunk_text: str, query_terms: tuple[str, ...]) -> bool:
    """Report whether a query term substantially occurs in a chunk.

    Used by the relevance gate. A semantic retriever always returns its nearest
    neighbour, so for a field the bank does not offer it will return *something*;
    requiring either a lexical hit or a clear similarity score is what keeps
    those fields honestly NOT_FOUND.

    Args:
        chunk_text: The chunk's text.
        query_terms: The field's query terms.

    Returns:
        True when at least :data:`MIN_TERM_COVERAGE` of some term's stemmed
        tokens occur in the chunk.
    """
    return has_informative_hit(chunk_text, query_terms, is_informative=None)


def has_informative_hit(
    chunk_text: str,
    query_terms: tuple[str, ...],
    *,
    is_informative: Callable[[str], int] | None,
) -> bool:
    """Report whether a query term's *discriminating* tokens occur in a chunk.

    Coverage is measured over the informative tokens only. «հայտի
    ուսումնասիրության վճար» shares «հայտ» and «վճար» with half the document;
    the token that actually identifies the field is «ուսումնասիրության».
    Counting the common ones let the gate accept any fee paragraph as evidence
    for an application fee the consumer product never states.

    Args:
        chunk_text: The chunk's text.
        query_terms: The field's query terms.
        is_informative: Ranks tokens by how rare they are in the corpus being
            searched; the rarest token of a term must be present. ``None``
            skips that check, which is the right default outside a corpus.

    Returns:
        True when a term is substantially present.
    """
    chunk_tokens = set(tokenize(chunk_text))
    for term in query_terms:
        term_tokens = tokenize(term)
        if not term_tokens:
            continue
        matched = sum(1 for token in term_tokens if token in chunk_tokens)
        needed = max(1, round(len(term_tokens) * MIN_TERM_COVERAGE))
        if matched < needed:
            continue
        if is_informative is None:
            return True
        # Coverage alone is not enough. «հայտի ուսումնասիրության վճար» shares
        # «հայտ» and «վճար» with half of a tariff document, so two of its three
        # tokens match paragraphs that say nothing about an application fee.
        # The token that identifies the field - the rarest one - has to be
        # there, or the consumer product would report a fee it never states.
        identifying = min(
            term_tokens, key=lambda token: (is_informative(token), -len(token))
        )
        if identifying in chunk_tokens:
            return True
    return False
