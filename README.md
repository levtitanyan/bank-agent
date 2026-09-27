# ACBA Tariff Monitoring Agent

Monitors publicly available **ACBA Bank** loan tariffs. Give it a product name in Armenian,
English or Russian («սպառողական վարկ», "consumer loan", "ипотека") and it finds the official
ACBA document, extracts the ten tariff fields **with a source quote for each**, validates
them deterministically, stores a snapshot, and reports what changed since the last run —
escalating to a human when it cannot decide safely.

Python 3.11 · Google ADK · Gemini

> **Status: complete — phases 1–9.** A fuzzy product name resolves to a product, to its official ACBA
> sources; those are parsed into clean pages, tables and sections, indexed, and read by Gemini
> into the ten tariff fields — each carrying a quote verified against the passage it came
> from, normalized deterministically, and validated. Runs are stored as snapshots and diffed
> against the previous one, and anything large, conflicting or ambiguous stops for a human
> whose decision is remembered.
>
> An ADK agent reaches the same parts through six tools and decides which steps a question
> needs; a CLI exposes both paths.
>
> **14 of 20 tariff fields found** through the path a reviewer actually runs, measured by
> [eval/results-live.md](eval/results-live.md); 17/20 when the two authoritative documents are
> handed to it directly rather than discovered. The gap is explained in
> [docs/LIMITATIONS.md](docs/LIMITATIONS.md) §2.5. 431 tests.
> Five runnable demos and an 18-item evaluation set are in [demos/](demos/) and [eval/](eval/).
> See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and the roadmap, and
> [docs/LIMITATIONS.md](docs/LIMITATIONS.md) for what it gets wrong.

---

## Quick start — everything I can run

### 1 · Setup

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                      # add GOOGLE_API_KEY for the agent
brew install tesseract tesseract-lang     # OCR with Armenian; macOS
```

*Without a key everything below still works except the two agent commands, which refuse with
a message rather than failing.*

### 2 · The chat UI — the primary demo

```bash
tariff-agent run "consumer loan"   # seed a snapshot first, so the zero-fetch answer works
adk web src                        # NOT `adk web .`
```

Open the printed URL and pick **`tariff_agent.agent`**, then ask:

```text
Ի՞նչ է սպառողական վարկի տարեկան անվանական տոկոսադրույքը:
What are the current mortgage tariffs, and did anything change?
Какая процентная ставка по потребительскому кредиту?
```

*You will see each tool call and its result appear one at a time. The first question takes two
tools and fetches nothing — it answers from the snapshot. The second takes five and reads the
bank, because nothing is stored for the mortgage. Same agent, different paths.*

### 3 · The same thing without a browser

```bash
tariff-agent run "потребительский кредит"    # one product, deterministic, prints the report
tariff-agent monitor --json                  # the whole catalogue: the scheduled path
tariff-agent snapshots list                  # history, offline
tariff-agent agent "Ի՞նչ է սպառողական վարկի տոկոսադրույքը:"
```

*You will see an Armenian tariff report with a verified quote under every value, and for
`agent`, the tool sequence and a `run_metrics` line. Exit codes: `0` fine, `1` a run failed,
`2` a bad request, `3` something needs a human.*

### 4 · The five demos — offline, no key, no network

```bash
./demos/run_all.sh                    # all five, with a tally

python demos/01_discovery.py          # a name in three languages → ranked official documents
python demos/02_documents.py          # parsing a scanned PDF, and the margin OCR must beat
python demos/03_retrieval.py          # which passage states each field — and an honest NOT_FOUND
python demos/04_extraction.py         # a verified quote behind every value; a fabricated one refused
python demos/05_change_review.py      # a change detected, a human asked once, the answer remembered
```

*You will see each demo assert its own claims and exit non-zero if one fails. Add `--live` to
run any of them against acba.am.*

### 5 · Tests and evaluation

```bash
pytest -q                             # 431 tests, none touching the network
python eval/run_eval.py               # 18 items offline → rewrites eval/results.md
python eval/run_eval.py --live        # the same items against acba.am → eval/results-live.md
```

*You will see 18/18 offline. The live run asserts product resolution and reports field values,
because a tariff changing is the thing this system exists to notice, not a regression in it.*

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
ruff check src tests demos eval    # lint + docstring rules
mypy                               # strict type checking of src/
```

`pytest`, the demos and the evaluation set are in the quick start above.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | End-to-end flow, module map, what Gemini decides vs what code decides, test strategy |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Every design decision by phase: options considered, why this one, which requirement it serves |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | What the system gets wrong, what it has not measured, and each unfound field accounted for individually |
| [docs/PROGRESS_REPORT.md](docs/PROGRESS_REPORT.md) | A phase-by-phase report of what was built, what running it live revealed, and requirement coverage |
| [docs/REHEARSAL.md](docs/REHEARSAL.md) | The live-demo checklist: order, commands, and a fallback for every failure mode |
| [eval/results.md](eval/results.md) | Evaluation results, offline, regenerated by `eval/run_eval.py` |
| [eval/results-live.md](eval/results-live.md) | The same items against acba.am, with the live field values |

## Two ways in

The **deterministic pipeline** is the scheduled path: `tariff-agent run` and `monitor` do the
same steps in the same order every time, and that is what a cron job calls. The **agent**
reaches the same parts through six tools and decides which steps a question needs — it will
answer from a stored snapshot without fetching, skip discovery, or stop and ask when a name is
ambiguous. Both write to the same store and raise the same review questions.

`--json` goes to stdout and logs to stderr, so output stays parseable. Exit codes: `0` fine,
`1` a run failed, `2` a bad request, `3` something needs a human.

`adk web` and `tariff-agent agent` need `GOOGLE_API_KEY`; everything else runs offline with the
rule-based extractor.

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

## Results

Measured, reproducible, and written by the run rather than by hand —
[eval/results.md](eval/results.md) is regenerated by `eval/run_eval.py`.

| | |
|---|---|
| Tariff fields found, live | **14 of 20** through discovery + hybrid retrieval — [results-live.md](eval/results-live.md) |
| Same, BM25-only | 15 of 20 — lexical ranking places the answering chunk first slightly more often (P5-D14) |
| Same, sources supplied directly | 17 of 20 — the ceiling when discovery is taken out of the picture |
| Why the gap | Discovery adds lower-ranked documents that displace the answering passages under `top_k=4` ([LIMITATIONS §2.5](docs/LIMITATIONS.md)) |
| The other three | Accounted for individually in [docs/LIMITATIONS.md](docs/LIMITATIONS.md) §1 |
| Evaluation set | 18 items, hy/en/ru, both products, three NOT_FOUND negatives |
| Tests | 431, none touching the network |
| Retrieval | gate 19/20, recall@4 19/20, top-1 16/20 (BM25) |

```bash
python eval/run_eval.py          # offline, no key needed; rewrites eval/results.md
python eval/run_eval.py --live   # the same items against acba.am
```

## Requirement coverage

| § | Requirement | Where |
|---|---|---|
| 5.1 | ADK agent | [`agent/agent.py`](src/tariff_agent/agent/agent.py) · P8-D1…D8 |
| 5.2 | Focused tools | [`agent/tools.py`](src/tariff_agent/agent/tools.py) · P8-D1 (six tools, each at a fork) |
| 5.3 | Official source discovery | [`discovery/`](src/tariff_agent/discovery/) · P3-D1…D20 |
| 5.4 | PDF / OCR processing | [`documents/`](src/tariff_agent/documents/) · P4-D1…D13 |
| 5.5 | Chunking and RAG | [`rag/`](src/tariff_agent/rag/) · P5-D1…D10, P7-D13 |
| 5.6 | Structured extraction | [`extraction/`](src/tariff_agent/extraction/) · P6-D1…D16, P7-D12 |
| 5.7 | Evidence and provenance | [`extraction/verify.py`](src/tariff_agent/extraction/verify.py) · P6-D2…D4, P7-D4 |
| 5.8 | Deterministic validation | [`extraction/validate.py`](src/tariff_agent/extraction/validate.py) · P6-D5, P7-D9, P7-D10 |
| 5.9 | Change detection | [`snapshots/diff.py`](src/tariff_agent/snapshots/diff.py) · P7-D1…D4, P7-D8 |
| 5.10 | Human in the loop | [`snapshots/review.py`](src/tariff_agent/snapshots/review.py) · P7-D5…D7, P8-D7 |
| 5.11 | Error handling | [`errors.py`](src/tariff_agent/errors.py) · P8-D3 (nothing raises into the model) |
| 5.12 | Security | [`http/url_policy.py`](src/tariff_agent/http/url_policy.py) · P1-D9…D12, P2-D2…D7, P8-D2, P8-D4 |
| 5.13 | Observability | [`agent/metrics.py`](src/tariff_agent/agent/metrics.py) · P1-D16, P8-D9 |
| 5.14 | Testing | [`tests/`](tests/) · 431 tests, incl. one whole-flow run and four agent scenarios |
| 5.15 | Python engineering | Typed throughout, `mypy --strict` clean, docstring on every module |

Decision ids (`P7-D4`) index [docs/DECISIONS.md](docs/DECISIONS.md), which gives the options
rejected for each.

## AI assistance disclosure

Claude Code (Opus 5) was used as a coding assistant throughout: drafting modules, tests and
documentation from a phase-by-phase specification, and reviewing the result. The phase plan,
the architecture and every design decision were the author's, taken one phase at a time with
the alternatives written down in [docs/DECISIONS.md](docs/DECISIONS.md) before code was
written. Every file has been reviewed by the author, who is responsible for the whole
submission.

What that collaboration actually looked like is worth stating, because it is the honest answer
to "how much of this did you do":

- **Specification and review were the author's.** Each phase began with a written plan that
  was approved, amended or rejected before any code existed, and ended with a review that
  found real defects — several of the fixes recorded in `DECISIONS.md` exist because a review
  comment rejected the first attempt.
- **Nothing was accepted because it looked plausible.** Every claim in these documents that
  carries a number was measured: the retrieval comparison, the two-model evaluation, the
  marker counts behind each NOT_FOUND, the OCR margin. Where a number could not be measured —
  `gemini-2.5-flash`, blocked by a free-tier quota — the gap is recorded rather than filled.
- **The most valuable defects came from running it, not from writing it.** A verifier that
  accepted an invented rate at 0.94 similarity, a reviewer prompt fired by our own extractor
  change, a `--json` mode made unparseable by a library banner, a conflict check that compared
  a document against itself. All were found by executing the whole thing end to end, and all
  are listed in [docs/PROGRESS_REPORT.md](docs/PROGRESS_REPORT.md) §7 with the test that now
  pins each one.
