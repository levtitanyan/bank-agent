#!/usr/bin/env python
"""Demo 5 — detecting a change, asking a human once, and remembering the answer.

Three claims: a second run over unchanged documents reports nothing; a moved
value is reported with both quotes and a magnitude in the field's own units;
and a change caused by *us* — a different extractor, a reworded prompt — is
reported but never put to a reviewer.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _offline import parts, start  # noqa: E402

from tariff_agent.pipeline import load_sources  # noqa: E402
from tariff_agent.snapshots.diff import Provenance, diff_snapshots  # noqa: E402
from tariff_agent.snapshots.pipeline import review_requests, run_monitoring  # noqa: E402
from tariff_agent.snapshots.review import CliReviewer  # noqa: E402
from tariff_agent.snapshots.store import SnapshotStore  # noqa: E402


def main() -> None:
    """Run a baseline, a repeat, a forced change, and a review."""
    demo = start("Demo 5 — change detection and human review")
    p = parts()
    if not p.live:
        demo.note(
            "Offline: the rule-based extractor, not Gemini. Said plainly because it matters -"
        )
        demo.note(
            "it reads prose worse than a model, and it is what makes the source conflicts"
        )
        demo.note("below fire reliably in a review room. See docs/LIMITATIONS.md §2.4.")
    product = p.catalog.get("consumer_loan")
    assert product is not None
    tmp = Path(tempfile.mkdtemp())
    store = SnapshotStore(tmp / "demo.db")
    loaded = load_sources(p.client, product, p.discovery, p.settings, embedder=None)

    def cycle(reviewer: object | None = None) -> object:
        return run_monitoring(
            product,
            p.catalog.bank,
            loaded.retriever,
            p.extractor,
            p.allowlist,
            store,
            monitoring=p.monitoring,
            reviewer=reviewer,
            doc_id=loaded.doc_id,
            top_k=p.settings.rag.top_k,
            primary_document=loaded.primary_document,
        )

    demo.heading("Two runs over unchanged documents")
    first, second = cycle(), cycle()
    print(f"  run 1: baseline={first.diff.is_baseline}  changes={len(first.diff.changes)}")
    print(f"  run 2: baseline={second.diff.is_baseline}  changes={len(second.diff.changes)}")
    demo.check("the first run is a baseline, not twenty changes", first.diff.is_baseline)
    demo.check("the second run reports nothing changed", not second.diff.changes)

    demo.heading("A value the bank moved")
    demo.note("Editing the stored snapshot stands in for the bank repricing overnight.")
    latest = store.latest("consumer_loan")
    assert latest is not None
    field_id = next(
        (
            fid
            for fid, v in latest.extraction.fields.items()
            if v.status.value == "found" and v.normalized and "min" in (v.normalized or {})
        ),
        "nominal_rate",
    )
    with sqlite3.connect(tmp / "demo.db") as db:
        row = db.execute("SELECT payload FROM snapshots WHERE id = ?", (latest.id,)).fetchone()
        payload = json.loads(row[0])
        payload["fields"][field_id]["value"] = "8.0%"
        payload["fields"][field_id]["normalized"] = {"min": 8.0, "max": 8.0, "unit": "percent"}
        payload["fields"][field_id]["evidence"]["quote"] = "Տարեկան տոկոսադրույք՝ 8.0%"
        db.execute(
            "UPDATE snapshots SET payload = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), latest.id),
        )
    print(f"  stored {field_id} rewritten to 8.0%")

    # Several questions can be raised in one run; a demo that answers only the
    # first crashes on the second.
    answers = itertools.repeat("a")
    reviewer = CliReviewer("demo-reviewer", stream=sys.stdout, prompt=lambda _p: next(answers))
    print("\n  --- what the reviewer sees ---")
    third = cycle(reviewer)
    print("  --- end ---")
    for change in third.diff.changes:
        print(f"  {change.described()}  [{change.significance.value}]")
    demo.check("the change was detected", bool(third.diff.changes))
    demo.check("a human was asked", bool(third.reviews))
    demo.check("the snapshot was resolved", third.status.value in {"confirmed", "rejected"})

    demo.heading("The same question is not asked twice")
    fourth = cycle(reviewer)
    fresh = [outcome for _, outcome in fourth.reviews if not outcome.remembered]
    print(f"  reviews this run: {len(fourth.reviews)}  of which newly asked: {len(fresh)}")
    demo.check("nothing was re-asked", not fresh)

    demo.heading("Two official sources that disagree")
    demo.note("Both documents state the same field and do not agree; that is a question")
    demo.note("for a person, not an average.")
    conflicts = third.outcome.conflicts
    for conflict in conflicts:
        print(f"  {conflict.field_id}:")
        print(f"    primary   : {conflict.primary.value[:56]}")
        print(f"    supporting: {conflict.supporting.value[:56]}")
    demo.check("a source conflict was detected", bool(conflicts))
    demo.check(
        "each conflict carries both sources' evidence",
        all(c.primary.evidence and c.supporting.evidence for c in conflicts),
    )

    demo.heading("A change that is ours is reported, never escalated")
    before = store.history("consumer_loan", limit=10)[-1].extraction
    ours = diff_snapshots(
        before.model_copy(update={"extraction_method": "rule_based"}),
        before.model_copy(update={"extraction_method": "gemini:test", "fields": {**before.fields}}),
    )
    print(f"  provenance: {ours.provenance.value}")
    demo.check(
        "a method change is labelled, not silently compared",
        ours.provenance is Provenance.METHOD_CHANGED,
    )
    demo.check(
        "and it raises no review question",
        review_requests("consumer_loan", ours, [], p.monitoring) == [],
    )
    demo.finish()


if __name__ == "__main__":
    main()
