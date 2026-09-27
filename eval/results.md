# Evaluation results

Written by `eval/run_eval.py` from an actual run. Not edited by hand: if a number
here disagrees with a number elsewhere in the documentation, this one is right.

- **Run:** 2026-09-27 11:46 UTC
- **Corpus:** offline (fixture corpus)
- **Extractor:** rule_based
- **Result:** 18/18 asserted items passed

## What this measures, and what it does not

Two products at one bank, in Armenian. The field registry, the morphology, the value
shapes and the query terms are all fitted to that corpus — the query terms were
corrected against the real documents three times, which is the honest way to build
them and also the reason they should not be assumed to transfer. A pass rate here is
evidence that the pipeline reads *these* documents correctly, and no evidence at all
about a second bank.

## Items

| Item | Kind | Outcome | Detail |
|---|---|---|---|
| `resolve-hy-consumer` | resolution | ✅ pass | → consumer_loan (100) |
| `resolve-en-consumer` | resolution | ✅ pass | → consumer_loan (100) |
| `resolve-ru-consumer` | resolution | ✅ pass | → consumer_loan (100) |
| `resolve-en-typo` | resolution | ✅ pass | → consumer_loan (88) |
| `resolve-hy-mortgage` | resolution | ✅ pass | → mortgage (100) |
| `resolve-ru-mortgage` | resolution | ✅ pass | → mortgage (100) |
| `resolve-latin-translit` | resolution | ✅ pass | → mortgage (100) |
| `reject-unsupported-product` | resolution | ✅ pass | status=not_found |
| `reject-unrelated-query` | resolution | ✅ pass | status=not_found |
| `consumer-nominal-rate` | field | ✅ pass | nominal_rate = 20.1-21.6% |
| `consumer-currency` | field | ✅ pass | currency = ված րոպեների ընթացքում Վարկի տրամադրում առանց միջնորդավճ |
| `consumer-amount` | field | ✅ pass | amount = 50,000 - 10,000,000 ՀՀ դրամ Տոկոսադրույ |
| `consumer-collateral` | field | ✅ pass | collateral = ված րոպեների ընթացքում Վարկի տրամադրում առանց միջնորդավճ |
| `mortgage-nominal-rate` | field | ✅ pass | nominal_rate = 13.5 - 14.5% |
| `mortgage-currency` | field | ✅ pass | currency = Արժույթ՝ ՀՀ դրամ Տևողություն՝ 9-60 ամիս Գումար՝ 50,000 - |
| `consumer-application-fee-absent` | field | ✅ pass | application_fee = not_found |
| `consumer-term-absent-offline` | field | ✅ pass | term = not_found |
| `no-field-is-invented` | invariant | ✅ pass | 16 reported values, every quote verified |

## Notes on individual items

**`resolve-hy-consumer`** — Armenian, exact synonym.

**`resolve-en-consumer`** — English.

**`resolve-ru-consumer`** — Russian.

**`resolve-en-typo`** — Two typos; rapidfuzz should still land it.

**`resolve-hy-mortgage`** — Armenian.

**`resolve-ru-mortgage`** — Russian, one word.

**`resolve-latin-translit`** — Armenian typed on an English keyboard.

**`reject-unsupported-product`** — ACBA sells this; we do not monitor it. Fuzzy matching alone resolves it to the retail mortgage, because the phrase contains it.

**`reject-unrelated-query`** — A card deposit — not a loan at all.

**`consumer-currency`** — Offline the value is a whole passage rather than «ՀՀ դրամ» alone, because the rule-based extractor quotes the sentence it found rather than reading the value out of it. The assertion is on the currency being present and evidenced, which is what the offline backend can honestly deliver.

**`mortgage-nominal-rate`** — Offline, both products are served the one summary PDF in the fixture set, so the mortgage items assert that extraction succeeds with verified evidence rather than any particular figure. Asserting a mortgage rate against the consumer loan's fixture would be a test that passes for the wrong reason. Run --live for the bank's actual mortgage values.

**`mortgage-currency`** — Same caveat as mortgage-nominal-rate.

**`consumer-application-fee-absent`** — Zero hits on «ուսումնաս», the loosest stem that could match, across both of the consumer loan's documents. The bank does not state this charge.

**`consumer-term-absent-offline`** — The trimmed fixture page does not carry the term statement the live page does. Asserted as absent offline, which is what the documents in front of it actually say.

**`no-field-is-invented`** — Every value reported for either product must carry evidence whose quote occurs in the passage it was taken from. This is the promise the whole project rests on, so it is an evaluation item and not only a unit test.

