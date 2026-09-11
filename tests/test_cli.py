"""Tests de la capa de CLI.

Es pegamento —parsea opciones, llama a un módulo, pinta el resultado—, pero
es también lo único que el usuario toca, y los fallos que tiene son los que
más se notan: un flag que no llega, un código de salida 0 cuando algo ha
fallado, un mensaje que no dice qué hacer. Aquí se comprueba eso, con los
módulos de debajo sustituidos por dobles.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from podcast_kb import bench as bench_mod
from podcast_kb import cli, db, feeds, ingest, pipeline
from podcast_kb import index as index_mod

runner = CliRunner()


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """Base de datos y YAML de usar y tirar."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def correr(*args: str):
    return runner.invoke(cli.app, list(args))


def escribir_yaml(path: Path, podcasts: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"podcasts": podcasts}), encoding="utf-8")


def con_episodio(db_path: Path, **campos) -> int:
    """Deja un podcast y un episodio en la base, y devuelve el id del episodio."""
    conn = db.connect(db_path)
    db.init_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO podcasts (id, slug, title, rss_url, language) "
        "VALUES (1, 'monos', 'Monos', 'https://ejemplo.com/f.xml', 'es')"
    )
    fila = {
        "podcast_id": 1, "guid": "g1", "enclosure_sha256": "a" * 64,
        "title": "Agentes", "published_at": "2026-07-15T06:00:00+00:00",
        "audio_url": "https://ejemplo.com/a.mp3", "discovered_at": db.utcnow(),
    }
    fila.update(campos)
    columnas = ", ".join(fila)
    marcas = ", ".join("?" * len(fila))
    cur = conn.execute(f"INSERT INTO episodes ({columnas}) VALUES ({marcas})", tuple(fila.values()))
    conn.commit()
    episode_id = cur.lastrowid
    conn.close()
    return episode_id


class TestInit:
    def test_crea_el_esquema(self, entorno):
        salida = correr("init", "--db", "local.sqlite")
        assert salida.exit_code == 0
        conn = sqlite3.connect(entorno / "local.sqlite")
        tablas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"podcasts", "episodes"} <= tablas


class TestAdd:
    def test_alta_con_rss(self, entorno):
        salida = correr("add", "--slug", "monos", "--rss", "https://x/f.xml",
                        "--config", "p.yaml")
        assert salida.exit_code == 0
        entradas = yaml.safe_load((entorno / "p.yaml").read_text())["podcasts"]
        assert entradas[0]["slug"] == "monos"
        assert entradas[0]["rss_url"] == "https://x/f.xml"

    def test_sin_rss_ni_apple_id(self, entorno):
        salida = correr("add", "--slug", "monos", "--config", "p.yaml")
        assert salida.exit_code != 0
        assert not (entorno / "p.yaml").exists()

    def test_slug_invalido(self, entorno):
        """El slug es nombre de directorio: si cuela `../`, se sale de la raíz."""
        salida = correr("add", "--slug", "../fuera", "--rss", "https://x/f.xml",
                        "--config", "p.yaml")
        assert salida.exit_code != 0
        assert "slug inválido" in salida.output

    def test_slug_duplicado(self, entorno):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://x/f.xml"}])
        salida = correr("add", "--slug", "monos", "--rss", "https://y/f.xml",
                        "--config", "p.yaml")
        assert salida.exit_code == 1
        assert "Ya existe" in salida.output

    def test_apple_id_no_reconocible(self, entorno):
        salida = correr("add", "--slug", "monos", "--apple-id", "no-es-un-id",
                        "--config", "p.yaml")
        assert salida.exit_code == 1
        assert not (entorno / "p.yaml").exists()

    def test_resuelve_el_feed_desde_apple(self, entorno, monkeypatch):
        monkeypatch.setattr(feeds, "resolve_feed_from_apple_id", lambda _id, **kw: {
            "rss_url": "https://anchor.fm/s/x/podcast/rss", "title": "Monos",
            "authors": "Antonio Ortiz, Matías S. Zavia"})
        salida = correr("add", "--slug", "monos", "--apple-id", "1723256857",
                        "--config", "p.yaml")
        assert salida.exit_code == 0
        entrada = yaml.safe_load((entorno / "p.yaml").read_text())["podcasts"][0]
        assert entrada["rss_url"] == "https://anchor.fm/s/x/podcast/rss"
        assert entrada["apple_id"] == "1723256857"

    def test_si_apple_falla_no_escribe_el_yaml(self, entorno, monkeypatch):
        def explota(_id, **kw):
            raise feeds.FeedError("Apple no devuelve resultados")
        monkeypatch.setattr(feeds, "resolve_feed_from_apple_id", explota)
        salida = correr("add", "--slug", "monos", "--apple-id", "1723256857",
                        "--config", "p.yaml")
        assert salida.exit_code == 1
        assert not (entorno / "p.yaml").exists()


def _inspeccion(**campos) -> feeds.FeedInspection:
    base = {"rss_url": "https://ejemplo.com/f.xml", "ok": True, "status": 200,
            "title": "Monos estocásticos", "language": "es", "n_items": 178,
            "first_published": "2023-01-01T00:00:00+00:00",
            "last_published": "2026-07-15T00:00:00+00:00"}
    base.update(campos)
    return feeds.FeedInspection(**base)


class TestResolve:
    def test_persiste_la_url_final(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://vieja.com/f.xml"}])
        monkeypatch.setattr(feeds, "inspect_feed", lambda url, **kw: _inspeccion(
            rss_url=url, final_url="https://nueva.com/f.xml"))
        salida = correr("resolve", "--config", "p.yaml")
        assert salida.exit_code == 0
        entrada = yaml.safe_load((entorno / "p.yaml").read_text())["podcasts"][0]
        assert entrada["rss_url"] == "https://nueva.com/f.xml"

    def test_dry_run_no_escribe(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://vieja.com/f.xml"}])
        monkeypatch.setattr(feeds, "inspect_feed", lambda url, **kw: _inspeccion(
            rss_url=url, final_url="https://nueva.com/f.xml"))
        salida = correr("resolve", "--config", "p.yaml", "--dry-run")
        assert salida.exit_code == 0
        entrada = yaml.safe_load((entorno / "p.yaml").read_text())["podcasts"][0]
        assert entrada["rss_url"] == "https://vieja.com/f.xml"

    def test_un_feed_caido_sale_con_error(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://ejemplo.com/f.xml"}])
        monkeypatch.setattr(feeds, "inspect_feed", lambda url, **kw: _inspeccion(
            ok=False, status=404, error=None))
        salida = correr("resolve", "--config", "p.yaml")
        assert salida.exit_code == 1
        assert "sin resolver" in salida.output

    def test_avisa_de_lo_que_ahorra_el_feed(self, entorno, monkeypatch):
        """§4.4: si el feed trae subtítulos con marcas, no hay que transcribir."""
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://ejemplo.com/f.xml"}])
        monkeypatch.setattr(feeds, "inspect_feed", lambda url, **kw: _inspeccion(
            n_transcripts=10, n_transcripts_timed=7, n_chapters=3,
            n_note_chapters=5, trackers=["dts.podtrac.com"]))
        salida = correr("resolve", "--config", "p.yaml")
        assert "7 episodios traen transcripción CON" in salida.output
        assert "3 episodios traen transcripción sin marcas" in salida.output
        assert "3 episodios declaran capítulos" in salida.output

    def test_avisa_si_los_autores_no_cuadran(self, entorno, monkeypatch):
        """§2.1: hay homónimos. Dar de alta el podcast equivocado es fácil."""
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "apple_id": "1723256857",
                                            "authors": "Antonio Ortiz"}])
        monkeypatch.setattr(feeds, "resolve_feed_from_apple_id", lambda _id, **kw: {
            "rss_url": "https://ejemplo.com/f.xml", "title": "Otro",
            "authors": "Alguien Distinto"})
        monkeypatch.setattr(feeds, "inspect_feed", lambda url, **kw: _inspeccion())
        salida = correr("resolve", "--config", "p.yaml")
        assert "los autores no cuadran" in salida.output

    def test_slug_inexistente(self, entorno):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://x/f.xml"}])
        salida = correr("resolve", "--config", "p.yaml", "--slug", "no-existe")
        assert salida.exit_code == 1

    def test_sin_rss_ni_apple_id_resoluble(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "apple_id": "1723256857"}])
        def explota(_id, **kw):
            raise feeds.FeedError("Apple no contesta")
        monkeypatch.setattr(feeds, "resolve_feed_from_apple_id", explota)
        salida = correr("resolve", "--config", "p.yaml")
        assert salida.exit_code == 1
        assert "Apple no contesta" in salida.output

    def test_config_ilegible(self, entorno):
        (entorno / "p.yaml").write_text("esto: [no cierra", encoding="utf-8")
        salida = correr("resolve", "--config", "p.yaml")
        assert salida.exit_code == 1


class TestSync:
    def _reporte(self, **campos) -> ingest.SyncReport:
        item = ingest.PodcastSyncReport(slug="monos", seen=178, **campos)
        return ingest.SyncReport(podcasts=[item])

    def test_dry_run_lista_los_nuevos(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://x/f.xml"}])
        nuevos = [feeds.EpisodeItem(
            guid=f"g{i}", enclosure_sha256=f"{i:064d}", title=f"Episodio {i}",
            audio_url="https://x/a.mp3",
            published_at="2026-07-15T06:00:00+00:00") for i in range(25)]
        monkeypatch.setattr(ingest, "sync_all",
                            lambda *a, **kw: self._reporte(new=nuevos, with_timed_transcript=4))
        salida = correr("sync", "--db", "local.sqlite", "--config", "p.yaml", "--dry-run")
        assert salida.exit_code == 0
        assert "Episodio 0" in salida.output
        assert "y 5 más" in salida.output
        assert "25" in salida.output

    def test_sin_novedades(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://x/f.xml"}])
        monkeypatch.setattr(ingest, "sync_all", lambda *a, **kw: self._reporte(not_modified=True))
        salida = correr("sync", "--db", "local.sqlite", "--config", "p.yaml")
        assert salida.exit_code == 0
        assert "Nada nuevo" in salida.output

    def test_un_feed_que_falla_sale_con_error(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://x/f.xml"}])
        monkeypatch.setattr(ingest, "sync_all",
                            lambda *a, **kw: self._reporte(ok=False, error="404"))
        salida = correr("sync", "--db", "local.sqlite", "--config", "p.yaml")
        assert salida.exit_code == 1
        assert "404" in salida.output

    def test_pasa_el_tope_de_episodios(self, entorno, monkeypatch):
        escribir_yaml(entorno / "p.yaml", [{"slug": "monos", "language": "es",
                                            "rss_url": "https://x/f.xml"}])
        visto = {}
        def falso(conn, entries, *, dry_run, limit):
            visto["limit"] = limit
            return self._reporte()
        monkeypatch.setattr(ingest, "sync_all", falso)
        correr("sync", "--db", "local.sqlite", "--config", "p.yaml", "--max-episodes", "5")
        assert visto["limit"] == 5

    def test_config_ilegible(self, entorno):
        (entorno / "p.yaml").write_text("[[[", encoding="utf-8")
        salida = correr("sync", "--db", "local.sqlite", "--config", "p.yaml")
        assert salida.exit_code == 1


class TestEpisodes:
    def test_lista_vacia_dice_qué_hacer(self, entorno):
        salida = correr("episodes", "--db", "local.sqlite")
        assert salida.exit_code == 0
        assert "podcast-kb sync" in salida.output

    def test_marca_los_que_hay_que_revisar(self, entorno):
        con_episodio(entorno / "local.sqlite", needs_review=1,
                     feed_transcript_url="https://x/a.vtt")
        salida = correr("episodes", "--db", "local.sqlite")
        assert "revisar" in salida.output

    def test_filtra_por_podcast_y_etapa(self, entorno):
        con_episodio(entorno / "local.sqlite", stage="exported")
        assert "Agentes" in correr("episodes", "--db", "local.sqlite",
                                   "--podcast", "monos").output
        assert "Agentes" not in correr("episodes", "--db", "local.sqlite",
                                       "--stage", "discovered").output


class TestProcess:
    def _outcome(self, **campos) -> pipeline.EpisodeOutcome:
        base = {"slug": "monos", "title": "Agentes", "md_path": Path("t/monos/a.md"),
                "segments_path": Path("t/monos/a.json.gz")}
        base.update(campos)
        return pipeline.EpisodeOutcome(**base)

    def test_exige_un_id_o_pending(self, entorno):
        assert correr("process", "--db", "local.sqlite").exit_code != 0
        assert correr("process", "3", "--pending", "2", "--db", "local.sqlite").exit_code != 0

    def test_un_episodio(self, entorno, monkeypatch):
        episode_id = con_episodio(entorno / "local.sqlite")
        monkeypatch.setattr(pipeline, "process_episode", lambda *a, **kw: self._outcome(
            realtime_factor=4.2, elapsed_sec=900, fixups_applied=3,
            prompt_echo_stripped=True, missing_config=["glossary.es.txt"]))
        salida = correr("process", str(episode_id), "--db", "local.sqlite")
        assert salida.exit_code == 0
        assert "4.2×" in salida.output
        assert "3 fixups" in salida.output
        assert "glossary.es.txt" in salida.output

    def test_transcripcion_del_feed(self, entorno, monkeypatch):
        episode_id = con_episodio(entorno / "local.sqlite")
        monkeypatch.setattr(pipeline, "process_episode",
                            lambda *a, **kw: self._outcome(source="feed", diarized=True))
        salida = correr("process", str(episode_id), "--db", "local.sqlite")
        assert "tomada del feed" in salida.output
        assert "con hablantes" in salida.output

    def test_pasa_los_flags_al_pipeline(self, entorno, monkeypatch):
        episode_id = con_episodio(entorno / "local.sqlite")
        visto = {}
        def falso(conn, eid, **kw):
            visto.update(kw)
            return self._outcome()
        monkeypatch.setattr(pipeline, "process_episode", falso)
        correr("process", str(episode_id), "--db", "local.sqlite", "--no-vad",
               "--word-timestamps", "--keep-wav", "--force-whisper",
               "--engine", "mlx-whisper", "--model", "m")
        assert visto["vad"] is False
        assert visto["word_timestamps"] is True
        assert visto["keep_wav"] is True
        assert visto["prefer_feed_transcript"] is False
        assert visto["engine"] == "mlx-whisper"

    def test_pending_sin_nada_pendiente(self, entorno):
        con_episodio(entorno / "local.sqlite", md_path="t/monos/a.md")
        salida = correr("process", "--pending", "3", "--db", "local.sqlite")
        assert salida.exit_code == 0
        assert "No hay episodios pendientes" in salida.output

    def test_un_fallo_programa_el_reintento_y_no_tira_el_lote(self, entorno, monkeypatch):
        """§4.3: en un lote, el que falla no puede llevarse por delante al resto."""
        primero = con_episodio(entorno / "local.sqlite")
        segundo = con_episodio(entorno / "local.sqlite", guid="g2",
                               enclosure_sha256="b" * 64, title="Otro",
                               published_at="2026-07-14T06:00:00+00:00")
        def falso(conn, eid, **kw):
            if eid == primero:
                raise httpx.ConnectError("se cayó la red")
            return self._outcome(title="Otro")
        monkeypatch.setattr(pipeline, "process_episode", falso)

        salida = correr("process", "--pending", "2", "--db", "local.sqlite")
        assert salida.exit_code == 1
        assert "1 de 2 fallaron" in salida.output
        assert "siguiente intento" in salida.output

        conn = db.connect(entorno / "local.sqlite")
        fila = conn.execute("SELECT attempts, next_retry_at, last_error FROM episodes "
                            "WHERE id = ?", (primero,)).fetchone()
        assert fila["attempts"] == 1 and fila["next_retry_at"]
        assert "se cayó la red" in fila["last_error"]
        # El que sí funcionó no queda marcado.
        ok = conn.execute("SELECT attempts FROM episodes WHERE id = ?", (segundo,)).fetchone()
        assert ok["attempts"] == 0

    def test_tras_demasiados_intentos_lo_abandona(self, entorno, monkeypatch):
        episode_id = con_episodio(entorno / "local.sqlite", attempts=db.MAX_INTENTOS - 1)
        def explota(*a, **kw):
            raise OSError("404")
        monkeypatch.setattr(pipeline, "process_episode", explota)
        salida = correr("process", str(episode_id), "--db", "local.sqlite")
        assert salida.exit_code == 1
        assert "abandonado tras" in salida.output

        conn = db.connect(entorno / "local.sqlite")
        fila = conn.execute("SELECT next_retry_at, needs_review FROM episodes WHERE id = ?",
                            (episode_id,)).fetchone()
        assert fila["next_retry_at"] is None and fila["needs_review"] == 1

    def test_un_exito_limpia_el_historial_de_fallos(self, entorno, monkeypatch):
        episode_id = con_episodio(entorno / "local.sqlite", attempts=2,
                                  next_retry_at="2020-01-01T00:00:00+00:00",
                                  last_error="lo de antes")
        monkeypatch.setattr(pipeline, "process_episode", lambda *a, **kw: self._outcome())
        assert correr("process", str(episode_id), "--db", "local.sqlite").exit_code == 0
        conn = db.connect(entorno / "local.sqlite")
        fila = conn.execute("SELECT attempts, next_retry_at, last_error FROM episodes "
                            "WHERE id = ?", (episode_id,)).fetchone()
        assert fila["attempts"] == 0 and fila["next_retry_at"] is None
        assert fila["last_error"] is None

    def test_pending_respeta_la_espera(self, entorno, monkeypatch):
        """Un episodio con el reintento en el futuro no entra en el lote."""
        con_episodio(entorno / "local.sqlite", next_retry_at="2099-01-01T00:00:00+00:00")
        monkeypatch.setattr(pipeline, "process_episode", lambda *a, **kw: self._outcome())
        salida = correr("process", "--pending", "3", "--db", "local.sqlite")
        assert "No hay episodios pendientes" in salida.output


def _escribir_md(root: Path) -> None:
    destino = root / "monos"
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "2026-07-15-agentes.md").write_text(
        "---\n"
        'podcast: "monos"\n'
        'guid: "g1"\n'
        'title: "Agentes"\n'
        'published_at: "2026-07-15"\n'
        'audio_url: "https://ejemplo.com/a.mp3"\n'
        'language: "es"\n'
        "---\n\n"
        "## [00:00:00] Intro\n\nHola, esto es una prueba de indexado.\n",
        encoding="utf-8",
    )


class TestIndex:
    def test_sin_md_avisa(self, entorno):
        salida = correr("index", "--root", "transcripts")
        assert salida.exit_code == 1
        assert "podcast-kb process" in salida.output

    def test_dry_run_cuenta_chunks_sin_dsn(self, entorno):
        _escribir_md(entorno / "transcripts")
        salida = correr("index", "--root", "transcripts", "--dry-run")
        assert salida.exit_code == 0
        assert "chunks" in salida.output
        assert "no se ha escrito nada" in salida.output

    def test_dry_run_reporta_un_md_ilegible(self, entorno):
        _escribir_md(entorno / "transcripts")
        (entorno / "transcripts" / "monos" / "roto.md").write_text("sin front matter",
                                                                  encoding="utf-8")
        salida = correr("index", "--root", "transcripts", "--dry-run")
        assert "roto.md" in salida.output

    def test_sin_dsn_lo_dice(self, entorno, monkeypatch):
        monkeypatch.delenv("PODCAST_KB_DSN", raising=False)
        _escribir_md(entorno / "transcripts")
        salida = correr("index", "--root", "transcripts")
        assert salida.exit_code == 1
        assert "PODCAST_KB_DSN" in salida.output

    def test_embedder_desconocido(self, entorno):
        _escribir_md(entorno / "transcripts")
        salida = correr("index", "--root", "transcripts", "--dsn", "postgresql://x",
                        "--embedder", "no-existe")
        assert salida.exit_code == 1
        assert "desconocido" in salida.output

    def _psycopg_falso(self, monkeypatch, capturado: dict):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return Cursor()
            def commit(self): capturado["commit"] = True
        modulo = type("psycopg", (), {"connect": staticmethod(lambda dsn: Conn())})
        monkeypatch.setitem(__import__("sys").modules, "psycopg", modulo)

    def test_avisa_del_embedder_de_desarrollo(self, entorno, monkeypatch):
        """Con el embedder de hashing la búsqueda semántica no significa nada."""
        _escribir_md(entorno / "transcripts")
        capturado: dict = {}
        self._psycopg_falso(monkeypatch, capturado)
        monkeypatch.setattr(index_mod, "index_all",
                            lambda *a, **kw: index_mod.IndexReport(scanned=1, chunks=4, embedded=4))
        salida = correr("index", "--root", "transcripts", "--dsn", "postgresql://x")
        assert salida.exit_code == 0
        assert "embedder de desarrollo" in salida.output
        assert capturado.get("commit")

    def test_lista_los_huerfanos_y_sugiere_prune(self, entorno, monkeypatch):
        _escribir_md(entorno / "transcripts")
        self._psycopg_falso(monkeypatch, {})
        huerfanos = [f"monos / Episodio {i}" for i in range(12)]
        monkeypatch.setattr(index_mod, "index_all",
                            lambda *a, **kw: index_mod.IndexReport(scanned=1, huerfanos=huerfanos))
        salida = correr("index", "--root", "transcripts", "--dsn", "postgresql://x")
        assert "12 episodios siguen en el índice" in salida.output
        assert "y 2 más" in salida.output
        assert "--prune" in salida.output

    def test_los_errores_de_indexado_salen_con_codigo_1(self, entorno, monkeypatch):
        _escribir_md(entorno / "transcripts")
        self._psycopg_falso(monkeypatch, {})
        monkeypatch.setattr(index_mod, "index_all",
                            lambda *a, **kw: index_mod.IndexReport(errors=["monos/a.md: sin guid"]))
        salida = correr("index", "--root", "transcripts", "--dsn", "postgresql://x")
        assert salida.exit_code == 1
        assert "sin guid" in salida.output

    def test_pasa_force_y_prune(self, entorno, monkeypatch):
        _escribir_md(entorno / "transcripts")
        self._psycopg_falso(monkeypatch, {})
        visto: dict = {}
        def falso(cur, embedder, **kw):
            visto.update(kw)
            return index_mod.IndexReport(podados=3)
        monkeypatch.setattr(index_mod, "index_all", falso)
        salida = correr("index", "--root", "transcripts", "--dsn", "postgresql://x",
                        "--force", "--prune")
        assert visto["force"] is True and visto["prune"] is True
        assert "huérfanos borrados" in salida.output


class TestDoctor:
    def test_resume_y_sale_con_error_si_hay_alguno(self, entorno, monkeypatch):
        from podcast_kb import doctor as doctor_mod
        monkeypatch.setattr(doctor_mod, "comprobar_local", lambda db_path: [
            doctor_mod.Comprobacion("ffmpeg", doctor_mod.OK, "6.1"),
            doctor_mod.Comprobacion("RLS", doctor_mod.ERROR, "desactivado"),
            doctor_mod.Comprobacion("modelo", doctor_mod.AVISO, "no descargado"),
        ])
        salida = correr("doctor", "--db", "local.sqlite")
        assert salida.exit_code == 1
        assert "1 errores, 1 avisos" in salida.output

    def test_con_dsn_comprueba_tambien_el_backend(self, entorno, monkeypatch):
        from podcast_kb import doctor as doctor_mod
        visto: dict = {}
        monkeypatch.setattr(doctor_mod, "comprobar_local", lambda db_path: [])
        def backend(dsn):
            visto["dsn"] = dsn
            return [doctor_mod.Comprobacion("postgres", doctor_mod.OK, "16.3")]
        monkeypatch.setattr(doctor_mod, "comprobar_backend", backend)
        salida = correr("doctor", "--db", "local.sqlite", "--dsn", "postgresql://x")
        assert salida.exit_code == 0
        assert visto["dsn"] == "postgresql://x"


class TestBench:
    def test_tabla_y_errores(self, entorno, monkeypatch):
        (entorno / "a.wav").write_bytes(b"RIFF")
        monkeypatch.setattr(bench_mod, "run_bench", lambda wav, combos, **kw: bench_mod.BenchReport(
            wav=str(wav), audio_duration_sec=600.0,
            runs=[
                bench_mod.BenchRun("whisper.cpp", "turbo", 120.0, 5.0, 300, 1800, 40960,
                                   text_head="Hola a todos"),
                bench_mod.BenchRun("mlx-whisper", "large-v3", 0.0, None, 0, 0, 0,
                                   error="no está instalado"),
            ]))
        salida = correr("bench", "a.wav", "--engines", "whisper.cpp,mlx-whisper",
                        "--models", "turbo,large-v3")
        assert salida.exit_code == 0
        assert "5.0×" in salida.output
        assert "no está instalado" in salida.output
        assert "Hola a todos" in salida.output

    def test_combina_motores_y_modelos(self, entorno, monkeypatch):
        (entorno / "a.wav").write_bytes(b"RIFF")
        visto: dict = {}
        def falso(wav, combos, **kw):
            visto["combos"] = combos
            visto["lang"] = kw.get("language")
            return bench_mod.BenchReport(wav=str(wav), audio_duration_sec=None)
        monkeypatch.setattr(bench_mod, "run_bench", falso)
        correr("bench", "a.wav", "--engines", "whisper.cpp,mlx-whisper",
               "--models", "turbo,large-v3", "--lang", "en")
        assert visto["combos"] == [("whisper.cpp", "turbo"), ("whisper.cpp", "large-v3"),
                                   ("mlx-whisper", "turbo"), ("mlx-whisper", "large-v3")]
        assert visto["lang"] == "en"


class TestAyuda:
    def test_sin_argumentos_muestra_la_ayuda(self):
        salida = correr()
        assert "podcast" in salida.output.lower()

    def test_todos_los_comandos_tienen_ayuda(self):
        for comando in ("init", "add", "resolve", "sync", "episodes", "process",
                        "index", "doctor", "bench"):
            salida = correr(comando, "--help")
            assert salida.exit_code == 0, comando
            assert comando in salida.output or "Usage" in salida.output


def test_el_json_de_ejemplo_de_la_web_sigue_siendo_json():
    """La PWA se configura copiando este fichero: si no parsea, no arranca."""
    ejemplo = Path(__file__).resolve().parents[1] / "web" / "config.example.json"
    json.loads(ejemplo.read_text(encoding="utf-8"))
