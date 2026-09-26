"""The command line: four commands over the same parts.

``run`` and ``monitor`` are the deterministic path - nothing chooses an order,
nothing is skipped, and they are what a scheduler calls. ``agent`` is the same
work reached through the model, which may skip steps it can justify skipping.
``snapshots`` reads the history without touching the network.

Everything is constructed in :func:`build_context`, one function, so a test can
replace the network and the model in one place.
"""

from __future__ import annotations

import json as jsonlib
import sys
from dataclasses import dataclass
from typing import Annotated, Any

import typer

from tariff_agent.config import (
    Allowlist,
    DiscoveryConfig,
    MonitoringConfig,
    ProductCatalog,
    Settings,
    get_settings,
    load_allowlist,
    load_discovery_config,
    load_monitoring_config,
    load_products,
)
from tariff_agent.discovery.product_matcher import ResolutionStatus, resolve_product
from tariff_agent.errors import TariffAgentError
from tariff_agent.extraction.cache import CachedExtractor
from tariff_agent.extraction.extractor import Extractor, GeminiExtractor, RuleBasedExtractor
from tariff_agent.http.client import SafeHttpClient
from tariff_agent.http.robots import build_client
from tariff_agent.observability.logging import configure_logging, run_context
from tariff_agent.pipeline import run_product
from tariff_agent.rag.embeddings import Embedder, build_embedder
from tariff_agent.snapshots.review import CliReviewer, Reviewer
from tariff_agent.snapshots.store import SnapshotStore

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Monitor ACBA Bank's published loan tariffs.",
)
snapshots_app = typer.Typer(no_args_is_help=True, help="Read the monitoring history.")
app.add_typer(snapshots_app, name="snapshots")


@dataclass
class Context:
    """Everything a command needs, built once.

    Attributes:
        settings: Process configuration.
        catalog: The monitored products.
        allowlist: Domains that may be fetched.
        discovery: Source-ranking weights.
        monitoring: Thresholds and review policy.
        store: Snapshots and remembered decisions.
        client: The guarded HTTP client.
        extractor: The extraction backend.
        embedder: Embeddings, or None for BM25 only.
        reviewer: Who to ask when something needs a human.
    """

    settings: Settings
    catalog: ProductCatalog
    allowlist: Allowlist
    discovery: DiscoveryConfig
    monitoring: MonitoringConfig
    store: SnapshotStore
    client: SafeHttpClient
    extractor: Extractor
    embedder: Embedder | None
    reviewer: Reviewer | None


def build_context(*, cache: bool = True, interactive: bool = True) -> Context:
    """Construct everything the commands share.

    Args:
        cache: False makes every extraction call the model, which is what
            ``--no-cache`` is for when a reviewer wants to watch a real call.
        interactive: Whether a person is at the terminal to answer review
            questions. Without one, anything needing review stays pending.

    Returns:
        The assembled context. Without a configured API key the extractor is
        the offline rule-based one, which is worse at reading prose and says so
        on every snapshot it stamps - never a silent downgrade.
    """
    settings = get_settings()
    allowlist = load_allowlist()
    key = settings.google_api_key.get_secret_value() if settings.google_api_key else None
    backend: Extractor = (
        GeminiExtractor(key, model=settings.gemini_model) if key else RuleBasedExtractor()
    )
    return Context(
        settings=settings,
        catalog=load_products(),
        allowlist=allowlist,
        discovery=load_discovery_config(),
        monitoring=load_monitoring_config(),
        store=SnapshotStore(settings.snapshots_db),
        client=build_client(allowlist, settings.http),
        extractor=CachedExtractor(backend, settings.rag.extraction_cache_dir, enabled=cache),
        embedder=build_embedder(key),
        reviewer=CliReviewer() if interactive else None,
    )


def _resolve_or_exit(context: Context, query: str) -> Any:
    """Resolve a product name, exiting with a usable message if it cannot be.

    Args:
        context: The built context.
        query: The product as the user named it.

    Returns:
        The resolved product.
    """
    resolution = resolve_product(query, context.catalog)
    if resolution.status is ResolutionStatus.RESOLVED and resolution.best is not None:
        product = context.catalog.get(resolution.best.product_id)
        if product is not None:
            return product
    typer.secho(resolution.reason, fg=typer.colors.RED, err=True)
    if resolution.status is ResolutionStatus.AMBIGUOUS:
        for match in resolution.alternatives:
            typer.echo(f"  {match.product_id}  ({match.score:.0f})", err=True)
    raise typer.Exit(code=2)


@app.command()
def run(
    product: Annotated[str, typer.Argument(help="Product name in Armenian, English or Russian.")],
    no_cache: Annotated[bool, typer.Option("--no-cache", help="Bypass the answer cache.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Monitor one product once, deterministically, and print its report."""
    configure_logging("DEBUG" if verbose else "WARNING")
    context = build_context(cache=not no_cache, interactive=not json_out)
    resolved = _resolve_or_exit(context, product)

    try:
        with run_context():
            result = run_product(
                resolved,
                context.catalog.bank,
                context.client,
                context.extractor,
                context.store,
                settings=context.settings,
                allowlist=context.allowlist,
                discovery_config=context.discovery,
                monitoring=context.monitoring,
                embedder=context.embedder,
                reviewer=context.reviewer,
            )
    except TariffAgentError as exc:
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if json_out:
        typer.echo(jsonlib.dumps(_result_payload(result), ensure_ascii=False, indent=2))
    else:
        typer.echo(result.report)
    raise typer.Exit(code=3 if result.needs_attention else 0)


@app.command()
def monitor(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Monitor every configured product: the scheduled path."""
    configure_logging("DEBUG" if verbose else "WARNING")
    context = build_context(interactive=False)

    payloads: list[dict[str, Any]] = []
    failures = 0
    attention = 0
    for product in context.catalog.products:
        try:
            with run_context():
                result = run_product(
                    product,
                    context.catalog.bank,
                    context.client,
                    context.extractor,
                    context.store,
                    settings=context.settings,
                    allowlist=context.allowlist,
                    discovery_config=context.discovery,
                    monitoring=context.monitoring,
                    embedder=context.embedder,
                    reviewer=None,
                )
        except TariffAgentError as exc:
            failures += 1
            payloads.append(
                {"product_id": product.id, "error_type": type(exc).__name__, "error": str(exc)}
            )
            typer.secho(f"{product.id}: {type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
            continue
        attention += int(result.needs_attention)
        payloads.append(_result_payload(result))
        if not json_out:
            typer.echo(result.report)

    if json_out:
        typer.echo(jsonlib.dumps(payloads, ensure_ascii=False, indent=2))
    raise typer.Exit(code=1 if failures else (3 if attention else 0))


@app.command()
def agent(
    question: Annotated[str, typer.Argument(help="What to ask, in any of the three languages.")],
    budget: Annotated[int, typer.Option(help="Maximum tool calls for this turn.")] = 12,
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Ask the agent, which decides for itself which steps are needed."""
    configure_logging("DEBUG" if verbose else "WARNING")
    from tariff_agent.agent.agent import run_agent
    from tariff_agent.agent.session import AgentSession, Budget

    context = build_context(interactive=not json_out)
    if context.settings.google_api_key is None:
        typer.secho(
            "the agent needs GOOGLE_API_KEY; `run` works offline with the rule-based extractor",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    session = AgentSession(
        settings=context.settings,
        catalog=context.catalog,
        allowlist=context.allowlist,
        discovery=context.discovery,
        monitoring=context.monitoring,
        store=context.store,
        client=context.client,
        extractor=context.extractor,
        embedder=context.embedder,
        reviewer=context.reviewer,
        budget=Budget(max_tool_calls=budget),
    )
    result = run_agent(question, session)

    if json_out:
        typer.echo(
            jsonlib.dumps(
                {
                    "answer": result.answer,
                    "tools": result.tool_sequence,
                    "metrics": result.metrics.as_dict(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(result.answer)
        typer.secho(
            f"\ntools: {' → '.join(result.tool_sequence) or 'none'}", fg=typer.colors.BRIGHT_BLACK
        )
        typer.secho(
            jsonlib.dumps(result.metrics.as_dict(), ensure_ascii=False),
            fg=typer.colors.BRIGHT_BLACK,
        )


@snapshots_app.command("list")
def snapshots_list(
    product: Annotated[str | None, typer.Argument(help="Product; omit for all.")] = None,
    limit: Annotated[int, typer.Option(help="How many per product.")] = 10,
) -> None:
    """List stored snapshots, newest first."""
    configure_logging("WARNING")
    context = build_context(interactive=False)
    products = [_resolve_or_exit(context, product)] if product else list(context.catalog.products)
    for entry in products:
        typer.secho(f"{entry.id}", bold=True)
        history = context.store.history(entry.id, limit=limit)
        if not history:
            typer.echo("  (no snapshots yet)")
            continue
        for stored in history:
            found = sum(
                1 for value in stored.extraction.fields.values() if value.status.value == "found"
            )
            typer.echo(
                f"  #{stored.id:<4} {stored.taken_at:%Y-%m-%d %H:%M}  {stored.status.value:<14} "
                f"{found}/{len(stored.extraction.fields)} found  {stored.extraction_method}"
            )


@snapshots_app.command("show")
def snapshots_show(
    snapshot_id: Annotated[int, typer.Argument(help="Snapshot id from `snapshots list`.")],
) -> None:
    """Print one snapshot's values and the evidence behind them."""
    configure_logging("WARNING")
    context = build_context(interactive=False)
    for entry in context.catalog.products:
        for stored in context.store.history(entry.id, limit=1000):
            if stored.id != snapshot_id:
                continue
            typer.secho(
                f"#{stored.id}  {entry.id}  {stored.taken_at:%Y-%m-%d %H:%M}  "
                f"{stored.status.value}",
                bold=True,
            )
            for field_id, value in stored.extraction.fields.items():
                typer.echo(f"  {field_id:<22} {value.value}")
                if value.evidence is not None:
                    typer.echo(
                        f"  {'':<22} ← {value.evidence.document_name}: "
                        f'"{value.evidence.quote[:90]}"'
                    )
            return
    typer.secho(f"no snapshot #{snapshot_id}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2)


def _result_payload(result: Any) -> dict[str, Any]:
    """Shape one monitoring result for ``--json``.

    Args:
        result: The :class:`~tariff_agent.snapshots.pipeline.MonitoringResult`.

    Returns:
        A JSON-safe mapping.
    """
    return {
        "product_id": result.product_id,
        "snapshot_id": result.snapshot_id,
        "status": result.status.value,
        "completeness": round(result.outcome.validation.completeness, 3),
        "model_calls": result.outcome.model_calls,
        "from_cache": result.outcome.from_cache,
        "baseline": result.diff.is_baseline,
        "changes": [change.described() for change in result.diff.changes],
        "conflicts": [conflict.field_id for conflict in result.outcome.conflicts],
        "fields": {
            field_id: {
                "value": value.value,
                "status": value.status.value,
                "quote": value.evidence.quote if value.evidence else None,
                "document": value.evidence.document_name if value.evidence else None,
            }
            for field_id, value in result.outcome.extraction.fields.items()
        },
    }


def main() -> None:
    """Entry point for the ``tariff-agent`` console script."""
    try:
        app()
    except TariffAgentError as exc:  # pragma: no cover - defence in depth
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
