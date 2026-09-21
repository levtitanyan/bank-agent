
## Phases 1–3: problem → options → decision → why

### Phase 1: the rules

**1. Where do we define the 10 tariff fields?**

- Options: in every module separately · one plain list of names · one registry with all info
- Chosen: one registry (`fields.py`) with id, labels, type, required, search words
- Why: copies drift apart, and adding a field is one edit

**2. How do we stop the model from inventing values?**

- Options: tell Gemini in the prompt · check afterwards and warn · a code rule that blocks it
- Chosen: a validator that makes a value without evidence impossible to create
- Why: prompts can fail with a new model version; code rules can't

**3. How do we represent a missing value?**

- Options: `None` · empty string · leave the key out · `"NOT_FOUND"`
- Chosen: `"NOT_FOUND"`
- Why: `None` is ambiguous (missing, or not checked?), while the string survives JSON, the DB and the report

**4. Which form of a value do we store?**

- Options: only the bank's text · only the normalized number · both
- Chosen: both
- Why: the report shows the bank's wording; the diff compares clean numbers (so "10 000 000" = "10,000,000")

**5. What if the model returns fewer or extra fields?**

- Options: accept partial results · require exactly 10
- Chosen: exactly 10
- Why: "bank doesn't offer it" must look different from "a bug dropped it", and the model can't add fields

**6. How is the source recorded?**

- Options: a text citation · a structured record
- Chosen: a record with document, URL, page, section and quote
- Why: the quote can be checked automatically against the document

**7. What about sources without page numbers?**

- Options: page required · page optional
- Chosen: optional
- Why: HTML product pages have no pages

**8. Can data be changed after it's created?**

- Options: editable · frozen
- Chosen: frozen
- Why: no stage can secretly change another stage's data

**9. Where do settings and secrets live?**

- Options: all in `.env` · all in YAML · split
- Chosen: secrets in `.env`, rules (domains, products) in YAML
- Why: secrets never reach git, and rules are visible to reviewers

**10. Which domains are allowed?**

- Options: `*.acba.am` · exact hosts
- Chosen: exactly `acba.am` and `www.acba.am`
- Why: a wildcard lets unknown or hijacked subdomains in

**11. What are the Google variables named?**

- Options: `TARIFF_GOOGLE_API_KEY` · `GOOGLE_API_KEY`
- Chosen: unprefixed
- Why: the Google SDK reads those exact names

**12. How is the API key protected, and what if there's none?**

- Options: plain string, crash without a key · hidden key, offline mode
- Chosen: `SecretStr` + offline mode
- Why: it never shows in logs, and tests and demos run without a key

**13. What if a config file is broken?**

- Options: fail when it's first used · fail at startup
- Chosen: at startup, with a `ConfigError` naming the file
- Why: a clear error instead of weird behavior mid-run

**14. How are failures represented?**

- Options: one generic error with a message · one error type per failure
- Chosen: one type per failure
- Why: code decides by type (retry or stop), not by reading text

**15. How do we say "a human must decide"?**

- Options: treat it as an error · a separate type
- Chosen: `NeedsReviewError` with a reason + evidence
- Why: asking a human is correct behavior, not a bug

**16. How do we log?**

- Options: plain text · JSON; pass run_id to every function · store it in context
- Chosen: JSON + run_id stored in context
- Why: metrics can be computed from the logs, and function signatures stay clean

**17. Where does the allowlist-checking logic live?**

- Options: in the config · in the HTTP layer
- Chosen: the HTTP layer (config is just data)
- Why: the check must run on every redirect, which is a network concern

**18. Which Python?**

- Options: 3.14 (system) · 3.11
- Chosen: 3.11
- Why: PyMuPDF and ADK don't support 3.14 reliably

### Phase 1.5: fixes

**19. What happens to old snapshots when we add a field?**

- Options: one strict loader (breaks) · a `strict=False` flag · a separate loader for stored data
- Chosen: `schema_version` + `from_stored()`
- Why: old data still loads, and fresh Gemini output stays strict

**20. How is a new field filled in an old snapshot?**

- Options: a new "UNKNOWN" state · `NOT_FOUND`
- Chosen: `NOT_FOUND`
- Why: simple and honest, since we never captured it

**21. What about fields removed from the registry?**

- Options: crash · drop silently · drop and log
- Chosen: drop and log
- Why: it loads, but visibly

**22. Field problem vs. "stop the run"?**

- Options: one "needs review" for both · separate
- Chosen: field status (`unverified`, `conflict`) separate from run-level review
- Why: one bad field shouldn't mean the same thing as stopping everything

**23. Where does confidence come from?**

- Options: Gemini reports it · default 1.0 · computed by code, `None` until then
- Chosen: computed by code
- Why: an LLM's self-confidence is unreliable, and there are no made-up numbers

### Phase 2: safe downloading

**24. Where do URL rules live?**

- Options: inside the client · separate pure functions
- Chosen: separate
- Why: testable without the internet, and reused for filtering links in Phase 3

**25. How do we check a domain?**

- Options: "ends with .acba.am" · exact match
- Chosen: exact match
- Why: `acba.am.evil.com` passes an ends-with check

**26. What about `%20` in URLs?**

- Options: decode · keep as is
- Chosen: keep
- Why: `loan%20info.pdf` would 404

**27. How do we handle redirects?**

- Options: automatic · manual, checking every hop
- Chosen: manual
- Why: automatic redirects could silently land on another site

**28. When do we check the URL?**

- Options: after connecting · before
- Chosen: before
- Why: a blocked site is never contacted

**29. How do we know the file is really a PDF?**

- Options: header only · file bytes only · both
- Chosen: both
- Why: servers can lie, and a bad file fails here, not in the parser

**30. How do we limit size?**

- Options: trust `Content-Length` · count real bytes
- Chosen: count real bytes (the header is only a first check)
- Why: the header can lie

**31. When do we retry?**

- Options: always · never · only temporary errors
- Chosen: timeout / 429 / 5xx, max 3, with backoff
- Why: a 404 is an answer, and retrying it wastes time

**32. What if the site blocks us (403)?**

- Options: retry · work around it · stop
- Chosen: stop and log `access_blocked`
- Why: the assignment forbids bypassing access rules

**33. What if the server says "retry after 10 minutes"?**

- Options: obey fully · ignore · obey with a cap
- Chosen: capped
- Why: one server can't stall the whole run

**34. How do we cache without missing changes?**

- Options: serve the cache without asking · never cache · ask "changed?" (ETag/304)
- Chosen: ask every time
- Why: otherwise monitoring never sees a new PDF

**35. What about the ETag across ACBA's www→acba.am redirect?**

- Options: drop it · keep it for the same file
- Chosen: keep it
- Why: live bug, the 1 MB file was re-downloaded every run

**36. How do we identify cached files vs. changed content?**

- Options: one hash · two hashes
- Chosen: URL hash (find the file) + content hash (detect change)
- Why: different jobs

**37. How do we run without internet?**

- Options: guess it · an explicit `offline=true`
- Chosen: explicit
- Why: guessing hides real failures

**38. How do we test retries without waiting?**

- Options: real network and sleep · injected fakes
- Chosen: injected transport + sleep
- Why: fast offline tests that can count requests

**39. What if robots.txt can't be read?**

- Options: always allow · always stop · depends on the error
- Chosen: 404 → allowed, 5xx → stop
- Why: same as Google, and we don't guess in our own favor

**40. How is robots.txt downloaded?**

- Options: separate httpx call · the same safe client
- Chosen: the same client
- Why: only one network door

**41. Sync or async?**

- Options: async · sync
- Chosen: sync
- Why: tools run one at a time, so async adds bugs for no gain

**42. What if saving to the cache fails?**

- Options: fail the run · log and continue
- Chosen: log and continue
- Why: we already have the file

**43. How long to wait for a download?**

- Options: 30s · 60s
- Chosen: 60s
- Why: live, ACBA took more than 30s for the 1 MB PDF

### Phase 3: discovery

**44. How do we understand the product name?**

- Options: ask Gemini · embeddings · rules
- Chosen: rules (no Gemini)
- Why: a wrong guess reports another product's rates, and rules are testable, offline and explainable

**45. How do we handle synonyms, typos and "business mortgage"?**

- Options: fuzzy only · synonyms + fuzzy · synonyms + fuzzy + penalty words
- Chosen: all three
- Why: synonyms = meaning, fuzzy = typos, penalty = "business mortgage" (fuzzy alone scores it 100)

**46. When is a match accepted?**

- Options: one threshold · bands + a required lead
- Chosen: ≥85 and a 10-point lead = found; 60–85 or close = ask; <60 = not found
- Why: "loan" matches both products, so we ask rather than pick randomly

**47. What if the product isn't found or is unclear?**

- Options: raise an error · return a status
- Chosen: a status (found / ambiguous / not_found)
- Why: the agent and the reviewer get the options and scores, with no crash

**48. How do we compare Armenian text reliably?**

- Options: raw text · normalized
- Chosen: NFC + strip «» and punctuation
- Why: the same letters can be encoded differently

**49. How do we handle "ipoteka"-style spellings?**

- Options: transliteration library · list them as synonyms
- Chosen: synonyms
- Why: simple; the cost is that unlisted spellings fail

**50. How do we read the sitemap safely?**

- Options: lxml · `defusedxml`
- Chosen: `defusedxml`
- Why: protects against XML-bomb memory attacks

**51. What if the sitemap is broken?**

- Options: crash · return empty and continue
- Chosen: empty
- Why: it's one of three routes, and ACBA's sitemap really has a broken entry

**52. How many sources do we return?**

- Options: one winner · "PDF always wins" · main + supporting
- Chosen: main + supporting
- Why: the consumer loan's rates are on its page, not in a PDF

**53. Can the general tariffs PDF be the main source?**

- Options: highest score wins · role first, then score
- Chosen: role first
- Why: a document covering all loans can support but never lead (live bug)

**54. How do we explain the ranking?**

- Options: numbers only · numbers + reasons
- Chosen: reasons ("ամփոփագիր +50")
- Why: the reviewer and the logs see _why_

**55. Where do ranking weights live?**

- Options: code · YAML
- Chosen: YAML, including negative weights (archive, business)
- Why: readable and fixable by bank staff

**56. How do we tell individual from business products?**

- Options: keywords · URL path
- Chosen: path (`/business/` = −60) plus keywords
- Why: it reflects how the bank itself organizes the site

**57. Which pages are worth downloading at all?**

- Options: one score floor · two floors
- Chosen: two (drop vs. worth fetching)
- Why: with one floor, the business mortgage page got crawled

**58. Must the tariffs PDF contain the product name?**

- Options: yes · exempt shared documents
- Chosen: exempt
- Why: it legitimately covers all loans

**59. What if crawling finds nothing?**

- Options: fail · use seeds silently · use seeds, marked
- Chosen: seeds always get crawl slots, and are marked `is_seed`
- Why: the curated page isn't pushed out, and the reviewer knows it's a backup

**60. What if two summaries score close?**

- Options: pick the higher · ask a human
- Chosen: ask (purchase vs. renovation mortgage)
- Why: otherwise it's a coin flip on which product's rates get reported

**61. What if one page fails mid-crawl?**

- Options: stop everything · skip it
- Chosen: skip and log; error only if nothing at all is found
- Why: the other sources are still valid

**62. How many allowlist copies exist?**

- Options: load it again in discovery · share one
- Chosen: share the client's instance
- Why: the two can't drift out of sync

**63. What HTML do the tests use?**

- Options: invented · full real pages · trimmed real pages
- Chosen: trimmed real pages, with URL and date
- Why: they caught the `http://` link and the duplicate URLs, which invented HTML wouldn't