"""El backoff de §4.3.

`next_retry_at` estaba declarado en el esquema, indexado y filtrado por la
consulta de lotes — y nada lo escribía nunca. Un episodio cuyo audio da 404
se reintentaba en cada ejecución, y con `--pending N` llegaba a copar el lote
entero impidiendo que avanzasen los que sí funcionan.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from podcast_kb import db as db_mod
from podcast_kb.cli import app


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "kb.sqlite")
    db_mod.init_schema(c)
    pid = db_mod.upsert_podcast(
        c, {"slug": "x", "title": "X", "rss_url": "r", "language": "es"}
    )
    for n in range(1, 4):
        db_mod.insert_episode_if_new(c, {
            "podcast_id": pid, "guid": f"g{n}", "enclosure_sha256": f"h{n}",
            "title": f"Episodio {n}", "published_at": f"2026-0{n}-01T00:00:00+00:00",
            "audio_url": f"https://x/{n}.mp3",
        })
    return c


class TestEspera:
    def test_crece_con_cada_intento(self):
        esperas = []
        for intentos in range(1, db_mod.MAX_INTENTOS):
            cuando = datetime.fromisoformat(db_mod.proximo_reintento(intentos))
            esperas.append((cuando - datetime.now(UTC)).total_seconds())
        assert esperas == sorted(esperas)
        assert esperas[0] < esperas[-1]

    def test_tiene_tope(self):
        cuando = datetime.fromisoformat(db_mod.proximo_reintento(db_mod.MAX_INTENTOS - 1))
        horas = (cuando - datetime.now(UTC)).total_seconds() / 3600
        assert horas <= db_mod.BACKOFF_MAX_HORAS + 0.1

    def test_se_abandona_al_llegar_al_maximo(self):
        assert db_mod.proximo_reintento(db_mod.MAX_INTENTOS) is None


class TestRegistro:
    def test_anota_el_fallo_y_programa_el_siguiente(self, conn):
        intentos, cuando = db_mod.registrar_fallo(conn, 1, "ConnectError: sin ruta")
        fila = conn.execute("SELECT * FROM episodes WHERE id = 1").fetchone()
        assert intentos == 1 and cuando is not None
        assert fila["attempts"] == 1
        assert fila["next_retry_at"] == cuando
        assert "ConnectError" in fila["last_error"]

    def test_al_agotar_los_intentos_lo_marca_para_revisión(self, conn):
        for _ in range(db_mod.MAX_INTENTOS):
            _, cuando = db_mod.registrar_fallo(conn, 1, "404")
        fila = conn.execute("SELECT * FROM episodes WHERE id = 1").fetchone()
        assert cuando is None
        assert fila["needs_review"] == 1
        assert fila["next_retry_at"] is None

    def test_un_acierto_borra_el_historial(self, conn):
        db_mod.registrar_fallo(conn, 1, "fallo temporal")
        db_mod.limpiar_fallo(conn, 1)
        fila = conn.execute("SELECT * FROM episodes WHERE id = 1").fetchone()
        assert fila["attempts"] == 0
        assert fila["next_retry_at"] is None and fila["last_error"] is None


class TestLote:
    """`process --pending N` respeta la espera."""

    def _pendientes(self, db_path, n=10):
        resultado = CliRunner().invoke(
            app, ["process", "--pending", str(n), "--db", str(db_path)]
        )
        return resultado

    def test_un_episodio_en_espera_no_bloquea_el_lote(self, conn, tmp_path, monkeypatch):
        ruta = tmp_path / "kb.sqlite"
        futuro = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
        conn.execute("UPDATE episodes SET next_retry_at = ? WHERE id = 1", (futuro,))
        conn.commit()

        vistos = []
        from podcast_kb import pipeline

        def falso(conn_, episode_id, **kw):
            vistos.append(episode_id)
            raise RuntimeError("simulado")

        monkeypatch.setattr(pipeline, "process_episode", falso)
        self._pendientes(ruta)
        assert 1 not in vistos, "el episodio en espera no debería intentarse"
        assert set(vistos) == {2, 3}

    def test_los_ya_transcritos_no_se_repiten(self, conn, tmp_path, monkeypatch):
        ruta = tmp_path / "kb.sqlite"
        conn.execute("UPDATE episodes SET md_path = 'x.md' WHERE id = 2")
        conn.commit()

        vistos = []
        from podcast_kb import pipeline
        monkeypatch.setattr(
            pipeline, "process_episode",
            lambda c, i, **k: vistos.append(i) or (_ for _ in ()).throw(RuntimeError("x")),
        )
        self._pendientes(ruta)
        assert 2 not in vistos

    def test_sin_pendientes_no_falla(self, conn, tmp_path):
        ruta = tmp_path / "kb.sqlite"
        conn.execute("UPDATE episodes SET md_path = 'x.md'")
        conn.commit()
        resultado = self._pendientes(ruta)
        assert resultado.exit_code == 0
        assert "No hay episodios pendientes" in resultado.output

    def test_no_se_admite_id_y_pending_a_la_vez(self, conn, tmp_path):
        resultado = CliRunner().invoke(
            app, ["process", "1", "--pending", "5", "--db", str(tmp_path / "kb.sqlite")]
        )
        assert resultado.exit_code != 0
