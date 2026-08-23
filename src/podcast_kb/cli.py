"""CLI de podcast-kb."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import bench as bench_mod
from . import config, db, feeds, ingest, paths, pipeline, transcribe

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


@app.command()
def episodes(
    slug: Optional[str] = typer.Option(None, "--podcast", help="Filtra por slug de podcast."),
    stage: Optional[str] = typer.Option(None, "--stage", help="Filtra por etapa."),
    limit: int = typer.Option(20, "--limit"),
    db_path: str = DbOption,
) -> None:
    """Lista los episodios conocidos y en qué etapa están."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    sql = (
        "SELECT e.id, p.slug, e.published_at, e.title, e.stage, e.needs_review, "
        "       e.feed_transcript_url "
        "FROM episodes e JOIN podcasts p ON p.id = e.podcast_id WHERE 1=1"
    )
    params: list = []
    if slug:
        sql += " AND p.slug = ?"
        params.append(slug)
    if stage:
        sql += " AND e.stage = ?"
        params.append(stage)
    sql += " ORDER BY e.published_at DESC LIMIT ?"
    params.append(limit)

    table = Table(title="Episodios")
    for col in ("id", "podcast", "fecha", "título", "etapa"):
        table.add_column(col)
    rows = conn.execute(sql, params).fetchall()
    for row in rows:
        titulo = row["title"]
        if row["needs_review"]:
            titulo += " [yellow](revisar)[/]"
        if row["feed_transcript_url"]:
            titulo += " [cyan](transcripción en el feed)[/]"
        table.add_row(
            str(row["id"]), row["slug"], row["published_at"][:10], titulo, row["stage"]
        )
    console.print(table)
    if not rows:
        console.print("[dim]Sin resultados. ¿Has corrido `podcast-kb sync`?[/]")


@app.command()
def process(
    episode_id: int = typer.Argument(..., help="ID del episodio (ver `podcast-kb episodes`)."),
    engine: str = typer.Option("whisper.cpp", "--engine", help="whisper.cpp | mlx-whisper"),
    model: str = typer.Option(transcribe.DEFAULT_MODEL, "--model"),
    no_vad: bool = typer.Option(False, "--no-vad", help="Desactiva el VAD (§5.8)."),
    word_timestamps: bool = typer.Option(
        False, "--word-timestamps", help="Timestamps por palabra: -ml 1 -sow (§5.6)."
    ),
    keep_wav: bool = typer.Option(
        False, "--keep-wav", help="Conserva el WAV normalizado por si se diariza (§3.3)."
    ),
    db_path: str = DbOption,
) -> None:
    """Camino completo sobre un episodio: descarga → WAV → Whisper → .md."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        outcome = pipeline.process_episode(
            conn, episode_id, engine=engine, model=model, vad=not no_vad,
            word_timestamps=word_timestamps, keep_wav=keep_wav,
        )
    except (ValueError, OSError, RuntimeError) as exc:
        console.print(f"[red]✗[/] {type(exc).__name__}: {exc}")
        conn.execute(
            "UPDATE episodes SET attempts = attempts + 1, last_error = ? WHERE id = ?",
            (str(exc)[:500], episode_id),
        )
        conn.commit()
        raise typer.Exit(1) from exc

    console.print(f"[green]✓[/] {outcome.md_path}")
    console.print(f"  segmentos: {outcome.segments_path}")
    if outcome.realtime_factor:
        console.print(
            f"  {outcome.elapsed_sec:.0f}s de cómputo "
            f"([bold]{outcome.realtime_factor:.1f}× tiempo real[/])"
        )
    if outcome.prompt_echo_stripped:
        console.print("  [dim]se descartó el eco del prompt inicial[/]")
    if outcome.fixups_applied:
        console.print(f"  [dim]{outcome.fixups_applied} fixups aplicados[/]")
    if outcome.needs_review:
        console.print(f"  [yellow]⚠ marcado para revisión: {outcome.review_reason}[/]")
    for missing in outcome.missing_config:
        console.print(f"  [yellow]⚠ no se encontró {missing}: se transcribió sin él[/]")


@app.command()
def bench(
    wav: str = typer.Argument(..., help="WAV ya normalizado. Usa un tramo de tertulia."),
    engines: str = typer.Option("whisper.cpp", "--engines", help="Lista separada por comas."),
    models: str = typer.Option(
        "models/ggml-large-v3-turbo-q5_0.bin,models/ggml-large-v3-q5_0.bin", "--models"
    ),
    word_timestamps: bool = typer.Option(False, "--word-timestamps"),
    lang: str = typer.Option("es", "--lang"),
) -> None:
    """Compara motores y modelos sobre el mismo audio (§5.3, §15 TODO 1-3).

    Mide velocidad y tamaño. La calidad la juzgas tú leyendo la salida: el
    criterio de aceptación está en §5.10 del diseño.
    """
    combos = [
        (e.strip(), m.strip())
        for e in engines.split(",") if e.strip()
        for m in models.split(",") if m.strip()
    ]
    prompt = pipeline.load_prompt(paths.config_path(f"glossary.{lang}.txt"))
    report = bench_mod.run_bench(
        wav, combos, language=lang, prompt=prompt, word_timestamps=word_timestamps
    )

    if report.audio_duration_sec:
        console.print(f"Audio: [bold]{report.audio_duration_sec:.0f}s[/] — {report.wav}\n")

    table = Table(title="Benchmark")
    for col, just in (
        ("motor", "left"), ("modelo", "left"), ("tiempo", "right"),
        ("× tiempo real", "right"), ("segmentos", "right"), ("palabras", "right"),
        ("json.gz", "right"),
    ):
        table.add_column(col, justify=just)
    for run in report.runs:
        if run.error:
            table.add_row(run.engine, run.model, "[red]error[/]", "-", "-", "-", "-")
            continue
        table.add_row(
            run.engine, run.model, f"{run.elapsed_sec:.0f}s",
            f"{run.realtime_factor:.1f}×" if run.realtime_factor else "-",
            str(run.n_segments), str(run.n_words), f"{run.segments_bytes_gz / 1024:.0f} KB",
        )
    console.print(table)

    for run in report.runs:
        if run.error:
            console.print(f"[red]✗ {run.engine} / {run.model}:[/] {run.error}")
        elif run.text_head:
            console.print(f"\n[bold]{run.engine} / {run.model}[/]\n  {run.text_head}…")


if __name__ == "__main__":
    app()
