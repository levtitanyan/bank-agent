# Design decisions

Every non-obvious choice made so far: the alternatives that were on the table, why this one
won, the assignment requirement it serves, and the file it lives in. Where a decision has a
cost or a known limitation, that is stated too — a decision without a cost is usually one that
was not really made.

Identifiers are phase-scoped (`P2-D4` = Phase 2, decision 4) and stable; later phases append
rather than renumber.

**Phases covered:** 1 (contracts), 1.5 (review fixes), 2 (HTTP), 3 (discovery). Phases 4–9 —
document processing, RAG, extraction, snapshots, HITL, the ADK agent — have not been built, so
their decisions are not listed. Open questions already known are collected at the end.

---

# Phase 1 — Contracts

*The vocabulary every later phase speaks: what a tariff is, what a value must carry, what may
be configured. No behaviour, no I/O.*

### P1-D1 · One field registry, not ten scattered declarations
**Requirement:** 5.6 structured extraction · 5.15 project structure · **File:** [`fields.py`](../src/tariff_agent/fields.py)

The ten tariff fields are needed, in different forms, by four later stages.

| Option | Consequence |
|---|---|
| Declare fields where each stage needs them | Four copies — Gemini schema, RAG queries, report labels, diff rules — that drift apart |
| A plain list of field names | Loses `kind`, `required` and `query_terms`, which are exactly what the stages differ on |
| **Chosen: one `FieldSpec` registry carrying all per-field metadata** | Adding an eleventh field touches one file |

`id` is the key in extraction, snapshot and diff; `label_hy`/`label_en` are the report labels;
`kind` selects the normalizer (Phase 6) *and* the change-magnitude rule (Phase 7); `required`
drives completeness; `query_terms` are the per-field retrieval queries (Phase 5).

**Cost:** `ValueKind` is coarse. `FEE` must cover `"0.5%"`, `"5,000 AMD"` and `"անվճար"` with
one normalizer. At ten fields that is cheaper than a richer type system; at fifty it would not be.

### P1-D2 · Invariants enforced in validators, not in the prompt
**Requirement:** 5.6 "the model must not invent values" · 5.8 · **File:** [`models.py`](../src/tariff_agent/models.py)

| Option | Consequence |
|---|---|
| Instruct Gemini to emit `NOT_FOUND` and trust it | Prompt adherence drifts between model versions; nothing enforces it |
| Validate afterwards and log a warning | A warning still lets an unsourced number reach the report |
| **Chosen: a Pydantic validator that rejects the illegal state** | A value that cannot cite a source **cannot be constructed at all** |

The model is still instructed as well; the validator is what is load-bearing. This is the
single most important decision in the project.

### P1-D3 · `NOT_FOUND` is a string sentinel, not `None`
**Requirement:** 5.6 "missing information must be explicitly represented"

`None` is ambiguous between "absent from the document", "not looked at yet" and "key missing";
omitting the key is indistinguishable from a bug that dropped it. The literal string survives
JSON → SQLite → report unchanged and is visible in the model's own structured output.

### P1-D4 · Keep `value` verbatim **and** `normalized`
**Requirement:** 5.7 evidence · 5.9 normalize before comparison

Storing only the normalized form loses what the bank actually wrote («մինչև 60 ամիս»), which
is what a business user must see. Storing only raw text makes `"10 000 000 AMD"` and
`"10,000,000 AMD"` look like a change. Both are kept: humans read `value`, the diff compares
`normalized`.

### P1-D5 · A fresh extraction must cover exactly the registry
**Requirement:** 5.6 · 5.8 required-field validation

Missing keys are rejected because "dropped by a bug" and "not offered by the bank" must never
look alike — the report distinguishes them. Unknown keys are rejected because the model must
not be able to add a field of its own.

### P1-D6 · Evidence, not just a citation string
**Requirement:** 5.7 "document/page/section or equivalent source location"

`Evidence` is a structured record — `document_name`, `source_url`, `page`, `section`, `quote` —
rather than a formatted sentence. The `quote` is load-bearing: Phase 6 fuzzy-matches it against
the chunk the model claimed to read, so a fabricated quote is detectable. A prose citation
could not be checked mechanically.

### P1-D7 · `Evidence.page` is optional
**Requirement:** 5.4 · 5.7

HTML product pages have no pagination, and Phase 3 proved the consumer-loan **page** is a
first-class source. A required page number would have made the most important consumer-loan
evidence unrepresentable.

### P1-D8 · All contract models are frozen
**Requirement:** 5.15

A stage receiving a `FieldValue` must not be able to edit it in place; changes produce new
objects. Removes a whole class of "who mutated this?" bugs from a multi-stage pipeline.

### P1-D9 · Policy in committed YAML, secrets in the environment
**Requirement:** 5.12 · 5.15 · **Files:** [`config/`](../config), [`config.py`](../src/tariff_agent/config.py)

| Option | Consequence |
|---|---|
| Everything in `.env` | A reviewer cannot see the allowlist or product list in the repository |
| Everything in YAML | Secrets get committed |
| **Chosen: split by who must audit it** | The whole network surface is six lines of reviewable YAML; the key is a `SecretStr` from the environment |

### P1-D10 · Exact hosts, no wildcard subdomains
**Requirement:** 5.3 · 5.12 · **File:** [`config/allowlist.yaml`](../config/allowlist.yaml)

`*.acba.am` would admit any user-content or hijacked subdomain. **Cost:** a legitimate new host
needs a YAML edit — which is the point, since it then appears in a diff.

### P1-D11 · Google environment variables stay unprefixed
**Requirement:** 5.12

Everything else uses `TARIFF_`, but the google-genai SDK reads `GOOGLE_API_KEY` and
`GOOGLE_GENAI_USE_VERTEXAI` by those exact names. Renaming buys consistency and breaks the SDK.
Deliberate, and commented where it happens.

### P1-D12 · The API key is a `SecretStr`, and no key is a supported mode
**Requirement:** 5.12 · 5.14

`SecretStr` keeps the key out of reprs, logs and tracebacks (asserted by a test). `has_api_key`
lets demos and tests take an offline path rather than crash — which is what makes the whole
suite runnable by a reviewer with no key.

### P1-D13 · Configuration failures raise `ConfigError` at load time
**Requirement:** 5.11 · 5.15

`_read_yaml` converts `FileNotFoundError` and YAML errors into one error type naming the file.
A typo fails at startup with a clear message instead of surfacing as odd behaviour halfway
through a monitoring run. `get_settings()` is `lru_cache`d — the environment is read once.

### P1-D14 · One exception class per failure mode
**Requirement:** 5.11 · **File:** [`errors.py`](../src/tariff_agent/errors.py)

Callers branch on type, never on message text: Phase 2's retry logic must distinguish a timeout
from a 404 without parsing English, and Phase 8 must map failures to tool statuses.

### P1-D15 · `NeedsReviewError` is separate from failures
**Requirement:** 5.10

Refusing to decide alone is correct behaviour, not a bug. It carries a machine-readable
`reason` and the `details` a reviewer needs — so escalation is a *data-carrying* event.

### P1-D16 · JSON logs with a `ContextVar` run id
**Requirement:** 5.13 · **File:** [`observability/logging.py`](../src/tariff_agent/observability/logging.py)

| Option | Consequence |
|---|---|
| Human-readable log lines | Phase 9 metrics would require parsing English |
| Thread `run_id` through every function | Pollutes every signature in every module, permanently |
| **Chosen: JSON + `ContextVar`** | Machine-readable, correlated, invisible to signatures |

Anything passed as `extra={...}` becomes a top-level key. Armenian is written unescaped
(`ensure_ascii=False`) so logs stay readable. **Chain-of-thought is never logged.**

### P1-D17 · `Allowlist` is data with no methods
**Requirement:** 5.2 least-privilege

Matching must run on every redirect hop, which is the HTTP layer's concern. Configuration
describes policy; the network layer enforces it.

### P1-D18 · src-layout, Python 3.11
**Requirement:** 5.15

src-layout means tests import the installed package, not stray files next to them. 3.11 because
the system default here is 3.14, which has no reliable PyMuPDF or google-adk wheels — verified
by installing the full stack before Phase 3.

### P1-D19 · Docstrings enforced by the linter
**Requirement:** 5.15

`ruff` runs with the `D` ruleset and Google convention, so "every module has a docstring" is
checked rather than remembered.

---

# Phase 1.5 — Review fixes

*Three contract changes made before any behaviour depended on them.*

### P1.5-D1 · `schema_version` plus a lenient `from_stored()`
**Requirement:** 5.9 · 5.11 previous snapshot unavailable

| Option | Consequence |
|---|---|
| One strict path for everything | Adding an eleventh field breaks every stored snapshot and crashes the diff |
| A `strict=False` flag on the constructor | One careless call site silently accepts partial Gemini output |
| **Chosen: a separately named loader used only for stored data** | The name says where the data came from; a test asserts the strict path stays strict |

### P1.5-D2 · Old snapshots backfill as `NOT_FOUND`
**Requirement:** 5.6 · 5.9

Introducing an `UNKNOWN` state for "this snapshot predates the field" would spread through the
diff, the report and the completeness score to serve one migration case. `NOT_FOUND` is the
honest reading: we never captured it. **Cost:** the first diff after adding a field shows
`NOT_FOUND → 0.5%` as a change — arguably correct, and the stored `schema_version` explains it.

### P1.5-D3 · Unknown stored fields are dropped, with a log line
**Requirement:** 5.11 · 5.13

A field removed from the registry must not block loading old data, but silently discarding
stored values would be invisible. It is dropped *and* logged with the field names.

### P1.5-D4 · Per-field `unverified`/`conflict`; run-level `NeedsReviewError`
**Requirement:** 5.10 · 5.8

A field can be questionable (`unverified`: its quote did not match; `conflict`: two official
sources disagree). Whether the **run** stops for a human is a different question, belonging to
the pipeline. Mixing them would have made "needs review" mean two things.

### P1.5-D5 · `confidence` is computed deterministically, and `None` until it is
**Requirement:** 5.6 · 5.8

| Option | Consequence |
|---|---|
| Ask Gemini for a confidence score | LLM self-reported confidence is not calibrated; it looks authoritative and means little |
| Default to `1.0` until Phase 6 | A made-up number in the schema that a reviewer would rightly challenge |
| **Chosen: `float \| None`, defaulting to `None`** | Phase 6 computes it from quote-match score, document quality and retrieval score |

The Gemini response schema will not contain a confidence field at all.

---

# Phase 2 — The guarded HTTP layer

*The single network door. Everything downstream receives bytes this layer has already declared
safe.*

### P2-D1 · URL policy is pure functions, separate from the client
**Requirement:** 5.3 · 5.12 · **File:** [`http/url_policy.py`](../src/tariff_agent/http/url_policy.py)

Separating the rule from the machinery makes it testable without HTTP, cheap to re-apply on
every redirect hop, and reusable by Phase 3 for filtering hundreds of harvested links — where
raising per rejected link would be the wrong shape, so `filter_allowed` drops and counts.

### P2-D2 · Exact-host matching, never suffix matching
**Requirement:** 5.3 · 5.12

`host.endswith(".acba.am")` is the classic mistake: it accepts `acba.am.evil.com`. Tests pin
the lookalikes — `acba.am.evil.com`, `evil-acba.am`, `sub.acba.am`, non-default ports, `http://`.

### P2-D3 · Normalization preserves percent-encoding
**Requirement:** 5.3 · 5.11

Lowercasing the host and dropping the fragment is safe; decoding the path is not.
`loan%20info.pdf` must stay encoded — that is the mortgage information summary, and a
round-trip bug there silently 404s. One test pins it.

### P2-D4 · Manual redirect loop
**Requirement:** 5.3 · 5.12 · **File:** [`http/client.py`](../src/tariff_agent/http/client.py)

| Option | Consequence |
|---|---|
| `httpx.follow_redirects=True` | One parameter, and silently wrong: httpx would follow `acba.am → cdn.elsewhere.com` and return the bytes with no indication the domain changed. The allowlist becomes decorative |
| **Chosen: follow hops manually, re-checking the allowlist on each** | ~15 lines; an off-domain redirect raises before the second request is made |

The test asserts the transport was called exactly **once**, which is the only way to prove the
second request never happened.

### P2-D5 · Policy runs before the socket
**Requirement:** 5.12

`assert_allowed()` is the first statement in `fetch()`. A blocked host is never contacted at
all — not contacted and then discarded.

### P2-D6 · Content kind checked twice
**Requirement:** 5.12 file-type validation · 5.11

The header is a claim (a 404 page can be served as `application/pdf`); magic bytes alone would
accept a PDF where a page was expected. Both are checked, so a mislabelled response fails at
the network boundary rather than as a confusing PyMuPDF crash in Phase 4. The HTML check strips
a BOM and leading whitespace first, because real pages start that way.

### P2-D7 · Size cap enforced on bytes received
**Requirement:** 5.12 download-size restrictions

`Content-Length` is checked first as an optimisation, but it is only a claim. The real defence
aborts the stream once 25 MB have actually arrived.

### P2-D8 · Retries bounded, and only where retrying can help
**Requirement:** 5.11 "bounded … only to appropriate transient failures, preferably with backoff"

Retryable: connection errors, timeouts, 408, 425, 429, 5xx. Not 4xx — a 404 is an answer, and
retrying it wastes the bank's bandwidth. Exponential backoff with jitter, so concurrent retries
do not synchronise.

### P2-D9 · 403 is logged as `access_blocked` and never retried
**Requirement:** 5.3 "do not bypass access restrictions" · 5.11

An access decision is respected, not hammered. The distinct log event also makes "the bank
started blocking us" visible in metrics rather than buried among generic failures.

### P2-D10 · `Retry-After` is honoured but capped
**Requirement:** 5.11

A server asking for 600 seconds would stall a monitoring run. The hint wins over our own
backoff, but never exceeds `backoff_max`.

### P2-D11 · The cache revalidates; it does not short-circuit
**Requirement:** 5.9 · 5.11

| Option | Consequence |
|---|---|
| Serve cached bytes when present | **A monitor that never asks the server cannot detect a change** — the cache would defeat the product |
| Never cache | Re-downloads a 1 MB PDF on every run and every demo |
| **Chosen: always request, with `If-None-Match`/`If-Modified-Since`; 304 reuses cached bytes** | One cheap round-trip, no re-download, and a positive statement from the bank that nothing changed |

### P2-D12 · Validators survive a permanent redirect — but only to the same resource
**Requirement:** 5.9 · 5.11

**Found by a live run, not by reasoning.** ACBA 301-redirects `www.acba.am` → `acba.am`, and
dropping the conditional headers after a redirect silently re-downloaded the 1 MB PDF every
time. They are now re-attached when the target matches the cached `final_url`, so a 304 from a
*different* resource still means nothing.

### P2-D13 · Two hashes, two jobs
**Requirement:** 5.9 · 5.5

The cache filename hashes the **URL** (it locates the entry); the **content** sha256 lives in
the sidecar metadata and tells Phase 5 whether the document itself changed. One hash could not
do both. **Known gap:** ACBA serves `loans-tariffs.pdf` from three different URLs, so URL-keyed
entries duplicate it — deduplication by content hash belongs to Phase 4/5.

### P2-D14 · `offline` is explicit, not inferred
**Requirement:** 5.14 · 5.11

Demos and tests need a no-network mode, but inferring it (e.g. "no key, so offline") hides
failures. `offline=true` serves the cache and, when nothing is cached, raises — missing data is
reported, never invented.

### P2-D15 · `transport` and `sleep` are injected
**Requirement:** 5.14

Patching `time.sleep` globally is fragile and invisible in a test's signature. Explicit seams
make all 25 client tests offline and instant, and let tests assert *how many* requests were
made — the only way to prove "404 is not retried" or "the second fetch revalidated".

### P2-D16 · robots.txt: 4xx allows, 5xx stops
**Requirement:** 5.3 · **File:** [`http/robots.py`](../src/tariff_agent/http/robots.py)

| Option | Consequence |
|---|---|
| Blanket fail-open | Ignores a real signal whenever the server hiccups |
| Blanket fail-closed | Makes the agent hostage to a file most servers do not have |
| **Chosen: the split Google's crawler documents** | 4xx → no rules exist → proceed; 5xx or timeout → permission unknown → stop rather than resolving doubt in our own favour |

### P2-D17 · robots.txt is fetched through `SafeHttpClient` itself
**Requirement:** 5.12 least privilege

A second `httpx` call would be a second network door with its own timeouts and caps. The
recursion is broken with a `check_robots=False` flag and a two-line `Protocol`, so the client
depends on a *shape* rather than on the module that depends on it.

### P2-D18 · `FetchResult` lives in `http/client.py`, not `models.py`
**Requirement:** 5.15

It carries raw bytes and HTTP concerns; putting it beside the tariff contracts would mix
transport with domain. The rule adopted here — *a type lives in the module that produces it* —
is followed again in Phase 3 (`ProductResolution`, `SourceCandidate`).

### P2-D19 · Synchronous client
**Requirement:** 5.11 · 5.15

ADK tools are called sequentially, so concurrency would add failure modes (partial results,
interleaved retries, harder logs) with no benefit at this scale. Revisit only if a crawl
becomes the bottleneck.

### P2-D20 · Cache write failures are logged and swallowed
**Requirement:** 5.11

An unwritable cache must not fail a run that already holds the bytes it needs. The failure is
logged; the run continues.

### P2-D21 · `read_timeout` raised 30 s → 60 s
**Requirement:** 5.11 reasonable timeouts

Not a guess: a live run showed ACBA taking over 30 s to serve the 1 MB tariff PDF, costing a
retry on every fetch. The reason is written into the docstring so the number is not mysterious.

---

# Phase 3 — Discovery

*Deciding what to download: which product the user meant, and which pages and documents are
authoritative for it.*

### P3-D1 · Product resolution is deterministic — no Gemini
**Requirement:** 5.3 · 5.1 "which controls remain deterministic" · **File:** [`discovery/product_matcher.py`](../src/tariff_agent/discovery/product_matcher.py)

| Option | Consequence |
|---|---|
| Ask Gemini which product was meant | A wrong resolution silently reports **another product's** tariffs; not reproducible offline, not explainable in one log line |
| Embeddings | Phase 5 will have an embedder; adding one here first is a half-semantic version with more moving parts |
| **Chosen: curated synonyms + fuzzy matching + a penalty list** | Honestly hybrid, testable, instant, and works with no API key |

### P3-D2 · Three matching layers, because two are not enough
**Requirement:** 5.3

The synonym dictionary is the semantic layer — no string metric discovers that «հիփոթեք»,
"mortgage" and "ипотека" are one product. Fuzzy matching is the typo layer. Neither can express
*"we do not monitor that"*: `token_set_ratio("business mortgage", "mortgage")` is **100**,
because set matching ignores the extra token. Hence `unsupported_terms` — and ACBA really does
sell a business mortgage, under `/hy/business/`.

### P3-D3 · Bands, not a single threshold
**Requirement:** 5.3 · 5.10

≥85 **with a ≥10 lead** resolves; 60–85, or any close race, is `AMBIGUOUS` and goes to a human;
below 60 is `NOT_FOUND`. The lead requirement is what makes bare `"loan"` escalate: it scores
100 against both products, and a single threshold would have picked one arbitrarily.

### P3-D4 · Resolution returns a status; it does not raise
**Requirement:** 5.10 · 5.1 stopping conditions

The ambiguous case carries the data a reviewer needs (both candidates and their scores), and
Phase 8 tools must hand the model a status dictionary, never an exception. The deterministic
pipeline converts `NOT_FOUND` into `ProductNotFoundError` at its own boundary.

### P3-D5 · Every resolution explains itself
**Requirement:** 5.13 · 5.10

The result records which synonym matched and in which language, plus a one-sentence `reason`.
"Why did «ипотека» resolve to mortgage?" is answerable from one log line.

### P3-D6 · Queries are NFC-normalized and punctuation-stripped
**Requirement:** 5.4 Armenian text

Composed and decomposed Armenian must compare equal, and «» quotes around a product name are
common in real queries. Over-long input is truncated rather than rejected, since it is usually
a pasted sentence.

### P3-D7 · Latin transliterations are synonyms, not a transliteration engine
**Requirement:** 5.3

`ipoteka`, `potrebkredit`, `hipotekayin vark` are listed in `products.yaml`. A transliteration
library would be a dependency and a source of surprises for a handful of predictable strings.
**Cost:** an unlisted transliteration will not resolve.

### P3-D8 · Sitemap parsed with `defusedxml`
**Requirement:** 5.12 · **File:** [`discovery/sitemap.py`](../src/tariff_agent/discovery/sitemap.py)

Stock XML parsers expand entities: a "billion laughs" document is a few hundred bytes on the
wire and gigabytes in memory. A test feeds the parser exactly that.

### P3-D9 · Sitemap failures degrade to `[]`
**Requirement:** 5.11

The sitemap is one discovery route of three; losing it must not stop a run that can still use
seed pages. ACBA's real sitemap contains a malformed `<loc>` — one bad entry must not discard
the other 587.

### P3-D10 · Primary source **plus** supporting sources
**Requirement:** 5.3 · 5.5 · **File:** [`discovery/sources.py`](../src/tariff_agent/discovery/sources.py)

| Option | Consequence |
|---|---|
| Return the single highest-scoring source | Reports the wrong document for the consumer loan |
| "PDF beats HTML" | The consumer-loan page links **no** «ամփոփագիր» and states its rates inline, while the shared `loans-tariffs.pdf` outscores it on keywords |
| **Chosen: one primary plus ranked supporting sources** | Consumer → the page (primary) + the tariff PDF (supporting); mortgage → the summary PDF (primary) + the rest |

Phase 6 needs both anyway: corroborating a rate across two official sources is how a `conflict`
is detected.

### P3-D11 · Role before score when choosing the primary
**Requirement:** 5.3 "most authoritative available information"

A document covering *every* loan product is never one product's authoritative source, however
well it scores. Only a candidate carrying a primary signal — an information summary, or a page
that states rates itself — may lead. **Supporting sources are then ranked by score alone**, so
the shared tariff PDF is not buried beneath sibling product pages. (Both halves were bugs
first, caught by the live run.)

### P3-D12 · Scores carry their reasons
**Requirement:** 5.7 · 5.13

Every candidate holds strings like `anchor mentions 'տեղեկատվական ամփոփագիր' (+50)`. Costs a
few lines; makes the HITL screen usable and lets a bad ranking be debugged from `log.jsonl`
alone.

### P3-D13 · Scoring weights in YAML, including negative ones
**Requirement:** 5.3 · 5.12 · **File:** [`config/discovery.yaml`](../config/discovery.yaml)

The Armenian keywords are policy a bank-side reviewer should be able to read and correct.
Negative weights matter as much as positive: an archived tariff PDF («արխիվ») and a business
product are the two ways a plausible-looking document is the wrong one.

### P3-D14 · Path segments outrank keywords as a discriminator
**Requirement:** 5.3

ACBA separates audiences structurally: `/hy/individual/`, `/hy/business/`, `/hy/agro/`. A
`/business/` penalty of −60 is more reliable than any keyword, because it reflects how the bank
organises its own site rather than how it phrases a title.

### P3-D15 · Two floors, not one
**Requirement:** 5.11 bounded work

`min_score` drops weak candidates from the results. The higher `page_min_score` decides what is
worth *fetching* at all, because a loosely-matching page costs a network round-trip to rule
out. A single floor let `business-mortgage` into the crawl.

### P3-D16 · Shared documents are exempt from the slug requirement
**Requirement:** 5.3

`loans-tariffs.pdf` legitimately covers every loan product. Requiring a product-slug match
would discard the one document that corroborates both products.

### P3-D17 · Seeds keep guaranteed crawl slots, and are flagged
**Requirement:** 5.3 · 5.7 provenance

A site full of loosely-matching pages must not evict the curated page from the budget. When
live discovery finds nothing and seeds are used instead, candidates are flagged `is_seed` with
a note, so a reviewer can tell curated fallback from live discovery.

### P3-D18 · Two close information summaries require review
**Requirement:** 5.10 "two plausible official PDFs … a reviewer selects the correct one"

ACBA publishes separate summaries for purchase (90) and renovation (80) mortgages. Picking the
higher automatically is a coin flip on which product's rates get reported. Restricted to PDFs:
sibling product pages routinely score alike and are not a real dilemma.

### P3-D19 · A broken page does not end the crawl
**Requirement:** 5.11 404 / website unavailable

One page failing is logged and skipped; the sources found elsewhere still stand. Only *no*
sources at all raises `SourceNotFoundError` — missing data is reported, never quietly empty.

### P3-D20 · The client owns the allowlist; link filtering reads it from there
**Requirement:** 5.12

Discovery filters links with the same allowlist instance the client enforces, exposed as a
property, rather than loading a second copy that could drift out of sync.

### P3-D21 · Fixtures are trimmed **real** pages, with provenance headers
**Requirement:** 5.14

| Option | Consequence |
|---|---|
| Hand-written HTML | Tests the parser against my assumptions, not the bank's markup |
| Whole pages (~400 KB each) | Unreviewable in a diff |
| **Chosen: trimmed to 0.5–3 KB, each headed with source URL and fetch date** | Real structure, reviewable, and honest about when it was captured |

This paid for itself: the fixtures carry the `http://` PDF link and the three URLs ACBA serves
one document from — neither of which I would have invented.

---

# Tooling decisions

### T-D1 · mypy strict on `src` only
**Requirement:** 5.15

Tests deliberately pass plain strings where pydantic coerces them (`source_url="https://…"`) —
that is what production callers do and what the tests verify, not a type error. Strictness is a
guarantee about shipped code; tests keep `disallow_untyped_defs` so signatures stay honest.

### T-D2 · Full dependency install verified before Phase 4
**Requirement:** 5.15

`google-adk`, PyMuPDF, pytesseract, rank-bm25 and rapidfuzz were installed and imported on 3.11
before the phase that needs them, and Tesseract was confirmed to have `hye`. Finding a wheel
problem during Phase 6 would be far more expensive.

---

# Known gaps and open questions

Things already known to need a decision later, recorded so they are not discovered in the
review instead:

| Topic | Status |
|---|---|
| Duplicate documents across URLs | `loans-tariffs.pdf` is served from three URLs; deduplication by content sha256 belongs to Phase 4/5 (P2-D13) |
| `ValueKind` granularity | `FEE` covers percent, amount and «անվճար» with one normalizer (P1-D1) |
| First diff after a registry change | Shows `NOT_FOUND → value` as a change (P1.5-D2) |
| Unlisted transliterations | Will not resolve (P3-D7) |
| Fixture staleness | Real fixtures date; the live script re-verifies, but they will need refreshing (P3-D21) |
| `gemini-2.5-flash-lite` | Chosen for cost; lite is weaker at structured extraction from Armenian tables, which is Phase 6's hard part. One env var to change |
| Scoring weights are hand-tuned | Validated against two products on one bank; the Phase 9 evaluation set should measure them |

---

# Requirement coverage

| § | Requirement | Status |
|---|---|---|
| 5.1 | ADK agent | Phase 8 · deterministic/LLM split already drawn (P3-D1) |
| 5.2 | Focused tools | Phase 8 · the functions they will wrap exist and are pure |
| 5.3 | Official source discovery | ✅ P3-D1…D20, P1-D10, P2-D2 |
| 5.4 | PDF / OCR processing | Phase 4 · Armenian normalization started (P3-D6) |
| 5.5 | Chunking and RAG | Phase 5 · per-field query terms exist (P1-D1) |
| 5.6 | Structured extraction | Schema ✅ (P1-D1…D5); extraction Phase 6 |
| 5.7 | Evidence and provenance | Model ✅ (P1-D6, P1-D7); populated Phase 6 |
| 5.8 | Deterministic validation | Invariants ✅ (P1-D2, P1-D5); normalizers Phase 6 |
| 5.9 | Change detection | Storage contract and revalidation ✅ (P1.5-D1, P2-D11); diff Phase 7 |
| 5.10 | HITL | Triggers ✅ (P1-D15, P1.5-D4, P3-D4, P3-D18); reviewer interface Phase 7 |
| 5.11 | Error handling | ✅ P1-D14, P2-D8…D10, P2-D20, P3-D9, P3-D19 |
| 5.12 | Security | ✅ P1-D9…D12, P2-D2, P2-D4…D7, P2-D16, P2-D17, P3-D8 |
| 5.13 | Observability | ✅ P1-D16, P3-D5, P3-D12 |
| 5.14 | Testing | ✅ 155 tests, P2-D15, P3-D21; evaluation dataset Phase 9 |
| 5.15 | Python engineering | ✅ P1-D18, P1-D19, T-D1 |
