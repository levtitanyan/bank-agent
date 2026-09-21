# ACBA Tariff Monitoring Agent — progress report

**Status:** phases 1–3 of 9 complete (contracts · guarded HTTP · discovery).
**Date:** 2026-09-21 · **Tests:** 172 passing, none touching the network · **Lint:** ruff clean ·
**Types:** mypy strict clean on `src/` · **Code:** ~3,400 lines source, ~1,900 lines tests · 14 commits.

This report states what works, how it decides, and — in the second half — everything that does
not work yet, including three defects found while preparing it.

---

## 1. What the system does today

Given a product name in Armenian, English or Russian, it resolves the product, finds the
official ACBA sources for it, and ranks them with the evidence for the ranking. Verified live
against acba.am:

```
«սպառողական վարկ» → consumer_loan (resolved, 100/100, matched «սպառողական վարկ» [hy])
  PRIMARY  [88] html  /hy/individual/loan/consumer-loan--up-to-10mln
      + is the product's own page (+25)  + path contains '/individual/' (+20)
      + url slug matches the product (+20)  + page states rates, 31 mentions (+15)
  SUPPORT  [73] pdf   loans-tariffs.pdf        [63] html  /loans/consumer-loans (category)

"ипотека" → mortgage (resolved, 100/100, matched «ипотека» [ru])
  PRIMARY  [90] pdf   loan info.pdf  ← «տեղեկատվական ամփոփագիր»
      + anchor mentions «տեղեկատվական ամփոփագիր» (+50)  + url contains 'loan info' (+30)
  SUPPORT  [88] html  /hy/individual/loan/purchase-mortgage    [73] pdf  loans-tariffs.pdf
```

Not yet built: opening those documents, retrieval, extraction, validation, snapshots, the
diff, the reviewer interface, and the ADK agent itself. Phases 4–9.

---

## 2. Architecture in one page

```
query ──► product_matcher ──► sources ──► SafeHttpClient ──► (Phase 4: documents)
          synonyms+fuzzy      ranking      allowlist, caps
             │                   │            │
             └── config/products.yaml ────────┴── config/allowlist.yaml, discovery.yaml
```

Five packages, one direction of dependency, no cycles:

| Package | Responsibility |
|---|---|
| `fields.py` | The ten tariff fields and everything each stage needs to know about them |
| `models.py` | `Evidence`, `FieldValue`, `TariffExtraction` and the rules they enforce |
| `config.py` + `config/*.yaml` | Environment settings; allowlist, products and scoring as reviewable policy |
| `http/` | The only code permitted to open a network connection |
| `discovery/` | Which product was meant, and which sources are authoritative for it |
| `observability/logging.py` | Structured JSON logs correlated by run id |

**What the model will decide (Phase 6):** which retrieved chunk answers a tariff field, and
what text to quote as evidence. That is all.

**What code decides, never the model:** which product was meant · which domains may be
fetched · whether a redirect may be followed · whether a download is allowed by size and type ·
which source is authoritative · whether a quoted value truly appears in the document · whether
a value is well-formed · what counts as a meaningful change · whether a human is needed.

---

## 3. Phase 1 — Contracts

**Delivered:** the tariff field registry, the data contracts and their invariants, typed
configuration, the exception hierarchy, structured logging. No behaviour.

### The decision that matters most

**A tariff value that cannot cite a source cannot be constructed.**

| Option | Why not |
|---|---|
| Instruct Gemini to emit `NOT_FOUND` and trust it | Prompt adherence drifts between model versions |
| Validate afterwards, log a warning | A warning still lets an unsourced number into the report |
| **Chosen: a Pydantic validator rejecting the illegal state** | `FieldValue(value="13.5%", status=FOUND)` with no evidence **raises** |

Both are done — the model is instructed too — but the validator is what is load-bearing.

### The rest, briefly

- **One field registry** (`fields.py`): each of the ten fields carries its id, Armenian and
  English labels, value kind, whether it is required, and its retrieval query terms. Four
  later stages read from it, so adding an eleventh field touches one file. *Cost:* the value
  kinds are coarse — one `FEE` normalizer must handle `"0.5%"`, `"5,000 AMD"` and «անվճար».
- **`NOT_FOUND` is a string sentinel, not `None`**, so absence survives JSON → SQLite → report
  and cannot be confused with "not looked at yet".
- **Values are kept verbatim *and* normalized**: the report shows what the bank wrote
  («մինչև 60 ամիս»), the diff compares the canonical form, so `"10 000 000 AMD"` and
  `"10,000,000 AMD"` raise no false alert.
- **An extraction must cover exactly the registry** — a dropped field and a field the bank does
  not offer must never look alike, and the model cannot invent fields of its own.
- **Policy in committed YAML, secrets in the environment.** A reviewer sees the entire network
  surface in six lines of YAML; the API key is a `SecretStr`, asserted by test never to appear
  in a repr or a dump.
- **Exact hosts, no wildcards.** `*.acba.am` would admit any hijacked or user-content subdomain.
- **One exception per failure mode**, so retry logic branches on type rather than on English
  message text. `NeedsReviewError` is deliberately *not* a failure: it carries the reason and
  the evidence a reviewer needs.
- **JSON logs with a run id in a `ContextVar`**, so metrics are computable without parsing prose
  and `run_id` does not pollute every function signature. Chain-of-thought is never logged.

### Phase 1.5 — three contract fixes before anything depended on them

1. **Snapshot versioning.** Adding an eleventh field would otherwise break every stored
   snapshot and crash the diff. `from_stored()` backfills unknown-to-old fields as `NOT_FOUND`
   and drops fields no longer in the registry. Leniency is confined to that one method — the
   strict constructor still governs fresh model output, asserted by test.
2. **Field status split.** `found / not_found / unverified / conflict`. Whether the *run* stops
   for a human is a separate, run-level decision, not a field's property.
3. **Confidence is computed, and `None` until it is.** LLM self-reported confidence is not
   calibrated, and a placeholder `1.0` would look authoritative while meaning nothing. Phase 6
   computes it from quote-match score, document quality and retrieval score.

---

## 4. Phase 2 — The guarded HTTP layer

**Delivered:** `url_policy.py` (pure allowlist functions), `client.py` (`SafeHttpClient`),
`robots.py`. 49 tests, all via `httpx.MockTransport`.

Enforced on every fetch, in order: policy before the socket → separate connect/read timeouts →
manual redirect loop (≤5 hops, allowlist re-checked on each) → streamed 25 MB cap on bytes
actually received → content type checked against both header and magic bytes → bounded
exponential backoff with jitter → one structured log line.

### Manual redirects

`httpx.follow_redirects=True` is one parameter and silently wrong: it would follow
`acba.am → cdn.elsewhere.com` and return the bytes with no indication the domain changed,
making the allowlist decorative. The manual loop costs ~15 lines; the test asserts the
transport was called **once**, which is the only way to prove the second request never happened.

### Retries

Retryable: connection errors, timeouts, 408, 425, 429, 5xx. **Not 4xx** — a 404 is an answer.
403 is logged as `access_blocked` and never retried: an access decision is respected, not
hammered. `Retry-After` is honoured but capped, so a server cannot stall a monitoring run.

### The cache revalidates; it does not short-circuit

| Option | Why not |
|---|---|
| Serve cached bytes when present | **A monitor that never asks the server cannot detect a change** — the cache would defeat the product |
| Never cache | Re-downloads a 1 MB PDF on every run and demo |
| **Chosen: always request with `If-None-Match`/`If-Modified-Since`; 304 reuses the bytes** | One cheap round-trip, no re-download, and a positive statement that nothing changed |

**Found by a live run, not by reasoning:** ACBA permanently redirects `www.acba.am → acba.am`,
and dropping the validators across that hop silently re-downloaded 1 MB every run. They are now
re-attached only when the redirect target matches the cached final URL, so a 304 from a
*different* resource still means nothing.

### robots.txt

4xx means no rules exist → proceed. 5xx or timeout means permission is unknown → stop, rather
than resolving doubt in our own favour. Blanket fail-open ignores a real signal; blanket
fail-closed makes the agent hostage to a file most servers do not have. robots.txt is fetched
**through the same client**, so there is exactly one network door — the recursion is broken with
a flag and a two-line `Protocol`.

### Testability as a design constraint

`transport` and `sleep` are constructor parameters. Patching `time.sleep` globally is fragile
and invisible; explicit seams keep 25 client tests offline and instant, and let tests assert
*how many* requests were made.

---

## 5. Phase 3 — Discovery

**Delivered:** `product_matcher.py`, `sitemap.py`, `sources.py`, plus scoring policy in
`config/discovery.yaml`. 60 tests against trimmed copies of real ACBA pages.

### Product resolution is deterministic — no Gemini

A wrong resolution silently reports *another product's* tariffs, and the decision must be
reproducible offline and explainable in one log line. Three layers, because two are not enough:

1. **A curated multilingual synonym dictionary** — the semantic layer. No string metric
   discovers that «հիփոթեք», "mortgage" and "ипотека" are one product.
2. **Fuzzy matching** (rapidfuzz `token_set_ratio`) — the typo and word-order layer.
3. **A penalty for unsupported products** — because neither of the above can express *"we do
   not monitor that"*. `token_set_ratio("business mortgage", "mortgage")` is **100**: set
   matching ignores the extra token, and ACBA really does sell a business mortgage.

Bands: ≥85 **with a ≥10 lead** resolves; 60–85 or a close race is ambiguous and goes to a human;
below 60 is not found. The lead requirement is what makes bare `"loan"` escalate rather than
picking one of two equally-scoring products. Resolution returns a *status object*, never an
exception — the ambiguous case is data a reviewer needs, and agent tools must not raise into the
model.

### Source ranking: primary **plus** supporting

Two properties of the real site forced this shape:

- **The authoritative source is not always a PDF.** The consumer product links no «ամփոփագիր»
  and states its rates in the page itself, while the shared `loans-tariffs.pdf` outscores it on
  keywords. A "PDF beats HTML" rule reports the wrong document.
- **Some documents are shared.** `loans-tariffs.pdf` covers every loan product, so it is exempt
  from the product-slug requirement — and, being product-agnostic, can corroborate but never lead.

So **role is decided before score**: only a candidate with a primary signal (an information
summary, or a page that states rates itself) may lead. Supporting sources are then ranked by
score alone, so the shared PDF is not buried beneath sibling pages.

Every candidate carries the reasons for its score (`anchor mentions «տեղեկատվական ամփոփագիր»
(+50)`), which are logged and shown to a reviewer.

### Negative signals matter as much as positive ones

An archived tariff PDF and a business-product page are the two ways a plausible-looking document
is the wrong one. ACBA separates its audiences structurally — `/hy/individual/`, `/hy/business/`,
`/hy/agro/` — so a path-segment penalty is more reliable than any keyword.

Two refinements came from reviewing real output:

- **`canonical_page` per product.** Discovery first chose the consumer *category* page, which
  lists several loans, shows four headline percentages and states no effective rate at all — one
  honest set of ten tariff fields cannot come from it. The monitored product is now named
  exactly: the unsecured consumer loan up to 10M AMD.
- **`exclude_terms` per product.** ACBA's renovation-mortgage summary is a real official
  document and a real distraction; it sat inside the ambiguity band and stopped *every* run for a
  decision already made. Escalation that repeats nightly is an alarm nobody reads. It is now
  excluded for the mortgage specifically — not globally, since the term is only negative
  *relative to* the purchase mortgage. A test pins that an unrecognised rival still escalates.

### Security in this phase

The sitemap is parsed with `defusedxml`: stock XML parsers expand entities, and a
"billion laughs" document is a few hundred bytes on the wire and gigabytes in memory. A test
feeds the parser exactly that. Any parse failure degrades to an empty list — ACBA's real sitemap
contains a malformed `<loc>`, which must not discard the other 587 entries.

---

## 6. Testing approach

168 tests, none touching the network.

| Area | Tests | What they defend |
|---|---|---|
| Contracts | 49 | Registry shape, config loading, every illegal value/status/evidence combination, snapshot migration |
| URL policy | 19 | `acba.am.evil.com`, `evil-acba.am`, plain http, ports, relative resolution, percent-encoding |
| HTTP client | 25 | 404/403 not retried, 5xx retried then succeeds, capped `Retry-After`, oversize aborted, mislabelled content rejected, off-domain redirect refused, 304 revalidation, offline mode |
| robots.txt | 5 | Disallowed path refused, 404 allows, 5xx stops, fetched once per host |
| Product matching | 31 | hy/en/ru + transliterations, typos, ambiguity, business products refused, NFC equivalence, determinism |
| Discovery | 29 | Sitemap traps and XML bombs, scoring, primary selection, crawl budget, seed fallback, HITL escalation |

HTML and sitemap fixtures are **trimmed copies of real ACBA pages**, each headed with its source
URL and fetch date. Invented markup tests the parser against its author's assumptions; real
markup caught an `http://` PDF link on a live page and the fact that ACBA serves one document
from three different URLs.

---

## 7. Limitations

### 7.1 Defects found by audit — all fixed

An audit while preparing this report found three defects. All three are fixed, each in its own
commit with a test that fails if the defect returns.

| # | Defect | Effect | Resolution |
|---|---|---|---|
| 1 | `RobotsUnavailableError` subclassed `FetchError`, and discovery catches `FetchError` per page so one broken page does not end a crawl | Together these **defeated a documented security guarantee**: a 503 on robots.txt was swallowed as "skip this page" and the crawl continued into a site we had no permission to read | Now a direct `TariffAgentError`, outside the hierarchy those handlers catch. A test asserts it is *not* a `FetchError`, so the inheritance cannot be reintroduced silently. `RobotsDisallowedError` stays a `FetchError` — skipping a disallowed page is correct |
| 2 | `log.jsonl` was referenced in the decision log, but nothing wrote a log file | A documentation claim the code did not support | `configure_logging(runs_dir=…)` arms per-run files at `data/runs/<run_id>/log.jsonl`, same formatter, handler removed when the run ends. Off unless armed |
| 3 | `fetch_sitemap_urls()` returned a `<sitemapindex>`'s entries as if they were page URLs | Latent — ACBA publishes a flat `<urlset>` — and a real bug at any bank that does not | The parser now reports `is_index` as data rather than leaving callers to guess. An index is followed one level through the same guarded client, capped at ten children; a nested index is refused |

Two further gaps named below were closed at the same time: requests to one host are now paced
(default 1 s, per host, configurable), and `FetchResult` carries `checked_at` beside
`retrieved_at`, so "last verified" and "last changed" are no longer the same number.

### 7.2 Design limitations (deliberate, with reasons)

- **`Crawl-delay` is not read.** Pacing is a fixed configured interval per host (default 1 s);
  a robots.txt asking for more is not honoured.
- **The cache is unbounded.** No size limit, TTL or eviction — currently 8.3 MB across 30
  entries. Writes are not atomic, so a crash mid-write can orphan a body file (harmless: the
  next read treats it as absent and re-fetches).
- **Duplicate documents are deduplicated by URL, not content.** ACBA serves
  `loans-tariffs.pdf` from three URLs; the content hash needed to collapse them is already
  computed but not yet used.
- **Discovery does not crawl beyond the pages it selects.** A document linked only from a page
  outside the sitemap and seed list will be missed.
- **Links rendered by JavaScript are invisible.** ACBA's pages are server-rendered enough for
  this to work today; a redesign could break it, and the failure would be silent.
- **Scoring weights and matching thresholds are hand-tuned** against two products at one bank.
  They are policy in YAML, not code, but nothing yet measures whether they generalise.
- **Unlisted transliterations will not resolve** — transliterations are dictionary entries, not
  a transliteration engine.
- **Value kinds are coarse.** One `FEE` normalizer will have to handle percentages, amounts and
  «անվճար».
- **Single-threaded and synchronous**, by choice: ADK tools are called sequentially, and
  concurrency would add failure modes without benefit at this scale.
- **The first diff after adding a tariff field will show `NOT_FOUND → value` as a change.**
  Arguably correct; the stored schema version explains it.

### 7.3 Product-scope limitations

- **Two products, one bank.** `consumer_loan` now means one purchasable product (unsecured, up
  to 10M AMD), not the family. Monitoring the others is configuration, not code.
- **Armenian-language sources only.** English and Russian appear in queries, not in the
  documents used.
- **A reviewer's decision is not remembered.** A genuinely new rival document will escalate on
  every run until `products.yaml` is edited. Persisting HITL outcomes belongs with the Phase 7
  snapshot store.

### 7.4 Not built yet (phases 4–9)

Document parsing and OCR · chunking and RAG · Gemini extraction · deterministic normalization
and validation · snapshots and change detection · the reviewer interface · the ADK agent and its
tools · the CLI · demos · the evaluation dataset.

Consequently these assignment requirements are **partial**: 5.11 error handling (document, OCR,
model and snapshot failures remain), 5.12 security (tool least-privilege and prompt-injection
defence only exist once tools and prompts do), 5.13 observability (the run metrics themselves),
5.14 testing (the evaluation dataset).

### 7.5 Known risk ahead

The configured model is now `gemini-2.5-flash`, switched from `flash-lite` before Phase 6:
extraction from Armenian tariff tables is the hardest thing the system does, and flash-lite is
weakest at exactly that. It remains one environment variable, so both can be compared in a demo.

A second risk surfaced during Phase 4 reconnaissance: **the mortgage information summary's PDF
text layer is unreliable.** Flat extraction yields one word per line; block extraction glues
words together; structured extraction returns 53 of 1,593 characters; and word extraction
dropped a digit from an amount. OCR at 300 dpi reads the same page cleanly at 91.9% mean
confidence. The document pipeline is therefore being designed with OCR as a peer strategy rather
than a last resort — which is honest, and demonstrates requirement 5.4 on a real document that
genuinely needs it.

---

## 8. What comes next

| Phase | Contents |
|---|---|
| 4 | PDF text and tables, Armenian-aware cleaning, quality scoring, OCR, HTML as a document |
| 5 | Section-aware chunking, hybrid BM25 + embedding retrieval |
| 6 | Gemini structured extraction with quote verification, deterministic normalization and validation |
| 7 | SQLite snapshots, normalized diffing, the human-in-the-loop gate |
| 8 | The ADK agent over the same tool functions, plus the CLI |
| 9 | Demos, the evaluation dataset and its results, remaining documentation |

Fuller detail: [ARCHITECTURE.md](ARCHITECTURE.md) for the design, [DECISIONS.md](DECISIONS.md)
for all 61 decisions with the options rejected for each.
