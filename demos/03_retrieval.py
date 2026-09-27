#!/usr/bin/env python
"""Demo 3 — which passage states each field, and when the honest answer is none.

The relevance gate is the reason this system reports NOT_FOUND instead of the
nearest paragraph. A semantic retriever always returns *something*; the gate
requires the field's rarest identifying token **and** a value of the right kind
in the same passage before anything is handed to the model.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _offline import parts, start  # noqa: E402

from tariff_agent.fields import TARIFF_FIELDS  # noqa: E402
from tariff_agent.pipeline import load_sources  # noqa: E402


def main() -> None:
    """Index the consumer loan's documents and gate every field."""
    demo = start("Demo 3 — retrieval, and an honest NOT_FOUND")
    p = parts()
    product = p.catalog.get("consumer_loan")
    assert product is not None

    loaded = load_sources(p.client, product, p.discovery, p.settings, embedder=None)
    demo.note(f"Indexed: {loaded.primary_title[:48]} + {len(loaded.supporting_titles)} supporting")

    demo.heading("Per-field gate")
    reached, refused = [], []
    for spec in TARIFF_FIELDS:
        result = loaded.retriever.search_field(spec, k=4)
        best = result.best
        where = ""
        if result.is_relevant and best is not None:
            snippet = " ".join(best.chunk.text.split())[:46]
            where = f"  {best.chunk.source_role.value[:7]:7s} «{snippet}…»"
        print(f"  {'PASS' if result.is_relevant else 'none':4s}  {spec.id:18s}{where}")
        (reached if result.is_relevant else refused).append(spec.id)

    demo.heading("Why a field was refused")
    for spec in TARIFF_FIELDS:
        if spec.id in refused:
            result = loaded.retriever.search_field(spec, k=4)
            print(f"  {spec.id}: {result.reason}")

    demo.note("\nThe gate is lexical. A similarity floor was tried and deleted: the absent")
    demo.note("field scored 0.687 and two present fields scored 0.682 and 0.687, so a")
    demo.note("threshold that excluded the first would have excluded the others too.")

    demo.check("most fields are reached", len(reached) >= 6)
    demo.check(
        "a refusal states its reason",
        all(loaded.retriever.search_field(s, k=4).reason for s in TARIFF_FIELDS),
    )
    demo.check(
        "nothing is reached without a passage",
        all(
            loaded.retriever.search_field(s, k=4).best is not None
            for s in TARIFF_FIELDS
            if s.id in reached
        ),
    )
    demo.finish()


if __name__ == "__main__":
    main()
