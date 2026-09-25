# ACBA Tariff Monitoring Agent

Monitors publicly available **ACBA Bank** loan tariffs. Give it a product name in Armenian,
English or Russian («սպառողական վարկ», "consumer loan", "ипотека") and it finds the official
ACBA document, extracts the ten tariff fields **with a source quote for each**, validates
them deterministically, stores a snapshot, and reports what changed since the last run —
escalating to a human when it cannot decide safely.

Python 3.11 · Google ADK · Gemini

> **Status: Phase 5 of 9.** A fuzzy product name resolves to a product, to its official
> ACBA sources, and those sources are parsed into clean pages, tables and sections and then
> indexed — so the system can now answer, per tariff field, *which passages state this, and
> is any of them actually relevant?* No extraction or model calls yet. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
> for the design and the roadmap.

---

## Setup

Requires **Python 3.11 or 3.12** (3.13+ has no reliable PyMuPDF / google-adk wheels yet).

```bash
python3.11 -m venv .venv          # macOS with Homebrew: /opt/homebrew/bin/python3.11
source .venv/bin/activate

pip install -e ".[dev]"

cp .env.example .env              # then add your AI Studio key to GOOGLE_API_KEY
```

`.env` is gitignored. `.env.example` is the committed template and holds no secrets.
Running without a key is supported — offline demos and tests use a mock path.

## Run the checks

```bash
pytest                            # 265 tests, none of which touch the network
ruff check src tests              # lint + docstring rules
mypy                              # strict type checking of src/
```

OCR in Phase 4 will additionally need Tesseract with Armenian:

```bash
brew install tesseract tesseract-lang     # provides hye + eng
```

## Demo

Phase 1.5 has no CLI yet; the pipeline arrives in Phase 8. What works today is
verifiable from the test suite, which covers the field registry, configuration loading,
the evidence invariants and forward migration of stored snapshots.

Planned demos (Phase 9): normal extraction · change detection · OCR fallback · a
human-in-the-loop review · two controlled failures (404 and timeout).

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | End-to-end flow, module map, what Gemini decides vs what code decides, test strategy |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every design decision by phase: options considered, why this one, which requirement it serves |

## Ground rules this project follows

1. Only public `acba.am` pages; no bypassing of login, CAPTCHA or anti-bot protection.
2. The LLM gets **no** filesystem, database, shell or arbitrary-URL access — tools take ids
   produced by earlier tools, never raw paths or URLs.
3. Never invent a tariff value: missing data is an explicit `NOT_FOUND`, and a value that
   cannot cite its source cannot even be constructed.
4. Never replace a failure with fabricated data.
5. No secrets in code or git; configuration via environment plus `config/*.yaml`.
6. Never log chain-of-thought.
7. Type hints and a docstring in every module.

## AI assistance disclosure

Claude Code (Opus 5) was used as a coding assistant throughout: drafting modules, tests and
documentation from a phase-by-phase specification, and reviewing the result. The phase plan
and the architecture were decided by the author with AI assistance; every file has been
reviewed by the author, who is responsible for the whole submission.
