# ACBA Tariff Monitoring Agent

An agent that monitors publicly available **ACBA Bank** product tariffs: it resolves a fuzzy
product name («սպառողական վարկ», "consumer loan", "ипотека") to a known product, finds the
official ACBA document, extracts the tariff fields **with source evidence**, validates them
deterministically, stores a snapshot, diffs it against the previous one, and escalates to a
human when it cannot decide safely.

Built with Python 3.11, Google ADK and Gemini.

> **Status: Phase 1 of 9 complete.** This phase contains contracts only — configuration, the
> tariff field registry, data models, errors and logging. There is no network access, no
> document processing and no model call yet. Later phases are listed at the bottom.

---

## Setup

```bash
# 1. Virtual environment (Python 3.11 — 3.14 has no reliable PyMuPDF/ADK wheels yet)
/opt/homebrew/bin/python3.11 -m venv .venv
source .venv/bin/activate

# 2. Dependencies
pip install -e ".[dev]"          # full stack
# Phase 1 alone only needs: pip install pydantic pydantic-settings pyyaml pytest

# 3. Configuration
cp .env.example .env             # then put your AI Studio key in GOOGLE_API_KEY
```

`.env` is gitignored; `.env.example` is the committed template and contains no secrets.

## Run the tests

```bash
pytest            # 24 tests in Phase 1
```

---

## Project layout

```
config/                        version-controlled policy (safe to read in a review)
  allowlist.yaml               which domains/schemes may ever be fetched
  products.yaml                monitored products + hy/en/ru synonyms + seed URLs
src/tariff_agent/
  __init__.py                  package docstring and version
  fields.py                    THE tariff field registry — everything derives from it
  models.py                    shared data contracts (Evidence, FieldValue, TariffExtraction)
  config.py                    env settings + typed YAML loaders
  errors.py                    exception hierarchy, one class per failure mode
  observability/
    logging.py                 JSON logging with a per-run correlation id
tests/
  test_phase1_contracts.py     registry, config and model-invariant tests
data/samples/                  committed fixtures (OCR sample page lands here in Phase 4)
pyproject.toml                 dependencies, pytest/ruff/mypy configuration
.env.example                   configuration template, no secrets
```

---

## What each file does, and why it exists

### `config/allowlist.yaml`
The only domains the agent may fetch: `acba.am` and `www.acba.am`, https only.
Exact hostnames, **no wildcards** — a wildcard like `*.acba.am` would let a user-content or
hijacked subdomain through. It lives in YAML rather than in code so a reviewer can see the
entire network surface of the project in six lines. The rule is enforced in Python (Phase 2),
never by the LLM.

### `config/products.yaml`
The two monitored products (`consumer_loan`, `mortgage`): official Armenian/English names,
synonyms in Armenian, English and Russian, plus `seed_pages` / `seed_documents`.
Synonyms feed fuzzy product resolution in Phase 3. Seeds are the fallback when live discovery
finds nothing; anything sourced from a seed gets flagged `is_seed` so a reviewer knows it was
not freshly discovered. Adding a third product is a YAML edit, not a code change.

### `src/tariff_agent/fields.py`
**The single source of truth for what a tariff is.** Ten `FieldSpec` entries (currency, term,
amount, nominal rate, effective rate, collateral, three fees, salary privileges), each with:

| attribute | used by |
|---|---|
| `id` | extraction keys, snapshot keys, diff keys — must never change once snapshots exist |
| `label_hy` / `label_en` | the business report |
| `kind` (`percent`, `amount`, `term`, `currency`, `fee`, `text`) | which normalizer runs (Phase 6) and how change magnitude is measured (Phase 7) |
| `required` | what counts as a completeness gap in validation |
| `query_terms` | the per-field RAG queries (Phase 5), Armenian first |

Adding an eleventh field should mean adding one entry here and touching nothing else. That
property is the whole point of the module.

### `src/tariff_agent/models.py`
The contracts passed between stages, and the place where two invariants are enforced rather
than trusted:

- **`Evidence`** — `document_name`, `source_url`, `page`, `section`, `quote`. Enough for a human
  to re-open the PDF and check the number themselves.
- **`FieldValue`** — `value` (verbatim from the document, or the `NOT_FOUND` sentinel),
  `normalized`, `evidence`, `status`, `confidence`. A validator makes "found" and "missing"
  mutually exclusive: a value without evidence is rejected, and a `NOT_FOUND` field may not
  carry evidence or a normalized value. This is the structural reason the agent cannot
  quietly report an invented rate.
- **`TariffExtraction`** — one product, one run. A validator requires *exactly* the registry's
  field ids: no field silently dropped, no field invented by the model.

No parsing, no I/O — models only.

### `src/tariff_agent/config.py`
Splits configuration in two on purpose:

- **`Settings`** (pydantic-settings) reads the environment / `.env`: `TARIFF_GEMINI_MODEL`,
  `TARIFF_LOG_LEVEL`, and `GOOGLE_API_KEY`. The Google variables are intentionally
  *unprefixed* because the google-genai SDK reads those exact names itself. The key is a
  `SecretStr`, so it is masked in reprs, logs and tracebacks. `has_api_key` lets offline demos
  and tests take the mock path instead of crashing.
- **`load_allowlist()` / `load_products()`** parse the YAML files into frozen pydantic models.
  A typo fails at load time with a `ConfigError` naming the file, instead of surfacing as odd
  behaviour halfway through a monitoring run.

### `src/tariff_agent/errors.py`
One exception per failure mode — `ConfigError`, `FetchError`, `DomainNotAllowedError`,
`DocumentError`, `ProductNotFoundError`, `SourceNotFoundError`, `ExtractionError`,
`ValidationFailedError`, `SnapshotError`, `ReviewRejectedError` — so callers branch on type
instead of string-matching messages. Phase 2 retries only transient `FetchError`s; Phase 8
converts these into `{"status": "error", ...}` tool results so the model never sees a
traceback and never gets fabricated data in place of a failure.

`NeedsReviewError` is the odd one out: it is not a bug but the pipeline correctly refusing to
decide alone. It carries a machine-readable `reason` and the `details` a reviewer needs.

### `src/tariff_agent/observability/logging.py`
Single-line JSON logs so a run can be reconstructed end to end and metrics (tool failures,
extraction completeness, HITL rate) can be computed without parsing English. A `run_id` is
kept in a `ContextVar` and injected into every line by the formatter, so it does not have to be
threaded through every function signature. Anything passed as `extra={...}` becomes a
top-level JSON key. **Model chain-of-thought is never logged** — only decisions and their
inputs and outputs.

### `tests/test_phase1_contracts.py`
Covers exactly what Phase 1 promises: the registry is complete, ordered and immutable; both
YAML files parse and every seed URL is on the allowlist; a missing or malformed config raises
`ConfigError`; the `NOT_FOUND`/evidence invariants reject every contradictory combination; and
the logger emits the run id and structured extras.

---

## Hard rules this project follows

1. Only public `acba.am` pages. No bypassing of login, CAPTCHA or anti-bot protection.
2. The LLM gets **no** filesystem, database, shell or arbitrary-URL access. Tools take ids
   produced by previous tools, never raw paths or URLs.
3. Never invent a tariff value — missing data is the explicit `NOT_FOUND` sentinel.
4. Never replace a failure with fabricated data.
5. No secrets in code or git; configuration via environment plus `config/*.yaml`.
6. Never log chain-of-thought.
7. Type hints and a docstring in every module.

## Roadmap

| Phase | Contents | Status |
|---|---|---|
| 1 | Skeleton, config, field registry, models, errors, logging | ✅ done |
| 2 | Safe HTTP client: allowlist on every redirect hop, size/MIME caps, bounded retries | next |
| 3 | Product resolution + official source discovery | |
| 4 | PDF/HTML processing, cleaning, OCR fallback | |
| 5 | Chunking + hybrid BM25/embedding RAG | |
| 6 | Gemini structured extraction + deterministic validation | |
| 7 | Snapshots, normalized diffing, human-in-the-loop | |
| 8 | ADK agent, pipeline and CLI | |
| 9 | Demos, evaluation dataset, architecture docs | |

## AI assistance disclosure

Claude Code (Opus 5) was used as a coding assistant while building this project: drafting
module scaffolding, tests and documentation from a phase-by-phase specification written by the
author. All architecture decisions, the phase plan and the review of every file are the
author's own.
