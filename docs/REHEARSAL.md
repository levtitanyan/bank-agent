# Live demo checklist

What to run, in what order, what to say, and what to do when something fails in front of
someone. Everything here is offline unless marked **live**, because a review room's network
is not a thing to bet on.

Total: about twelve minutes without questions.

---

## Before the room

- [ ] `pytest -q` — 427 pass. Do this on the actual machine, not from memory.
- [ ] `./demos/run_all.sh` — all five pass. This is the fallback for everything.
- [ ] `python eval/run_eval.py` — 18/18, and `eval/results.md` regenerates.
- [ ] Terminal font renders Armenian. Check «Տարեկան անվանական տոկոսադրույք՝ 20.1-21.6%»
      shows letters, not boxes. A demo nobody can read is not a demo.
- [ ] Window at least 100 columns; the report and the reviewer prompt are formatted for it.
- [ ] `echo $GOOGLE_API_KEY | head -c 8` — confirm a key is present **only if** you intend to
      run the live step. Everything else works without one.
- [ ] Decide in advance whether you are doing the live step at all. It is the only part that
      can fail for reasons outside the repository.

---

## The run

### 1 · What the problem is (1 min, no terminal)

Two ACBA loan products, ten tariff fields each, published across HTML pages and scanned PDFs
in Armenian. The job is to read them, notice when a number changes, and never invent one.

Say the constraint out loud, because it shapes every decision that follows: **a wrong tariff
is worse than a missing one.**

### 2 · Finding the documents (1 min)

```bash
python demos/01_discovery.py
```

Point at: three languages resolving to one product; «business mortgage» *refused* rather than
matched to the retail mortgage it literally contains; the ranked sources each carrying the
reason for every point they scored.

### 3 · Reading a scanned PDF (1 min)

```bash
python demos/02_documents.py
```

Point at: two readings of every page, scored, and the 0.10 margin OCR must beat the parser by.
The story is worth telling — OCR once won a page by 0.076 and had turned «չի» into «sh»,
turning a stated *absence* of a fee into unreadable text.

### 4 · Retrieval, and an honest NOT_FOUND (1.5 min)

```bash
python demos/03_retrieval.py
```

Point at: the refusals and their stated reasons. This is the demo that shows the system
declining to answer. A semantic retriever always returns *something*; the gate requires the
field's rarest identifying token **and** a value of the right kind in the same passage.

### 5 · Extraction with verified evidence (2 min)

```bash
python demos/04_extraction.py
```

Point at: a quote behind every value, then the fabricated rate being refused. Say that this
check was weaker than it looked until recently — «Տևողություն 9-600 ամիս» against a document
saying 9-60 scored **0.98** on fuzzy matching, so every number a quote states is now checked
exactly and separately from the wording.

### 6 · Change detection and a human (3 min)

```bash
python demos/05_change_review.py
```

Point at, in order: two runs over unchanged documents reporting nothing; a moved value with
both quotes and a magnitude in percentage points; two official sources disagreeing and a real
reviewer prompt; the same question **not** asked again on the next run; and a change caused by
our own extractor being reported but never escalated.

That last one is the argument worth making: a reviewer trained to click through our changes
will click through the one that matters.

### 7 · The agent decides what to skip (1.5 min) — **live**

```bash
tariff-agent run "потребительский кредит"        # seeds a snapshot
tariff-agent agent "Ի՞նչ է սպառողական վարկի տոկոսադրույքը:"
```

Point at the tool sequence and the metrics line: two tools, **zero network fetches**, answered
from the snapshot. The agent is not a fixed chain — with no snapshot it reads the bank, with an
ambiguous name it stops and asks.

**If this fails, skip it.** Everything it demonstrates is also in `demos/`, and the four tool
sequences are asserted in `tests/test_agent.py` against the real ADK runner.

### 8 · What it gets wrong (1 min, no terminal)

Open [LIMITATIONS.md](LIMITATIONS.md). Volunteer this; do not wait to be asked.

Three fields are NOT_FOUND and each has a named cause. The `source_conflict` trigger has no
live example — it is exercised by tests and by demo 5, and §2.4 says exactly why the live
corpus does not currently contain a pair it can catch. `gemini-2.5-flash` was never measured,
because the free tier ran out.

---

## When something fails

| What happens | What to do |
|---|---|
| No network | Everything except step 7 is offline already. Skip step 7 and say why. |
| `429` quota exhausted | The live run replays from the extraction cache and makes **zero** model calls — point at `model_calls=0` and carry on. If it still fails, skip step 7. |
| No `GOOGLE_API_KEY` | `run` works offline with the rule-based extractor; `agent` refuses with a clear message. Show the refusal — it is the correct behaviour. |
| ACBA redesigned overnight | The demos use committed fixtures and do not care. Only step 7 touches the live site. |
| A demo exits non-zero | Read what it says. Each demo prints which claim failed; that is the design. Do not re-run hoping for a different result — say what broke and move on. |
| Armenian renders as boxes | Fall back to `--json`, or to `eval/results.md`, which uses the same strings in a file the reviewer can open. |
| Asked to see the code for X | `docs/ARCHITECTURE.md` §3 is a module map with one line per file. |

---

## Questions worth having an answer ready for

**"Why not just have the model do all of it?"** It does the one thing it is good at — reading
which passage answers a field and quoting it. Everything it could get wrong irrecoverably
(which URLs may be fetched, whether a quote is real, what counts as a large change) is decided
by code, and every answer it gives is checked afterwards. The written split is `P8-D8`.

**"How do you know it isn't hallucinating?"** Every value carries a quote matched back against
the passage it was attributed to, and any number in that quote must occur there exactly.
Demo 4 shows a fabricated rate being refused.

**"What happens when the bank changes a page?"** Content-addressed identity notices; the diff
compares normalized values so reformatting is not a change; anything large stops for a human.
Demo 5, steps in order.

**"Why is it only two products?"** Scope, not capability — adding one is an entry in
`products.yaml`, not new code. The honest cost is that the query terms are corpus-fitted and
should not be assumed to transfer, which is in LIMITATIONS §3.2.

**"What would you do next?"** A reranker for retrieval, per-currency rows on the primary once
the OCR damage is addressed, and a second bank — which is the only real test of whether any of
this generalises.
