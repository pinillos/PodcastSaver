"""CLI de podcast-kb."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import httpx
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
    apple_id: Optional[str] = typer.Option(
        None, "--apple-id",
        help="ID de Apple Podcasts, o una URL de pod.link / podcasts.apple.com.",
    ),
    lang: str = typer.Option("es", "--lang", help="Idioma del podcast (es | en)."),
    config_path: str = ConfigOption,
) -> None:
    """Da de alta un podcast en config/podcasts.yaml.

    El YAML es la fuente de verdad (§4.1); SQLite se reconcilia en el sync.
    """
    if not rss and not apple_id:
        raise typer.BadParameter("Hace falta --rss o --apple-id.")
    if not config.SLUG_RE.match(slug):
        raise typer.BadParameter(
            f"slug inválido: {slug!r}. Solo minúsculas, dígitos y guiones."
        )

    path = Path(config_path)
    entries = config.load_podcasts(path) if path.exists() else []
    if any(e["slug"] == slug for e in entries):
        console.print(f"[red]✗[/] Ya existe un podcast con slug [bold]{slug}[/].")
        raise typer.Exit(1)

    entry: dict = {"slug": slug, "language": lang}
    if rss:
        entry["rss_url"] = rss
    if apple_id:
        try:
            apple_id = feeds.extract_apple_id(apple_id)
        except feeds.FeedError as exc:
            console.print(f"[red]✗[/] {exc}")
            raise typer.Exit(1) from exc
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
def resolve(
    slug: Optional[str] = typer.Option(None, "--slug", help="Resuelve solo este podcast."),
    force: bool = typer.Option(
        False, "--force", help="Re-resuelve también los que ya tienen rss_url."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Muestra el diagnóstico sin escribir en el YAML."
    ),
    config_path: str = ConfigOption,
) -> None:
    """Resuelve las feedUrl reales vía Apple y valida que sirven.

    Escribe la URL final (tras redirecciones) en podcasts.yaml, que es la
    que hay que persistir (§2.3), y avisa si el feed ya trae transcripciones
    publicadas (§4.4).
    """
    try:
        entries = config.load_podcasts(config_path)
    except config.ConfigError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(1) from exc

    objetivo = [e for e in entries if not slug or e["slug"] == slug]
    if not objetivo:
        console.print(f"[red]✗[/] No hay ningún podcast con slug [bold]{slug}[/].")
        raise typer.Exit(1)

    cambios = 0
    fallos = 0
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for entry in objetivo:
            console.print(f"\n[bold]{entry['slug']}[/]")

            rss_url = entry.get("rss_url")
            if (not rss_url or force) and entry.get("apple_id"):
                console.print(f"  Apple id {entry['apple_id']} → ", end="")
                try:
                    resolved = feeds.resolve_feed_from_apple_id(entry["apple_id"], client=client)
                except (feeds.FeedError, httpx.HTTPError) as exc:
                    console.print(f"[red]{exc}[/]")
                    fallos += 1
                    continue
                rss_url = resolved["rss_url"]
                console.print(f"[green]{rss_url}[/]")
                if resolved.get("title"):
                    console.print(f"  Apple dice: {resolved['title']} — {resolved.get('authors')}")
                    _avisar_si_no_cuadra(entry, resolved)

            if not rss_url:
                console.print("  [red]✗ sin rss_url ni apple_id resoluble[/]")
                fallos += 1
                continue

            info = feeds.inspect_feed(rss_url, client=client)
            _render_inspection(info)
            if not info.ok:
                fallos += 1
                continue

            # §2.3: se persiste la URL final, no la inicial.
            final = info.final_url or rss_url
            if entry.get("rss_url") != final:
                if dry_run:
                    console.print(f"  [dim]se escribiría rss_url: {final}[/]")
                else:
                    entry["rss_url"] = final
                    cambios += 1

    if cambios and not dry_run:
        config.save_podcasts(entries, config_path)
        console.print(f"\n[green]✓[/] {cambios} rss_url actualizadas en {config_path}")
    elif not dry_run:
        console.print("\n[dim]Nada que actualizar en el YAML.[/]")

    if fallos:
        console.print(f"[red]{fallos} feed(s) sin resolver.[/] Ver README para alternativas.")
        raise typer.Exit(1)


def _avisar_si_no_cuadra(entry: dict, resolved: dict) -> None:
    """§2.1: hay un homónimo. Verificar por autores antes de dar de alta."""
    esperados = (entry.get("authors") or "").lower()
    reales = (resolved.get("authors") or "").lower()
    if not esperados or not reales:
        return
    tokens = {t for t in esperados.replace(",", " ").split() if len(t) > 3}
    if tokens and not any(t in reales for t in tokens):
        console.print(
            f"  [yellow]⚠ los autores no cuadran con los del YAML "
            f"({entry.get('authors')}). ¿Es el podcast correcto? Ver §2.1.[/]"
        )


def _render_inspection(info: feeds.FeedInspection) -> None:
    if not info.ok:
        detalle = info.error or f"HTTP {info.status}"
        console.print(f"  [red]✗ el feed no sirve:[/] {detalle}")
        return

    console.print(f"  [green]✓[/] {info.title} [dim]({info.language})[/]")
    if info.final_url and info.final_url != info.rss_url:
        console.print(f"  [yellow]redirige a:[/] {info.final_url}")
    rango = ""
    if info.first_published and info.last_published:
        rango = f", de {info.first_published[:10]} a {info.last_published[:10]}"
    console.print(f"  {info.n_items} episodios en el feed{rango}")

    if info.n_transcripts_timed:
        console.print(
            f"  [cyan]★ {info.n_transcripts_timed} episodios traen transcripción CON "
            "marcas de tiempo[/] [dim](§4.4: no hay que pasarles Whisper)[/]"
        )
    solo_texto = info.n_transcripts - info.n_transcripts_timed
    if solo_texto:
        console.print(
            f"  [yellow]{solo_texto} episodios traen transcripción sin marcas de tiempo[/] "
            "[dim](hay que transcribirlos igual: sin timestamps no hay salto al minuto)[/]"
        )
    if info.n_chapters:
        console.print(f"  [cyan]★ {info.n_chapters} episodios declaran capítulos[/]")
    if info.n_note_chapters:
        console.print(
            f"  [cyan]★ {info.n_note_chapters} episodios traen índice de capítulos en "
            "las show notes[/] [dim](§6: nos ahorramos generarlos con un LLM)[/]"
        )
    if info.trackers:
        console.print(f"  [dim]hosting: {', '.join(info.trackers)}[/]")


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
    table.add_column("Transcripción en feed", justify="right")
    table.add_column("Con capítulos", justify="right")

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
            f"{item.with_timed_transcript} con marcas"
            if item.with_timed_transcript
            else (str(item.with_feed_transcript) if item.with_feed_transcript else "-"),
            str(item.with_chapters) if item.with_chapters else "-",
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
    episode_id: Optional[int] = typer.Argument(
        None, help="ID del episodio (ver `podcast-kb episodes`). Omítelo con --pending."
    ),
    pending: Optional[int] = typer.Option(
        None, "--pending", "-n",
        help="Procesa los N episodios pendientes más recientes, en vez de uno concreto.",
    ),
    engine: str = typer.Option("whisper.cpp", "--engine", help="whisper.cpp | mlx-whisper"),
    model: str = typer.Option(transcribe.DEFAULT_MODEL, "--model"),
    no_vad: bool = typer.Option(False, "--no-vad", help="Desactiva el VAD (§5.8)."),
    word_timestamps: bool = typer.Option(
        False, "--word-timestamps", help="Timestamps por palabra: -ml 1 -sow (§5.6)."
    ),
    keep_wav: bool = typer.Option(
        False, "--keep-wav", help="Conserva el WAV normalizado por si se diariza (§3.3)."
    ),
    force_whisper: bool = typer.Option(
        False, "--force-whisper",
        help="Transcribe con Whisper aunque el feed publique subtítulos (§4.4).",
    ),
    db_path: str = DbOption,
) -> None:
    """Camino completo sobre un episodio: descarga → WAV → Whisper → .md."""
    if (episode_id is None) == (pending is None):
        raise typer.BadParameter("Indica un ID de episodio o --pending N, pero no ambos.")

    conn = db.connect(db_path)
    db.init_schema(conn)

    if pending is not None:
        # Los que aún no tienen .md, del más reciente al más antiguo.
        ids = [
            fila["id"]
            for fila in conn.execute(
                "SELECT id FROM episodes WHERE md_path IS NULL "
                "  AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "ORDER BY published_at DESC LIMIT ?",
                (db.utcnow(), pending),
            )
        ]
        if not ids:
            console.print("[dim]No hay episodios pendientes.[/]")
            return
    else:
        ids = [episode_id]

    fallos = 0
    for numero, actual in enumerate(ids, start=1):
        if len(ids) > 1:
            console.print(f"\n[bold]({numero}/{len(ids)})[/] episodio {actual}")
        try:
            outcome = pipeline.process_episode(
                conn, actual, engine=engine, model=model, vad=not no_vad,
                word_timestamps=word_timestamps, keep_wav=keep_wav,
                prefer_feed_transcript=not force_whisper,
            )
        except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
            # Un fallo de red o de un binario externo se registra para
            # reintento (§4.3), no se escupe como traceback, y en un lote no
            # tira abajo los episodios que sí funcionan.
            console.print(f"[red]✗[/] {type(exc).__name__}: {exc}")
            conn.execute(
                "UPDATE episodes SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (f"{type(exc).__name__}: {exc}"[:500], actual),
            )
            conn.commit()
            fallos += 1
            continue
        _render_outcome(outcome)

    if fallos:
        console.print(f"\n[red]{fallos} de {len(ids)} fallaron.[/] "
                      "Se reintentarán la próxima vez.")
        raise typer.Exit(1)


def _render_outcome(outcome) -> None:
    console.print(f"[green]✓[/] {outcome.md_path}")
    console.print(f"  segmentos: {outcome.segments_path}")
    if outcome.source == "feed":
        console.print(
            "  [cyan]★ transcripción tomada del feed[/] "
            "[dim](§4.4: sin descargar el audio ni pasar Whisper)[/]"
        )
    if outcome.diarized:
        console.print("  [cyan]★ con hablantes[/]")
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


@app.command(name="index")
def index_cmd(
    dsn: Optional[str] = typer.Option(
        None, "--dsn", envvar="PODCAST_KB_DSN",
        help="Cadena de conexión a Postgres. También por PODCAST_KB_DSN.",
    ),
    embedder: str = typer.Option(
        "hashing", "--embedder", help="hashing (dev) | e5 | bge-m3"
    ),
    root: str = typer.Option("transcripts", "--root", help="Directorio de .md."),
    force: bool = typer.Option(False, "--force", help="Reindexa todo, ignorando checksums."),
    dry_run: bool = typer.Option(False, "--dry-run", help="No escribe en la base de datos."),
) -> None:
    """Carga los .md en Postgres: chunking, embeddings e índice híbrido (§8).

    Lee solo el repositorio, así que puede correr en un GitHub Action con el
    Mac apagado (§3.2).
    """
    from . import embeddings as emb_mod
    from . import index as index_mod

    documentos = index_mod.discover(root)
    if not documentos:
        console.print(f"[yellow]No hay .md en {root}/[/]. ¿Has corrido `podcast-kb process`?")
        raise typer.Exit(1)

    if dry_run:
        total = 0
        for path in documentos:
            try:
                doc = index_mod.load_document(path)
                piezas = index_mod.chunks_for(doc)
            except (ValueError, OSError) as exc:
                console.print(f"[red]✗[/] {path}: {exc}")
                continue
            total += len(piezas)
            console.print(f"  {len(piezas):>4} chunks  {path.name}")
        console.print(f"\n[bold]{len(documentos)}[/] episodios, [bold]{total}[/] chunks. "
                      "[dim](dry-run: no se ha escrito nada)[/]")
        return

    if not dsn:
        console.print(
            "[red]✗[/] Falta la cadena de conexión: usa --dsn o la variable "
            "PODCAST_KB_DSN."
        )
        raise typer.Exit(1)

    try:
        import psycopg
    except ImportError as exc:
        console.print("[red]✗[/] Falta psycopg. Instálalo con `uv pip install 'psycopg[binary]'`.")
        raise typer.Exit(1) from exc

    try:
        embedder_obj = emb_mod.get_embedder(embedder)
    except emb_mod.EmbeddingError as exc:
        console.print(f"[red]✗[/] {exc}")
        raise typer.Exit(1) from exc

    if embedder_obj.name == "hashing-dev":
        console.print(
            "[yellow]⚠ embedder de desarrollo:[/] los vectores no capturan significado. "
            "La búsqueda léxica funciona; la semántica no."
        )

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            report = index_mod.index_all(cur, embedder_obj, root=root, force=force)
        conn.commit()

    table = Table(title="Indexado")
    table.add_column("métrica"); table.add_column("valor", justify="right")
    table.add_row("episodios escaneados", str(report.scanned))
    table.add_row("reindexados", str(report.inserted))
    table.add_row("solo metadatos", str(report.metadata_only))
    table.add_row("chunks", str(report.chunks))
    table.add_row("embeddings calculados", str(report.embedded))
    table.add_row("embeddings reutilizados", str(report.cached))
    console.print(table)

    for error in report.errors:
        console.print(f"[red]✗[/] {error}")
    if report.errors:
        raise typer.Exit(1)


@app.command()
def doctor(
    dsn: Optional[str] = typer.Option(
        None, "--dsn", envvar="PODCAST_KB_DSN",
        help="Comprueba también el backend Postgres.",
    ),
    db_path: str = DbOption,
) -> None:
    """Verifica el entorno y, con --dsn, el despliegue.

    Lo importante que comprueba: que el RLS está activo. Sin él las tablas
    son legibles con la anon key y el corpus entero es público (§7.5, §12).
    """
    from . import doctor as doctor_mod

    comprobaciones = doctor_mod.comprobar_local(db_path)
    if dsn:
        comprobaciones += doctor_mod.comprobar_backend(dsn)

    iconos = {doctor_mod.OK: "[green]✓[/]", doctor_mod.AVISO: "[yellow]·[/]",
              doctor_mod.ERROR: "[red]✗[/]"}
    for c in comprobaciones:
        console.print(f"  {iconos[c.estado]} [bold]{c.nombre}[/]  [dim]{c.detalle}[/]")

    errores = sum(1 for c in comprobaciones if c.estado == doctor_mod.ERROR)
    avisos = sum(1 for c in comprobaciones if c.estado == doctor_mod.AVISO)
    console.print(f"\n{errores} errores, {avisos} avisos.")
    if errores:
        raise typer.Exit(1)


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
