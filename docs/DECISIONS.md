# Design decisions

Every non-obvious choice made so far, the alternatives that were on the table, and why this
one won. Grouped by the phase that made it, with the assignment requirement it serves and the
file where it lives.

Phases 1–3 of 9 are complete: contracts, the guarded HTTP layer, and discovery. Decisions for
document processing, RAG, extraction, snapshots, HITL and the ADK agent are not listed because
they have not been made yet.

---

## Phase 1 — Contracts

*What a tariff is, what a value must carry, what may be configured. No behaviour.*

### D1 · One field registry, not ten scattered declarations
**Requirement:** 5.6 structured extraction · 5.15 project structure
**File:** [`fields.py`](../src/tariff_agent/fields.py)

| Option | Why not |
|---|---|
| Declare the ten fields wherever each stage needs them | Four copies (Gemini schema, RAG queries, report labels, diff rules) that drift apart |
| A plain `list[str]` of field names | Loses `kind`, `required` and `query_terms`, which are needed by different stages |
| **Chosen: one `FieldSpec` registry carrying all per-field metadata** | Adding an eleventh field touches one file; every stage derives from the same source |

`kind` drives the normalizer (Phase 6) *and* the change-magnitude rule (Phase 7); `required`
drives completeness; `query_terms` are the retrieval queries (Phase 5). **Cost:** `ValueKind`
is coarse — `FEE` must cover `"0.5%"`, `"5,000 AMD"` and `"անվճար"` with one normalizer.

### D2 · Invariants enforced in validators, not in the prompt
**Requirement:** 5.6 "the model must not invent values" · 5.8 deterministic validation
**File:** [`models.py:_check_not_found_invariants`](../src/tariff_agent/models.py)

| Option | Why not |
|---|---|
| Instruct Gemini to emit `NOT_FOUND` and trust it | Prompt adherence drifts between model versions; nothing enforces it |
| Check after the fact and log a warning | A warning still lets an unsourced number reach the report |
| **Chosen: a Pydantic validator that rejects the illegal state** | A tariff value that cannot cite a source **cannot be constructed at all** |

We still instruct the model as well — but the validator is what is load-bearing.

### D3 · `NOT_FOUND` is a string sentinel, not `None`
**Requirement:** 5.6 "missing information must be explicitly represented"
**File:** [`models.py`](../src/tariff_agent/models.py)

| Option | Why not |
|---|---|
| `None` | Ambiguous between "absent from the document", "not looked at yet" and "key missing" |
| Omit the key entirely | Indistinguishable from a bug that dropped the field |
| **Chosen: the literal string `"NOT_FOUND"`** | Survives JSON → SQLite → report unchanged, and is visible in the model's own output |

### D4 · Keep `value` verbatim **and** `normalized`
**Requirement:** 5.7 evidence · 5.9 normalize before comparison
**File:** [`models.py:FieldValue`](../src/tariff_agent/models.py)

| Option | Why not |
|---|---|
| Store only the normalized form | The report must show what the bank actually wrote («մինչև 60 ամիս»), not our parse of it |
| Store only the raw text | `"10 000 000 AMD"` vs `"10,000,000 AMD"` would raise a false change alert |
| **Chosen: both** | Humans read the verbatim value; the diff compares the normalized one |

### D5 · A fresh extraction must cover exactly the registry
**Requirement:** 5.6 · 5.8 required-field validation
**File:** [`models.py:_check_fields_match_registry`](../src/tariff_agent/models.py)

Rejecting missing keys means "dropped by a bug" and "not offered by the bank" can never look
alike. Rejecting unknown keys means the model cannot invent a field of its own.

### D6 · Policy in committed YAML, secrets in the environment
**Requirement:** 5.12 security · 5.15 configuration management
**Files:** [`config/`](../config), [`config.py`](../src/tariff_agent/config.py)

| Option | Why not |
|---|---|
| Everything in `.env` | A reviewer cannot see the allowlist or product list in the repository |
| Everything in YAML | Secrets would be committed |
| **Chosen: split by who must audit it** | The whole network surface is six lines of reviewable YAML; the key is a `SecretStr` from the environment |

### D7 · Exact hosts, no wildcard subdomains
**Requirement:** 5.3 allowlist · 5.12 security
**File:** [`config/allowlist.yaml`](../config/allowlist.yaml)

`*.acba.am` would admit any user-content or hijacked subdomain. **Cost:** a legitimate new host
needs a YAML edit — which is the point, because it appears in review.

### D8 · Google environment variables stay unprefixed
**Requirement:** 5.12 configuration
**File:** [`config.py:Settings`](../src/tariff_agent/config.py)

Everything else uses `TARIFF_`, but the google-genai SDK reads `GOOGLE_API_KEY` and
`GOOGLE_GENAI_USE_VERTEXAI` by those exact names. Renaming them buys consistency and breaks
the SDK. The inconsistency is deliberate and commented.

### D9 · One exception class per failure mode
**Requirement:** 5.11 error handling
**File:** [`errors.py`](../src/tariff_agent/errors.py)

Callers branch on type, never on message text — Phase 2's retry logic must distinguish a
timeout from a 404 without parsing English.

### D10 · `NeedsReviewError` is separate from failures
**Requirement:** 5.10 HITL
Refusing to decide alone is correct behaviour, not a bug. It carries a machine-readable
`reason` and the `details` a reviewer needs.

### D11 · JSON logs with a `ContextVar` run id
**Requirement:** 5.13 observability
**File:** [`observability/logging.py`](../src/tariff_agent/observability/logging.py)

| Option | Why not |
|---|---|
| Human-readable log lines | Phase 9 metrics would require parsing English |
| Pass `run_id` through every function | Pollutes every signature in every module, permanently |
| **Chosen: JSON + `ContextVar`** | Machine-readable, correlated, and invisible to function signatures |

Chain-of-thought is never logged — decisions and their inputs and outputs only.

### D12 · `Allowlist` is data with no methods
**Requirement:** 5.2 least-privilege tool design
Matching must run on every redirect hop, which is the HTTP layer's job, not configuration's.

### D13 · Python 3.11
**Requirement:** 5.15
The system default here is 3.14, which has no reliable PyMuPDF or google-adk wheels.

---

## Phase 1.5 — Review fixes

### D14 · `schema_version` + a lenient `from_stored()`
**Requirement:** 5.9 change detection · 5.11 previous snapshot unavailable
**File:** [`models.py:TariffExtraction.from_stored`](../src/tariff_agent/models.py)

| Option | Why not |
|---|---|
| One strict path for everything | Adding an eleventh field breaks every stored snapshot and crashes the diff |
| A `strict=False` flag on the constructor | One careless call site silently accepts partial Gemini output |
| **Chosen: a separately named loader, used only for stored data** | The name says where the data came from; a test asserts the strict path stays strict |

Fields missing from an old snapshot are backfilled as `NOT_FOUND` — the honest reading of "we
never captured this" — rather than a new `UNKNOWN` state that would spread through the diff,
the report and the completeness score.

### D15 · Per-field `unverified` / `conflict`; run-level `NeedsReviewError`
**Requirement:** 5.10 HITL · 5.8 validation
A field can be *questionable* (`unverified`: its quote did not match the source; `conflict`:
two official sources disagree). Whether the **run** stops for a human is a different question,
and belongs to the pipeline, not to one value.

### D16 · `confidence` is computed, and `None` until it is
**Requirement:** 5.6 · 5.8
| Option | Why not |
|---|---|
| Ask Gemini for a confidence score | LLM self-reported confidence is not calibrated; it looks authoritative and means little |
| Default to `1.0` until Phase 6 | A made-up number in the schema that a reviewer would rightly ask about |
| **Chosen: `float \| None`, defaulting to `None`** | Phase 6 computes it from quote-match score, document quality and retrieval score; until then it is honestly blank |

---

## Phase 2 — The guarded HTTP layer

*The one place that touches the network.*

### D17 · Manual redirect loop
**Requirement:** 5.3 stay within the allowlist · 5.12 security
**File:** [`http/client.py:_fetch_with_redirects`](../src/tariff_agent/http/client.py)

| Option | Why not |
|---|---|
| `httpx.follow_redirects=True` | One parameter, and silently wrong: httpx would follow `acba.am → cdn.elsewhere.com` and hand back the bytes with no indication the domain changed. The allowlist becomes decorative |
| **Chosen: follow hops manually, re-checking the allowlist on each** | ~15 lines, and an off-domain redirect raises before the second request is ever made |

### D18 · Content kind checked twice
**Requirement:** 5.12 file type validation · 5.11 parsing failures
Header alone is a claim (a 404 page can be served as `application/pdf`); magic bytes alone
would accept a PDF where a page was expected. Both are checked, so a mislabelled response
fails at the network boundary instead of as a confusing PyMuPDF crash.

### D19 · Size cap enforced on bytes received
**Requirement:** 5.12 download size restrictions
`Content-Length` is checked first as an optimisation, but it is only a claim; the real defence
aborts the stream past 25 MB.

### D20 · Retries bounded, and only where retrying can help
**Requirement:** 5.11 "retries must be bounded and applied only to appropriate transient failures"
Retryable: connection errors, timeouts, 408, 425, 429, 5xx. **Not** 4xx — a 404 is an answer.
403 is logged as `access_blocked` and never retried: an access decision is respected, not
hammered. `Retry-After` is honoured but capped at `backoff_max`, so a server cannot stall a
monitoring run.

### D21 · The cache revalidates; it does not short-circuit
**Requirement:** 5.9 change detection · 5.11 reliability
**File:** [`http/client.py`](../src/tariff_agent/http/client.py)

| Option | Why not |
|---|---|
| Serve cached bytes when present | **A monitor that never asks the server cannot detect a change** — the cache would defeat the product |
| Never cache | Re-downloads a 1 MB PDF on every run and every demo |
| **Chosen: always request, with `If-None-Match` / `If-Modified-Since`; `304` reuses cached bytes** | One cheap round-trip, no re-download, and a positive statement from the bank that nothing changed |

Only explicit `offline=true` skips the network, for demos and tests.

**Found live:** ACBA permanently redirects `www.acba.am` → `acba.am`, and dropping the
validators across that hop silently re-downloaded the 1 MB PDF on every run. Validators are now
re-attached when — and only when — the redirect target matches the cached `final_url`, so a 304
from a *different* resource still means nothing.

### D22 · robots.txt: 4xx allows, 5xx stops
**Requirement:** 5.3 "do not bypass access restrictions"
**File:** [`http/robots.py`](../src/tariff_agent/http/robots.py)

| Option | Why not |
|---|---|
| Blanket fail-open | Ignores a real signal whenever the server has a hiccup |
| Blanket fail-closed | Makes the whole agent hostage to one file that most servers do not have |
| **Chosen: the split Google's crawler documents** | 4xx means no rules exist → proceed. 5xx or timeout means permission is unknown → stop, rather than resolving doubt in our own favour |

### D23 · robots.txt is fetched through `SafeHttpClient` itself
**Requirement:** 5.12 least privilege
A second `httpx` call would be a second network door with its own timeouts and caps. The
recursion is broken with a `check_robots=False` flag and a two-line `Protocol`, so the client
depends on a *shape* rather than on the module that depends on it.

### D24 · `transport` and `sleep` are injected
**Requirement:** 5.14 testing
Patching `time.sleep` globally is fragile and invisible. Explicit seams make all 25 client
tests offline and instant, and let a test assert *how many* requests were made — which is the
only way to prove "404 is not retried" or "the cache revalidated".

---

## Tooling

### D25 · mypy strict on `src` only
**Requirement:** 5.15 type hints
Tests deliberately pass plain strings where pydantic coerces them (`source_url="https://…"`),
which is what production callers do and what the tests verify. Strictness is a guarantee about
shipped code; tests keep `disallow_untyped_defs` so signatures stay honest.

### D26 · `read_timeout` raised 30 s → 60 s
**Requirement:** 5.11 reasonable timeouts
Not a guess: a live run showed ACBA taking over 30 s to serve the 1 MB tariff PDF, costing a
retry on every single fetch.

---

## Phase 3 — Discovery

*Deciding what to download.*

### D27 · Product resolution is deterministic — no Gemini
**Requirement:** 5.3 semantic/fuzzy/hybrid matching · 5.1 "which decisions remain deterministic"
**File:** [`discovery/product_matcher.py`](../src/tariff_agent/discovery/product_matcher.py)

| Option | Why not |
|---|---|
| Ask Gemini which product was meant | A wrong resolution silently reports *another product's* tariffs; and it cannot be reproduced offline or explained in one log line |
| Embeddings | Phase 5 will have an embedder; adding one here first would be a half-semantic version with more moving parts |
| **Chosen: curated synonyms + fuzzy matching + a penalty list** | Honestly hybrid: the dictionary is the semantic layer, rapidfuzz the typo layer |

### D28 · Three matching layers, because two are not enough
**Requirement:** 5.3
Synonyms give semantics (no string metric discovers that «հիփոթեք», "mortgage" and "ипотека"
are one product). Fuzzy gives typo tolerance. Neither can express *"we do not monitor that"*:
`token_set_ratio("business mortgage", "mortgage")` is **100**, because set matching ignores the
extra token. Only an explicit `unsupported_terms` penalty fixes it — and ACBA really does sell
a business mortgage.

### D29 · Resolution returns a status; it does not raise
**Requirement:** 5.10 HITL · 5.1 stopping conditions
The ambiguous case carries the data a reviewer needs (both candidates and their scores), and
Phase 8 tools must hand the model a status dictionary, never an exception. The deterministic
pipeline converts `NOT_FOUND` into `ProductNotFoundError` at its own boundary.

### D30 · Primary source **plus** supporting sources
**Requirement:** 5.3 · 5.5 evidence supplied to the model
**File:** [`discovery/sources.py`](../src/tariff_agent/discovery/sources.py)

| Option | Why not |
|---|---|
| Return the single highest-scoring source | Reports the wrong document for the consumer loan |
| "PDF beats HTML" | The consumer-loan page links **no** «ամփոփագիր» and states its rates inline, while the shared `loans-tariffs.pdf` outscores it on keywords |
| **Chosen: one primary plus ranked supporting sources** | Consumer → the page (primary) + the tariff PDF (supporting); mortgage → the summary PDF (primary) + the rest. Phase 6 needs both anyway |

### D31 · Role before score when choosing the primary
**Requirement:** 5.3 "most authoritative available information"
A document covering *every* loan product is never one product's authoritative source, however
well it scores. Only a candidate with a primary signal — an information summary, or a page that
states the rates itself — may lead. Supporting sources are then ranked by score alone, so the
shared tariff PDF is not buried beneath sibling pages.

### D32 · Scores carry their reasons
**Requirement:** 5.7 evidence · 5.13 observability
Every candidate holds strings like `anchor mentions 'տեղեկատվական ամփոփագիր' (+50)`. Costs a
few lines; makes the HITL screen usable and lets a bad ranking be debugged from `log.jsonl`
alone.

### D33 · Scoring weights in YAML, including negative ones
**Requirement:** 5.3 · 5.12
The Armenian keywords are policy a bank-side reviewer should be able to read and correct.
Negative weights matter as much as positive: an archived tariff PDF («արխիվ») and a business
product are the two ways a plausible-looking document is the wrong one. `/individual/` vs
`/business/` in the URL path is a stronger discriminator than any keyword, because ACBA
separates its audiences structurally.

### D34 · Two floors, not one
**Requirement:** 5.11 bounded work
`min_score` drops weak candidates from the results; the higher `page_min_score` decides what is
worth *fetching* at all, because a loosely-matching page costs a network round-trip to rule out.

### D35 · Shared documents are exempt from the slug requirement
**Requirement:** 5.3
`loans-tariffs.pdf` legitimately covers every loan product, so requiring it to match the
product's own slug would discard the one document that corroborates both products.

### D36 · `defusedxml` for the sitemap
**Requirement:** 5.12 security
Stock XML parsers expand entities: a "billion laughs" document is a few hundred bytes on the
wire and gigabytes in memory. Any parse failure degrades to `[]`, because the sitemap is one
discovery route of three — and ACBA's real sitemap contains a malformed `<loc>` that must not
discard the other 587 entries.

### D37 · Seeds keep guaranteed crawl slots, and are flagged
**Requirement:** 5.3 · 5.7 provenance
A site full of loosely-matching pages must not evict the curated one from the budget. When
discovery finds nothing and seeds are used instead, every candidate is flagged `is_seed` with a
note, so a reviewer can tell live discovery from a configured fallback.

### D38 · Two close information summaries require review
**Requirement:** 5.10 "two plausible official PDFs are found and a reviewer selects the correct one"
ACBA publishes separate summaries for purchase and renovation mortgages, and they score 90 and
80. Picking the higher one automatically is a coin flip on which product's rates get reported.
Restricted to PDFs: sibling product pages routinely score alike and are not a real dilemma.

---

## Requirement coverage so far

| § | Requirement | Status |
|---|---|---|
| 5.1 | ADK agent | Phase 8 |
| 5.2 | Focused tools | Phase 8 (the functions they will wrap exist) |
| 5.3 | Official source discovery | ✅ D27–D38, D7 |
| 5.4 | PDF / OCR processing | Phase 4 |
| 5.5 | Chunking and RAG | Phase 5 |
| 5.6 | Structured extraction | Schema ✅ (D1–D5); extraction Phase 6 |
| 5.7 | Evidence and provenance | Model ✅ (D2, D4); populated Phase 6 |
| 5.8 | Deterministic validation | Invariants ✅ (D2, D5); normalizers Phase 6 |
| 5.9 | Change detection | Storage contract ✅ (D14, D21); diff Phase 7 |
| 5.10 | HITL | Triggers ✅ (D10, D15, D29, D38); reviewer UI Phase 7 |
| 5.11 | Error handling | ✅ D9, D20, D26, D36 |
| 5.12 | Security | ✅ D6, D7, D17–D19, D22–D23, D36 |
| 5.13 | Observability | ✅ D11, D32 |
| 5.14 | Testing | ✅ 155 tests, D24; evaluation dataset Phase 9 |
| 5.15 | Python engineering | ✅ D1, D13, D25 |
