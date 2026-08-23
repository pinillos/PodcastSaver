"""CLI de podcast-kb."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import config, db, feeds, ingest

app = typer.Typer(
    add_completion=False,
    help="Base de conocimiento de transcripciones de podcasts.",
    no_args_is_help=True,
)
console = Console()

DbOption = typer.Option(str(db.DEFAULT_DB_PATH), "--db", help="Ruta del SQLite local.")
ConfigOption = typer.Option(
    str(config.DEFAULT_CONFIG_PATH), "--config", "-c", help="Ruta de podcasts.yaml."
)


@app.command()
def init(db_path: str = DbOption) -> None:
    """Crea el esquema SQLite local."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    console.print(f"[green]✓[/] Esquema creado en [bold]{db_path}[/]")


@app.command()
def add(
    slug: str = typer.Option(..., "--slug", help="Identificador corto y estable."),
    rss: Optional[str] = typer.Option(None, "--rss", help="URL del feed RSS."),
    apple_id: Optional[str] = typer.Option(None, "--apple-id", help="ID de Apple Podcasts."),
    lang: str = typer.Option("es", "--lang", help="Idioma del podcast (es | en)."),
    config_path: str = ConfigOption,
) -> None:
    """Da de alta un podcast en config/podcasts.yaml.

    El YAML es la fuente de verdad (§4.1); SQLite se reconcilia en el sync.
    """
    if not rss and not apple_id:
        raise typer.BadParameter("Hace falta --rss o --apple-id.")

    path = Path(config_path)
    entries = config.load_podcasts(path) if path.exists() else []
    if any(e["slug"] == slug for e in entries):
        console.print(f"[red]✗[/] Ya existe un podcast con slug [bold]{slug}[/].")
        raise typer.Exit(1)

    entry: dict = {"slug": slug, "language": lang}
    if rss:
        entry["rss_url"] = rss
    if apple_id:
        entry["apple_id"] = apple_id
        if not rss:
            console.print(f"Resolviendo feed desde Apple id [bold]{apple_id}[/]…")
            try:
                resolved = feeds.resolve_feed_from_apple_id(apple_id)
            except (feeds.FeedError, OSError) as exc:
                console.print(f"[red]✗[/] {exc}")
                raise typer.Exit(1) from exc
            entry["rss_url"] = resolved["rss_url"]
            entry.setdefault("title", resolved.get("title"))
            entry.setdefault("authors", resolved.get("authors"))
            console.print(f"  → [green]{resolved['rss_url']}[/]")

    entries.append(entry)
    config.save_podcasts(entries, path)
    console.print(f"[green]✓[/] [bold]{slug}[/] añadido a {path}")


@app.command()
def sync(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Lista lo que se detectaría, sin escribir ni descargar."
    ),
    limit: Optional[int] = typer.Option(
        None, "--max-episodes", help="Tope de episodios por podcast (§5.9)."
    ),
    db_path: str = DbOption,
    config_path: str = ConfigOption,
) -> None:
    """Lee los feeds y da de alta los episodios nuevos."""
    try:
        entries = config.load_podcasts(config_path)
    except config.ConfigError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(1) from exc

    conn = db.connect(db_path)
    db.init_schema(conn)

    report = ingest.sync_all(conn, entries, dry_run=dry_run, limit=limit)
    _render(report, dry_run=dry_run)

    if report.failed:
        raise typer.Exit(1)


def _render(report: ingest.SyncReport, *, dry_run: bool) -> None:
    table = Table(title="Sincronización" + (" (dry-run)" if dry_run else ""))
    table.add_column("Podcast")
    table.add_column("Estado")
    table.add_column("Vistos", justify="right")
    table.add_column("Nuevos", justify="right")
    table.add_column("Con transcripción en el feed", justify="right")

    for item in report.podcasts:
        if not item.ok:
            estado = "[red]error[/]"
        elif item.not_modified:
            estado = "[dim]sin cambios[/]"
        else:
            estado = "[green]ok[/]"
        table.add_row(
            item.slug,
            estado,
            str(item.seen),
            str(item.new_count),
            str(item.with_feed_transcript) if item.with_feed_transcript else "-",
        )
    console.print(table)

    for item in report.podcasts:
        if item.new and dry_run:
            console.print(f"\n[bold]{item.slug}[/] — episodios que se darían de alta:")
            for ep in item.new[:20]:
                marca = " [cyan](transcripción en el feed)[/]" if ep.feed_transcript_url else ""
                console.print(f"  · {ep.published_at[:10]}  {ep.title}{marca}")
            if item.new_count > 20:
                console.print(f"  … y {item.new_count - 20} más")

    for item in report.failed:
        console.print(f"[red]✗ {item.slug}:[/] {item.error}")

    if report.total_new:
        verbo = "se darían de alta" if dry_run else "dados de alta"
        console.print(f"\n[bold green]{report.total_new}[/] episodios {verbo}.")
    else:
        console.print("\n[dim]Nada nuevo.[/]")


if __name__ == "__main__":
    app()
