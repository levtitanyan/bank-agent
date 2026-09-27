# Limitations

What this system does not do, what it gets wrong, and what has not been measured.

The extraction result depends on how the documents get in front of it:

| Path | Found |
|---|---|
| The two authoritative documents supplied directly | **17 / 20** |
| Discovery + BM25-only retrieval | 15 / 20 |
| Discovery + hybrid retrieval — **what `tariff-agent` and the agent actually run** | 14 / 20 |

§1 below accounts for the three absences in the first row, which are the interesting ones —
they are the bank's silence rather than a failure to read. §2.5 accounts for the gap between
the rows, which is ours. This
document accounts for all three that are not, one field at a time, with the evidence for each
claim. A number on its own is not a result; the three absences are the interesting part, and
two of them are the bank's silence rather than the system's failure.

Everything below was measured against the live site on 2026-09-26 with
`gemini-3.5-flash-lite`, and every count is reproducible from the indexed corpus.

---

## 1. The three fields reported NOT_FOUND

### 1.1 `consumer_loan` · `application_fee` — the bank does not state it

The consumer loan is extracted from two documents: the product page
(`Առանց գրավի սպառողական վարկի հայտ`) and the shared tariff book (`loans-tariffs.pdf`).

Marker counts across both, folded for Armenian morphology:

| Marker | Occurrences |
|---|---|
| «ուսումնաս» (the stem of «ուսումնասիրության», deliberately over-broad) | 0 |
| «հայտի վճար» | 0 |
| «հայտի ուսումնասիրության վճար» | 0 |

Zero, on the loosest stem that could possibly match. The same stem finds the charge
immediately in the mortgage's documents, where it *is* stated — so this is not a query-term
problem. ACBA does not publish an application review fee for this product, and NOT_FOUND is
the correct answer.

### 1.2 `mortgage` · `disbursement_fee` — stated, but not attributably

This one was a genuine miss until this phase, and it is now a judgement call rather than a
gap. Both mortgage documents carry the charge, in the same bulleted term sheet:

- `loan info.pdf` (the bank's own date: 2023-05-15) — «Միջնորդավճար - վարկի գումարի **1%**
  կամ կարող է սահմանվել 0%` փոխարենը վարկի անվանական տոկոսադրույքին … կգումարվի 0.5%»
- the current product page — «Միջնորդավճար - վարկի գումարի **0.5 %** կամ կարող է սահմանվել 0%»

Retrieval never showed either clause to the model. Every `disbursement_fee` query term carried
«տրամադրման» or «միանվագ» as its *identifying* token — the rarest token, which the relevance
gate requires — and this clause uses the bare word «Միջնորդավճար». Adding the collocation
«միջնորդավճար վարկի գումարի» fixed that: both clauses now reach the model, at ranks 6 and 14,
with `has_term` and `has_value` both true.

The model still declines to report it, and that is the right answer. The document says
«Միջնորդավճար» — *commission* — and never says this commission is the disbursement fee; the
two sources give different percentages; and each is conditional ("or may be set at 0%, in
which case 0.5% is added to the nominal rate instead"). Reporting `1%` would be choosing one
of three readings on the bank's behalf, which is the one thing this system must not do.

**The distinction worth keeping:** NOT_FOUND here now means *shown to the model and not
attributable*, not *never retrieved*. Those are different failures and only the second is ours.

**How it would be closed:** a human confirming once that ACBA's unqualified «Միջնորդավճար» on
a mortgage term sheet is the disbursement commission. That is a HITL decision, and the review
machinery to record it already exists — it is not a retrieval or prompting problem.

### 1.3 `mortgage` · `salary_privileges` — mentioned, never as a privilege

«աշխատավարձ» occurs five times across the two mortgage documents. All five, in full:

1. «…հաշվարկով **աշխատավարձի** և դրան հավասարեցված վճարումների մասով հայտարարագրված հարկային
   պարտավորություններն…» — declared *tax obligations* on salary.
2. «*Տեղեկանքը պետք է պարունակի հաճախորդի մաքուր ամսական **աշխատավարձի**/եկամտի չափը` հարկերը
   վճարելուց հետո…» — what an income *certificate* must contain.
3. A near-duplicate of (2) from the PDF's repeated footnote.
4–5. The same two clauses again on the product page.

Not one states a benefit for receiving salary through the bank. The consumer loan does state
one, and it is extracted («Աշխատավարձը Բանկի միջոցով ստանալու դեպքում արտոնյալ տոկոսադրույքով
վարկավորման հնարավորություն»), which confirms the field and its query terms work. The
mortgage simply does not offer the privilege.

---

## 2. Known defects that are open

### 2.1 OCR damage survives into reported values

`loan info.pdf` is a scanned document. Where the parsed text wins, «և» reads correctly; where
OCR wins, it is folded to «ն», and the damage reaches the report:

- reported: «ՀՀ դրամ **ն** արտարժույթ» — correct: «ՀՀ դրամ **և** արտարժույթ»
- reported: «…երաշխավորություններ **ն** այն» — correct: «…և այլն»

The value is right and a reader understands it; the transcription is not clean. The «և»→«ն»
fold is deliberate and measured (zero collisions across 26,054 corpus tokens) — it exists so
that retrieval matches OCR-damaged text at all. Undoing it for *display* would need a
reverse pass that is not written.

### 2.2 The mortgage's currency and term come from a 2023 document

The bank's own date on `loan info.pdf` is 2023-05-15. The report states that date under
`Last changed` and distinguishes it from `Last checked`, so the staleness is visible rather
than hidden — but a reader must notice it. Where the current product page states the same
field, the page's value is preferred; for `currency` and `term` it does not.

### 2.3 Conflict detection depends on the supporting document being retrieved

A conflict is only found for a field where the supporting source was also retrieved and
answered. A field the gate declines on the supporting side is compared against nothing and
reports no conflict — which reads the same as agreement. The run is not wrong, but "no
conflict" is weaker evidence than it looks.

---

### 2.4 The source-conflict trigger has no live example

Of the five review triggers, `source_conflict` is the only one with no naturally-occurring
instance on the live path today. It is not untested — unit tests cover it directly, and the
offline demo raises a real one, where the current product page states 20.1–21.6% against the
older information summary's 11.9–12.5%.

Why it does not fire live on these two products:

- The mortgage primary is a scan, and its rate table is the worst-damaged part of it: the
  header reads `Արժույթ Wofwtwywt տոկոսադրույք`. No labelled currency rows survive, so there
  is nothing to pair against the current page's rows.
- The reported rate is a multi-currency composite — «13.75-14.5% (ՀՀ դրամ), 10.5-11.5%
  (ԱՄՆ դոլար), 9-10% (Եվրո)» — which normalizes to a 9–14.5% span. Almost any figure the
  other document states overlaps that, so the headline comparison passes. Per-row comparison
  was added for exactly this, and needs both sides to label their rows.

Two conflicts *did* fire during verification and both were false: the 2023 summary's «առանց …
վճարի», rendered as «0», against the page's «անվճար» — the same fact written two ways. That
was fixed by giving a bare zero a fee normalizer, not by suppressing the trigger.

**The honest position:** the trigger works and is demonstrated offline; the live corpus does
not currently contain a pair it can catch. A reviewer should judge it on the offline demo and
the tests, not on a live run.

### 2.5 Discovery costs three fields against hand-picked sources

Measured on 2026-09-27 through `eval/run_eval.py --live`, which runs exactly what
`tariff-agent run` runs:

| Path | Found | Not found |
|---|---|---|
| Sources supplied directly (primary + the one shared tariff book) | 17/20 | 3 |
| Discovery, BM25-only | 15/20 | 4 |
| Discovery, hybrid (the default when a key is configured) | 14/20 | 5 |

**The fields lost are both products' `service_fee`, plus a `nominal_rate` that comes back
`unverified` instead of found.** Named individually:

- `consumer_loan.service_fee` — was found as «Վարկային հաշվի բացման, վարման և սպասարկման
  նպատակով … չի գանձվում», on page 2 of the shared tariff book.
- `mortgage.service_fee` — was found as «սպասարկման միջնորդավճարներ չկան». Its top retrieved
  passage is now the disclaimer «… ԲՈԼՈՐ ՊԱՐՏԱԴԻՐ ՎՃԱՐՆԵՐԸ …», which mentions service charges
  without stating one.

**The cause is not the retrieval mode.** Both the 17/20 and 14/20 runs used embeddings, and
BM25-only scores *better* than hybrid here (15 against 14) — consistent with
[P5-D14](DECISIONS.md), which measured semantic ranking as buying nothing on this corpus.

The cause is the **source set**. Discovery returns more documents than the hand-picked pair —
for the consumer loan a category page and an online-loan page alongside the tariff book; for
the mortgage a floating-rate PDF and a different information summary as primary — and
`top_k` is 4 passages *per source role*, not per document. More documents compete for the same
four slots, and the passage that answers `service_fee` is displaced by passages from documents
that merely mention fees.

**What would close it**, in rough order of cost: raise `top_k` for the supporting role, which
trades prompt size for recall; or cap the supporting set by score rather than by count, so a
document scoring 63 does not displace one scoring 70; or de-duplicate at retrieval so several
near-identical marketing pages cannot occupy four slots between them. None of these is a
research problem, and none was attempted, because the evidence for choosing between them is an
evaluation set larger than the one this project has.

**Should BM25-only be the default?** On this evidence, yes — it scores one field higher and
removes an API dependency, a per-run charge and a rate-limit failure mode. The decision should
rest on a corpus where the two are genuinely separable, which this is not: a one-field
difference across twenty is well inside the noise of a single model run. The honest position
is that hybrid is not *earning* its cost here, not that lexical retrieval is better in general
— a bank whose documents paraphrase more would likely invert it.

## 3. What has not been measured

### 3.1 `gemini-2.5-flash` is untested

The two-model comparison covers `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` (11/11
correct each; they differ on *scope*, not accuracy — 3.1-lite answered the tariff book's four
currencies for a single loan, which is the failure the product-scoping prompt rule addresses).

`gemini-2.5-flash` could not be measured: the free tier allows 20 generate-content requests a
day and one full two-product run makes about 13. The gap is recorded rather than filled with
an estimate.

### 3.2 Two products, one bank, one language

Everything is measured on `consumer_loan` and `mortgage` at acba.am, in Armenian. The field
registry, the morphology, the value shapes and the query terms are all tuned to that corpus.
Nothing here has been run against a second bank, and the query terms in particular are
corpus-fitted — they were corrected against the real documents three times, which is the
honest way to build them and also the reason they should not be assumed to transfer.

### 3.3 No adversarial testing beyond prompt-injection wording

The prompt states that passage text is data, never instructions, and the model gets no
filesystem, shell, network or arbitrary-URL access — tools take ids from previous tool
outputs. That is the design. It has not been red-teamed against a document crafted to attack
it, because no such document exists in the corpus.

---

## 4. Operational limits

- **The first run cannot detect a change.** A baseline snapshot has nothing to diff against;
  it is recorded as such and the report says so.
- **The extraction cache misses whenever the bank rebuilds a page**, even when the tariff text
  is unchanged. Cache keys include chunk ids, which derive from the document's sha256; the
  ACBA product pages are rebuilt frequently (six distinct byte-hashes for the mortgage page
  over one day, byte-stable within any short window). A run after a rebuild pays for a full
  set of model calls.
- **Scheduling is out of scope.** The agent runs when invoked. Nothing here is a daemon, and
  there is no retry queue for a run that fails halfway.
- **SQLite, single writer.** Concurrent runs over the same product are not supported.
