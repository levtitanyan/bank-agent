#!/usr/bin/env python
"""Run the evaluation set and write results.md from what actually happened.

Offline by default, over the fixture corpus, so the numbers are reproducible by
anyone with the repository and no key. ``--live`` runs the same items against
acba.am: resolution is still asserted, because it does not depend on what the
bank published today, while field items are reported as information. A tariff
changing is the thing this system exists to notice, not a regression in it.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "demos"))

from _offline import parts  # noqa: E402

from tariff_agent.discovery.product_matcher import (  # noqa: E402
    ResolutionStatus,
    resolve_product,
)
from tariff_agent.extraction.pipeline import extract_tariffs  # noqa: E402
from tariff_agent.extraction.verify import verify_quote  # noqa: E402
from tariff_agent.models import FieldStatus  # noqa: E402
from tariff_agent.observability.logging import configure_logging  # noqa: E402
from tariff_agent.pipeline import load_sources  # noqa: E402

DATASET = Path(__file__).parent / "dataset.yaml"
RESULTS = Path(__file__).parent / "results.md"


@dataclass
class Result:
    """One item's outcome.

    Attributes:
        item_id: The dataset id.
        kind: resolution, field or invariant.
        outcome: pass, fail, or info when not asserted in this mode.
        detail: What happened, in words.
    """

    item_id: str
    kind: str
    outcome: str
    detail: str


def check_resolution(item: dict[str, Any], catalog: Any) -> Result:
    """Resolve one query and compare with what the dataset expects.

    Args:
        item: The dataset item.
        catalog: The product catalogue.

    Returns:
        The outcome.
    """
    resolution = resolve_product(item["query"], catalog)
    expect = item["expect"]
    resolved = resolution.status is ResolutionStatus.RESOLVED

    if expect.get("resolved") is False:
        held = not resolved
        detail = f"status={resolution.status.value}"
    else:
        held = resolved and resolution.product_id == expect["product_id"]
        got = resolution.product_id or resolution.status.value
        score = f" ({resolution.best.score:.0f})" if resolution.best else ""
        detail = f"→ {got}{score}"
    return Result(item["id"], "resolution", "pass" if held else "fail", detail)


def check_field(item: dict[str, Any], extractions: dict[str, Any], live: bool) -> Result:
    """Compare one extracted field with what the dataset expects.

    Args:
        item: The dataset item.
        extractions: Per-product extraction outcomes.
        live: Whether this ran against the real site.

    Returns:
        The outcome. Live field items are reported, not asserted.
    """
    outcome = extractions.get(item["product"])
    if outcome is None:
        return Result(item["id"], "field", "fail", "the product could not be extracted")

    value = outcome.extraction.fields[item["field"]]
    expect = item["expect"]
    shown = value.value if value.status is FieldStatus.FOUND else value.status.value
    detail = f"{item['field']} = {shown[:56]}"

    if live:
        return Result(item["id"], "field", "info", detail)

    held = value.status.value == expect["status"]
    if held and "contains" in expect:
        held = expect["contains"] in (value.value or "")
        if not held:
            detail += f"  (expected to contain {expect['contains']!r})"
    return Result(item["id"], "field", "pass" if held else "fail", detail)


def check_invariant(item: dict[str, Any], extractions: dict[str, Any]) -> Result:
    """Verify that every reported value is backed by a quote that really occurs.

    Args:
        item: The dataset item.
        extractions: Per-product extraction outcomes.

    Returns:
        The outcome, naming the first field that fails if any does.
    """
    checked = 0
    for product_id, outcome in extractions.items():
        passages = [
            scored.chunk
            for result in outcome.retrieval.values()
            for scored in result.all_chunks
        ]
        for field_id, value in outcome.extraction.fields.items():
            if value.status is not FieldStatus.FOUND:
                continue
            if value.evidence is None:
                return Result(
                    item["id"], "invariant", "fail", f"{product_id}.{field_id}: no evidence"
                )
            if not verify_quote(value.evidence.quote, "", passages).verified:
                return Result(
                    item["id"], "invariant", "fail",
                    f"{product_id}.{field_id}: quote does not occur in any retrieved passage",
                )
            checked += 1
    return Result(
        item["id"], "invariant", "pass", f"{checked} reported values, every quote verified"
    )


def main() -> None:
    """Run every item and write results.md."""
    configure_logging("ERROR")
    dataset = yaml.safe_load(DATASET.read_text(encoding="utf-8"))
    p = parts()
    mode = "live (acba.am)" if p.live else "offline (fixture corpus)"
    print(f"Running {len(dataset['items'])} items — {mode}\n")

    extractions: dict[str, Any] = {}
    for product_id in ("consumer_loan", "mortgage"):
        product = p.catalog.get(product_id)
        if product is None:
            continue
        try:
            loaded = load_sources(p.client, product, p.discovery, p.settings, embedder=None)
            extractions[product_id] = extract_tariffs(
                product, p.catalog.bank, loaded.retriever, p.extractor, p.allowlist,
                primary_document=loaded.primary_document,
            )
        except Exception as exc:  # noqa: BLE001 - a broken product is a result, not a crash
            print(f"  {product_id}: could not be extracted — {type(exc).__name__}: {exc}")

    results: list[Result] = []
    for item in dataset["items"]:
        if item["kind"] == "resolution":
            result = check_resolution(item, p.catalog)
        elif item["kind"] == "field":
            result = check_field(item, extractions, p.live)
        else:
            result = check_invariant(item, extractions)
        results.append(result)
        mark = {"pass": "PASS", "fail": "FAIL", "info": "info"}[result.outcome]
        print(f"  {mark:4s}  {result.item_id:34s} {result.detail}")

    passed = sum(1 for r in results if r.outcome == "pass")
    failed = sum(1 for r in results if r.outcome == "fail")
    info = sum(1 for r in results if r.outcome == "info")
    print(f"\n{passed} passed, {failed} failed, {info} reported")

    RESULTS.write_text(render(results, dataset, mode, p), encoding="utf-8")
    print(f"wrote {RESULTS.relative_to(ROOT)}")
    sys.exit(1 if failed else 0)


def render(results: list[Result], dataset: dict[str, Any], mode: str, p: Any) -> str:
    """Write the results document.

    Args:
        results: Every item's outcome.
        dataset: The dataset, for the per-item notes.
        mode: Which corpus this ran against.
        p: The assembled parts, for the extractor's name.

    Returns:
        The markdown.
    """
    notes = {item["id"]: item.get("note", "").strip() for item in dataset["items"]}
    passed = sum(1 for r in results if r.outcome == "pass")
    failed = sum(1 for r in results if r.outcome == "fail")
    info = sum(1 for r in results if r.outcome == "info")
    asserted = passed + failed

    lines = [
        "# Evaluation results",
        "",
        "Written by `eval/run_eval.py` from an actual run. Not edited by hand: if a number",
        "here disagrees with a number elsewhere in the documentation, this one is right.",
        "",
        f"- **Run:** {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        f"- **Corpus:** {mode}",
        f"- **Extractor:** {p.extractor.method}",
        f"- **Result:** {passed}/{asserted} asserted items passed"
        + (f", {info} reported without assertion" if info else ""),
        "",
        "## What this measures, and what it does not",
        "",
        "Two products at one bank, in Armenian. The field registry, the morphology, the value",
        "shapes and the query terms are all fitted to that corpus — the query terms were",
        "corrected against the real documents three times, which is the honest way to build",
        "them and also the reason they should not be assumed to transfer. A pass rate here is",
        "evidence that the pipeline reads *these* documents correctly, and no evidence at all",
        "about a second bank.",
        "",
        "## Items",
        "",
        "| Item | Kind | Outcome | Detail |",
        "|---|---|---|---|",
    ]
    for result in results:
        mark = {"pass": "✅ pass", "fail": "❌ **fail**", "info": "· reported"}[result.outcome]
        detail = result.detail.replace("|", r"\|")
        lines.append(f"| `{result.item_id}` | {result.kind} | {mark} | {detail} |")

    commented = [r for r in results if notes.get(r.item_id)]
    if commented:
        lines += ["", "## Notes on individual items", ""]
        for result in commented:
            lines += [f"**`{result.item_id}`** — {notes[result.item_id]}", ""]

    if failed:
        lines += [
            "## Failures",
            "",
            "Recorded as measured. A failing item left in the set is more useful than a",
            "passing set that was trimmed to pass.",
            "",
        ]
        lines += [f"- `{r.item_id}`: {r.detail}" for r in results if r.outcome == "fail"]
        lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
