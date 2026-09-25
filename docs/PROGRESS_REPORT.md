# ACBA Tariff Monitoring Agent — progress report

**Phases 1–5 of 9 complete:** contracts · guarded HTTP · discovery · document processing ·
retrieval.

| | |
|---|---|
| Tests | **265 passing**, none touching the network |
| Lint / types | `ruff` clean · `mypy --strict` clean on `src/` |
| Code | ~7,000 lines source (35 modules) · ~3,600 lines tests · 19 commits |
| Verified against | `acba.am`, live, at each phase boundary — including a measured retrieval comparison |

This report states what works and how it decides. Section 7 states what does not work yet,
including defects found by audit and bugs found by running the code against the real site.

---

## 1. What runs today

**Resolve a product name in any of three languages, find its official sources, and read them.**

```
«սպառողական վարկ» → consumer_loan (100/100, matched «սպառողական վարկ» [hy])
  PRIMARY  [88] html  /hy/individual/loan/consumer-loan--up-to-10mln
  SUPPORT  [73] pdf   loans-tariffs.pdf   ·   [63] html  category page
  → processed: 1 page · 17 sections · 19 rate mentions · 21.6%, 23.97%, 15.9%, 13.9%

"ипотека" → mortgage (100/100, matched «ипотека» [ru])
  PRIMARY  [90] pdf   loan info.pdf  ← «տեղեկատվական ամփոփագիր»
  → processed: 8 pages · methods = [ocr, pdf_text] · «Տևողություն՝ 12 - 240 ամիս»
    «Նվազագույն գումար՝ 1,000,000» · «Առավելագույն գումար՝ 500,000,000» · «11.9-12.5%»
```

Those Armenian strings are real output from the live pipeline, not examples.

And then answers, per tariff field, which passages state it:

```
consumer_loan   gate 9/10 · recall@4 9/10   (the 10th is application_fee, which the
                documents genuinely never state — the gate says so rather than guessing)
mortgage        gate 10/10 · recall@4 10/10 — values on pages 1–7 of the ամփոփագիր
```

**Not built yet:** Gemini extraction, validation, snapshots, change detection, the reviewer
interface, the ADK agent, the CLI, demos, the evaluation set. Phases 6–9.

---

## 2. Architecture

```
query ─► product_matcher ─► sources ─► SafeHttpClient ─► documents ─► rag ─► (Phase 6)
         synonyms+fuzzy     ranking     allowlist,caps    parse/OCR   chunk+search
              │                │            │                │           │
              └── config/products.yaml ─────┴── allowlist.yaml, discovery.yaml
```

| Package | Responsibility |
|---|---|
| `fields.py` | The ten tariff fields and what each stage needs to know about them |
| `models.py` | `Evidence`, `FieldValue`, `TariffExtraction` and the rules they enforce |
| `config.py` + `config/*.yaml` | Environment settings; policy as reviewable YAML |
| `http/` | The only code permitted to open a network connection |
| `discovery/` | Which product was meant; which sources are authoritative |
| `documents/` | Bytes → clean pages, tables and sections |
| `rag/` | Chunking, indexing, and answering "which passages state this field?" |
| `observability/` | Structured JSON logs, correlated per run, written per run to a file |

**What the model will decide (Phase 6):** which retrieved chunk answers a tariff field, and
what text to quote as its evidence. That is the whole of it.

**What code decides, never the model:** which product was meant · which domains may be fetched
· whether a redirect may be followed · whether a download is allowed by size and type · which
source is authoritative · which reading of a page is best · whether a quoted value truly
appears in the document · whether a value is well-formed · what counts as a meaningful change
· whether a human is needed.

---

## 3. Phase 1 — Contracts

The tariff field registry, the data contracts, typed configuration, the exception hierarchy,
structured logging. No behaviour.

**The decision that matters most: a tariff value that cannot cite a source cannot be
constructed.** `FieldValue(value="13.5%", status=FOUND)` with no evidence *raises*. We also
instruct the model to emit `NOT_FOUND`, but the validator is what is load-bearing: prompt
adherence drifts between model versions, a validator does not.

Supporting decisions: one field registry, so adding an eleventh field touches one file ·
`NOT_FOUND` as a string sentinel, so absence survives JSON → SQLite → report · values kept
verbatim **and** normalized, so the report shows what the bank wrote while the diff compares
canonical forms · an extraction must cover exactly the registry, so a dropped field and a field
the bank does not offer never look alike · policy in committed YAML, secrets in the environment
· exact hosts, no wildcard subdomains · one exception per failure mode, so retries branch on
type rather than on English text · JSON logs with a run id in a `ContextVar`.

**Phase 1.5** added three contract fixes before anything depended on them: snapshot versioning
with a lenient loader for *stored* data only; a field-status split (`found / not_found /
unverified / conflict`) with run-level review kept separate; and `confidence` computed
deterministically, left `None` until Phase 6 rather than defaulted to a meaningless `1.0`.

---

## 4. Phase 2 — The guarded HTTP layer

Every fetch, in order: policy before the socket → separate connect/read timeouts → **manual
redirect loop** (≤5 hops, allowlist re-checked on each) → streamed 25 MB cap on bytes actually
received → content type checked against both header and magic bytes → per-host pacing →
bounded exponential backoff → one structured log line.

- **Manual redirects** because `follow_redirects=True` would follow `acba.am → cdn.elsewhere.com`
  silently, making the allowlist decorative. The test asserts the transport was called *once*.
- **Retries only where retrying helps**: connection errors, timeouts, 408/425/429/5xx. Not 4xx —
  a 404 is an answer. 403 is logged `access_blocked` and never retried.
- **The cache revalidates, it does not short-circuit.** A monitor that serves cached bytes
  without asking the server cannot detect a change. Every run sends `If-None-Match`; a 304
  reuses the bytes. `retrieved_at` and `checked_at` are separate, so "last changed" and "last
  verified" are different facts.
- **robots.txt**: 4xx means no rules exist → proceed; 5xx means permission is unknown → stop.
  Fetched through the same client, so there is one network door.

---

## 5. Phase 3 — Discovery

**Product resolution is deterministic — no Gemini.** A wrong resolution silently reports
another product's tariffs, so the decision must be reproducible offline and explainable in one
log line. Three layers, because two are not enough:

1. a curated hy/en/ru synonym dictionary — the semantic layer;
2. rapidfuzz `token_set_ratio` — typos, word order, extra words;
3. a penalty for unsupported products — because `token_set_ratio("business mortgage",
   "mortgage")` is **100**, and ACBA really does sell a business mortgage.

Bands: ≥85 with a ≥10 lead resolves; a close race is ambiguous and goes to a human; below 60 is
not found. Bare `"loan"` therefore escalates instead of being guessed.

**Source ranking returns a primary source plus supporting ones.** The consumer product links no
«ամփոփագիր» and states its rates in its own page, while the shared `loans-tariffs.pdf`
outscores it on keywords — so "PDF beats HTML" reports the wrong document. Role is decided
before score: only a candidate with a primary signal may lead; a document covering every loan
product can corroborate but never lead. Every candidate carries the reasons for its score
(`anchor mentions «տեղեկատվական ամփոփագիր» (+50)`), which are logged and shown to a reviewer.

Two refinements came from reading real output: a product may pin its `canonical_page` (ACBA's
consumer *category* page lists several loans and states no effective rate at all), and a product
may name neighbouring products in `exclude_terms` (the renovation-mortgage summary is official,
wrong, and was stopping *every* run for a decision already made).

Security: the sitemap is parsed with `defusedxml`, and a test feeds it a billion-laughs
document.

---

## 6. Phase 4 — Document processing

**OCR is a peer strategy, not a fallback**, because the authoritative mortgage document's text
layer is broken four different ways:

| Reading | Result |
|---|---|
| flat `get_text()` | 1,593 chars, **one word per line** |
| `get_text("blocks")` | words **glued** together |
| `get_text("dict")` | **53 of 1,593 characters** |
| `get_text("words")` | `1,000,000` came back as `0,000,000` |
| **OCR, 300 dpi** | **91.9% confidence**, amounts intact |

The parse does not *fail* here — it returns plausible text with a digit missing, which is worse
than failing. So every page is read by every strategy, scored on one scale, and the best is
kept; the method is recorded per page. Tesseract confidence is a **gate**, never added to the
score; a tie goes to the parser, which reads «և» correctly where Tesseract reads «ն».

Cleaning is deliberately timid: only unambiguous thousand groups are rejoined, because
«10 59 10 10» in the real document is a phone number; «և» is preserved exactly (144 occurrences,
«եւ» zero); table cells never see the prose rules. Detected tables must look like tables —
PyMuPDF reports `['ն','և','','']` on the graphics-heavy page. `doc_id` is the content hash, so
the tariff PDF served from three URLs is one document.

---

---

## 6.5 Phase 5 — Retrieval

Answers one question per tariff field: **which passages state this, and does any of them
really?** The second half matters as much as the first — a retriever always returns its nearest
neighbour, and an extractor will quote whatever it is given.

**Armenian first.** «տոկոսադրույք» occurs in five inflected forms; a light suffix stripper
collapses them, and it is the largest single accuracy contributor in the phase. Tesseract's
«և»→«ն» confusion is folded for matching only — **measured across 26,054 corpus tokens and
1,591 distinct folded forms to collide with no real word.** Numbers stay whole, because «13,5%»
and «1,000,000» are what is being looked for.

**Chunks protect the evidence trail.** None spans a page (evidence cites one page), each
carries exact offsets into the page text so a quote can be *verified* rather than guessed at,
and a split never falls inside a number. A table is its own chunk carrying its heading **inside
the text** — «0.5% | ամսական» means nothing until «Սպասարկման վճար» sits above it, and the model
sees text, not metadata.

**Ranking fuses BM25 and embeddings by rank**, with two corrections the real documents forced:
query phrasings are weighted canonical-first, and a third ranking promotes chunks that mention
the field *and* contain a value of its declared kind — without which BM25 ranked a marketing
banner above the rate table it advertises.

**The relevance gate is lexical, by measurement, not preference:**

| field on the consumer page | max cosine | actually stated? |
|---|---|---|
| `application_fee` | **0.687** | **no** |
| `collateral` | 0.687 | yes |
| `currency` | 0.682 | yes |

Gemini's similarities sit in a 0.63–0.81 band regardless of relevance, so no floor separates
present from absent. Embeddings rank; only a lexical hit — requiring the term's *identifying*
token, not just its common words — decides that an answer exists.

**Both modes measured on the real documents:**

| | gate | recall@4 | top-1 |
|---|---|---|---|
| BM25 only | **19/20** | 18/20 | **14/20** |
| Gemini + BM25 | 18/20 | **19/20** | 12/20 |

Semantic ranking buys one field of recall and costs one of gate accuracy and two of top-1, for
an API dependency and a per-run cost. On this corpus, with query terms written in the bank's own
vocabulary, lexical retrieval is at least as good. The hybrid remains the default when a key is
configured — but the number is reported rather than assumed, and a bank whose documents
paraphrase more would likely invert it.

Primary and supporting sources are searched separately, because ACBA's own documents disagree:
the 2023 mortgage summary states 11.9–12.5% where the current product page says 13.75–14.5%.
Phase 6 needs to see that as a `conflict`, not average over it.

## 7. What running it against the real site found

Six defects that reasoning alone did not catch. Each is fixed, with a test that fails if it
returns. This is offered as evidence of method, not of foresight.

| Phase | Found | Consequence had it shipped |
|---|---|---|
| 2 | Validators dropped across ACBA's `www → apex` redirect | A 1 MB PDF re-downloaded on every run; change detection never seeing a 304 |
| 2 | 30 s read timeout too short for that PDF | A retry on every single fetch |
| 3 | Ranking by score alone | The **wrong document** chosen as the consumer loan's source |
| 4 | Cleaning ran before scoring | Corrupted text scored **1.00**, beat OCR, and `50 0,000,000` was accepted as an amount |
| 4 | Line joining before furniture removal | Repeated headers unmatchable, so never removed |
| 4 | "Densest container" HTML heuristic | **19 of 21** interest-rate mentions silently discarded |
| 5 | Query terms written from the field's *title* | `salary_privileges` matched nothing; ACBA writes it differently |
| 5 | BM25 ranked a marketing banner above the rate table | Extraction shown a slogan instead of the tariff |
| 5 | A similarity floor for the relevance gate | A fee the documents never state reported as present |
| 5 | Embedding rate limit dropped a whole product's vectors | Silent loss of semantic ranking, all-or-nothing |
| 4 | PDFs had no section detection at all | Every PDF's evidence would carry an empty section |

A separate audit of the code found three more, also fixed: an exception-hierarchy accident that
let a 503 on `robots.txt` be swallowed as "skip this page" (defeating a documented guarantee); a
documentation claim about `log.jsonl` that nothing implemented; and a sitemap index that would
have been crawled as if it were a list of pages.

---

## 8. Testing

223 tests, none touching the network. `httpx.MockTransport` for HTTP, injected `sleep` for
retries and pacing, mocked Tesseract for OCR logic plus one real-binary test that auto-skips.

| Area | Tests |
|---|---|
| Contracts, config, snapshot migration, logging | 52 |
| URL policy — lookalike hosts, schemes, encoding | 19 |
| HTTP client — retries, caps, redirects, revalidation, pacing, offline | 30 |
| robots.txt — allow, disallow, 5xx stops the run | 6 |
| Product matching — hy/en/ru, typos, ambiguity, unsupported products | 31 |
| Discovery — scoring, primary selection, budgets, sitemap traps, HITL | 34 |
| Document cleaning — each rule alone | 16 |
| Text quality — every failure mode | 7 |
| Document processing — strategies, PDF, HTML, tables, OCR | 28 |
| RAG text — stemming, folding, the identifying-token rule | 13 |
| Chunking — page boundaries, offsets, tables, metadata | 10 |
| Retrieval — fusion, the gate, index reuse, degradation | 19 |

Fixtures are trimmed **real** ACBA pages, each headed with its source URL and fetch date. Real
markup is what caught the `http://` PDF link, the three URLs serving one document, and the
container bug. The PDF fixtures are synthetic — the real page cannot be trimmed without
destroying the defect it demonstrates — and the OCR test reads a real scanned ACBA page.

---

## 9. Limitations

**Design limits, deliberate:** `Crawl-delay` is not read (pacing is a fixed per-host interval) ·
the cache has no size limit, TTL or eviction, and writes are not atomic · discovery does not
crawl beyond the pages it selects · links rendered by JavaScript are invisible, and that failure
would be silent · scoring weights and matching thresholds are hand-tuned against two products at
one bank · unlisted transliterations will not resolve · value kinds are coarse (one `FEE`
normalizer must handle percentages, amounts and «անվճար») · single-threaded and synchronous.

**Document limits:** content genuinely identical at the edge of every page is indistinguishable
from a footer and is removed · a table detected on a graphics-heavy page may be refused as junk
when it is real.

**Retrieval limits:** the Armenian stemmer is rule-based and does not model vowel alternation,
so «ամփոփագիր» and its genitive «ամփոփագրի» do not meet, nor «ուսումնասիրության» and
«ուսումնասիրման» · fusion weights and the 60% term-coverage threshold were tuned against two
products and are not yet measured by an evaluation set · there is no reranker, which is the
honest production next step · the «և»→«ն» folding is collision-free *in this corpus*, not in
general.

**Scope limits:** two products at one bank; `consumer_loan` means one purchasable product, not
the family · Armenian-language sources only · **a reviewer's decision is not remembered**, so a
genuinely new rival document will escalate on every run until `products.yaml` is edited.

**Risk ahead:** the model is now `gemini-2.5-flash` (switched from flash-lite before Phase 6,
since Armenian tariff tables are the hardest thing the system does).

---

## 10. Requirement coverage

| § | Requirement | Status |
|---|---|---|
| 5.1 | ADK agent | Phase 8 · the deterministic/LLM split is already drawn |
| 5.2 | Focused tools | Phase 8 · the functions they will wrap exist and are pure |
| 5.3 | Official source discovery | ✅ |
| 5.4 | PDF, OCR and document processing | ✅ |
| 5.5 | Chunking and RAG | ✅ chunking with §5.5 metadata, hybrid retrieval, relevance gate, both modes measured |
| 5.6 | Structured extraction | Schema ✅ · extraction Phase 6 |
| 5.7 | Evidence and provenance | Model, sections and page rules ✅ · populated Phase 6 |
| 5.8 | Deterministic validation | Invariants ✅ · normalizers Phase 6 |
| 5.9 | Change detection | Storage contract, revalidation, `checked_at` ✅ · diff Phase 7 |
| 5.10 | Human-in-the-loop | Triggers ✅ · reviewer interface Phase 7 |
| 5.11 | Error handling | 🟡 network, HTTP, robots, discovery, parse, OCR, embedding failure and irrelevant retrieval ✅ · model and snapshot failures remain |
| 5.12 | Security | 🟡 allowlist, redirects, caps, robots, XML safety, render cap, secrets ✅ · tool least-privilege and prompt injection arrive with tools and prompts |
| 5.13 | Observability | 🟡 structured logs, per-run files, run correlation, per-decision reasons ✅ · run metrics remain |
| 5.14 | Testing | 🟡 265 tests ✅ · evaluation dataset Phase 9 |
| 5.15 | Python engineering | ✅ structure, type hints, config, logging, tests, pyproject, README, git history |

---

## 11. What comes next

| Phase | Contents |
|---|---|
| 5 | Section-aware chunking; hybrid BM25 + embeddings fused with RRF; «և»/«ն» folding |
| 6 | Gemini structured extraction with quote verification; deterministic normalization and validation |
| 7 | SQLite snapshots, normalized diffing, the human-in-the-loop gate |
| 8 | The ADK agent over the same tool functions, plus the CLI |
| 9 | Demos, the evaluation dataset and its results, remaining documentation |

Further detail: [ARCHITECTURE.md](ARCHITECTURE.md) for the design,
[DECISIONS.md](DECISIONS.md) for every decision with the options rejected for each.
