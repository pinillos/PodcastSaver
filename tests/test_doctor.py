"""Tests del verificador. Estaba al 0% de cobertura, y es el módulo que
comprueba que el RLS está puesto: si diera un falso «ok», el corpus quedaría
público sin que nadie se enterase."""

from __future__ import annotations

import pytest

from podcast_kb import db as db_mod
from podcast_kb import doctor


class CursorFalso:
    """Postgres simulado: se le dice qué debe responder a cada consulta."""

    def __init__(self, *, rls=None, vector=None, indices=(), rpc=True, chunks=0, sin_vector=0):
        self.rls = rls if rls is not None else {"podcasts": True, "episodes": True, "chunks": True}
        self.vector = vector
        self.indices = indices
        self.rpc = rpc
        self.chunks = chunks
        self.sin_vector = sin_vector
        self._r = None

    def execute(self, sql, params=()):
        low = " ".join(sql.split()).lower()
        if "relrowsecurity" in low:
            self._r = list(self.rls.items())
        elif "pg_extension" in low:
            self._r = [(self.vector,)] if self.vector else None
        elif "pg_indexes" in low:
            self._r = [(i,) for i in self.indices]
        elif "pg_proc" in low:
            self._r = [(1,)] if self.rpc else None
        elif "embedding is null" in low:
            self._r = (self.sin_vector,)
        elif "count(*) from chunks" in low:
            self._r = (self.chunks,)
        return self

    def fetchone(self):
        return self._r if isinstance(self._r, tuple) else (self._r[0] if self._r else None)

    def fetchall(self):
        return self._r or []


@pytest.fixture
def conexion(monkeypatch):
    """Sustituye psycopg.connect por el cursor falso."""
    estado = {}

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return estado["cursor"]

    class FalsoPsycopg:
        @staticmethod
        def connect(dsn, **kw): return Conn()

    import sys
    monkeypatch.setitem(sys.modules, "psycopg", FalsoPsycopg)
    return estado


def estados(comprobaciones, nombre):
    return [c for c in comprobaciones if c.nombre == nombre]


class TestRls:
    """La comprobación que importa."""

    def test_avisa_en_rojo_si_falta_el_rls(self, conexion):
        conexion["cursor"] = CursorFalso(
            rls={"podcasts": False, "episodes": False, "chunks": False}
        )
        r = doctor.comprobar_backend("postgresql://x")
        rls = estados(r, "RLS")[0]
        assert rls.estado == doctor.ERROR
        assert "PÚBLICO" in rls.detalle

    def test_una_sola_tabla_sin_rls_ya_es_error(self, conexion):
        conexion["cursor"] = CursorFalso(
            rls={"podcasts": True, "episodes": True, "chunks": False}
        )
        rls = estados(doctor.comprobar_backend("postgresql://x"), "RLS")[0]
        assert rls.estado == doctor.ERROR and "chunks" in rls.detalle

    def test_en_verde_cuando_está_puesto(self, conexion):
        conexion["cursor"] = CursorFalso()
        rls = estados(doctor.comprobar_backend("postgresql://x"), "RLS")[0]
        assert rls.estado == doctor.OK

    def test_si_faltan_tablas_no_dice_que_el_rls_esté_bien(self, conexion):
        """Sin tablas no hay nada que proteger, pero tampoco nada que afirmar."""
        conexion["cursor"] = CursorFalso(rls={})
        r = doctor.comprobar_backend("postgresql://x")
        assert estados(r, "esquema")[0].estado == doctor.ERROR
        assert estados(r, "RLS") == []


class TestBackend:
    def test_pgvector_ausente(self, conexion):
        conexion["cursor"] = CursorFalso(vector=None)
        assert estados(doctor.comprobar_backend("x"), "pgvector")[0].estado == doctor.ERROR

    def test_pgvector_antiguo_avisa_del_recall(self, conexion):
        conexion["cursor"] = CursorFalso(vector="0.7.0")
        r = doctor.comprobar_backend("x")
        assert estados(r, "pgvector")[0].estado == doctor.OK
        assert estados(r, "hnsw.iterative_scan")[0].estado == doctor.AVISO

    def test_pgvector_reciente_no_avisa(self, conexion):
        conexion["cursor"] = CursorFalso(vector="0.8.1")
        assert estados(doctor.comprobar_backend("x"), "hnsw.iterative_scan") == []

    def test_indices_y_rpc_ausentes(self, conexion):
        conexion["cursor"] = CursorFalso(indices=(), rpc=False)
        r = doctor.comprobar_backend("x")
        assert estados(r, "chunks_tsv_es_idx")[0].estado == doctor.ERROR
        assert estados(r, "hybrid_search")[0].estado == doctor.ERROR

    def test_chunks_sin_vector(self, conexion):
        conexion["cursor"] = CursorFalso(chunks=100, sin_vector=12)
        assert estados(doctor.comprobar_backend("x"), "embeddings")[0].estado == doctor.AVISO

    def test_una_caida_no_revienta_el_diagnostico(self, monkeypatch):
        class Explota:
            @staticmethod
            def connect(dsn, **kw): raise OSError("no hay ruta al host")
        import sys
        monkeypatch.setitem(sys.modules, "psycopg", Explota)
        r = doctor.comprobar_backend("postgresql://x")
        assert r[0].estado == doctor.ERROR and "OSError" in r[0].detalle


class TestLocal:
    def test_detecta_lo_que_falta(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PODCAST_KB_ROOT", str(tmp_path))
        (tmp_path / "config").mkdir()
        (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
        r = doctor.comprobar_local(tmp_path / "no-existe.sqlite")
        assert estados(r, "config/podcasts.yaml")[0].estado == doctor.ERROR
        assert estados(r, "SQLite")[0].estado == doctor.AVISO

    def test_avisa_del_escape_de_ssrf(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PODCAST_KB_ALLOW_PRIVATE_URLS", "1")
        monkeypatch.setenv("PODCAST_KB_ROOT", str(tmp_path))
        (tmp_path / "config").mkdir()
        aviso = estados(doctor.comprobar_local(tmp_path / "x.sqlite"),
                        "PODCAST_KB_ALLOW_PRIVATE_URLS")[0]
        assert aviso.estado == doctor.AVISO and "SSRF" in aviso.detalle

    def test_detecta_una_service_role_en_el_cliente(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PODCAST_KB_ROOT", str(tmp_path))
        (tmp_path / "config").mkdir()
        (tmp_path / "web").mkdir()
        (tmp_path / "web" / "config.json").write_text(
            '{"anonKey":"x","serviceRoleKey":"service_role-abc"}', encoding="utf-8"
        )
        c = estados(doctor.comprobar_local(tmp_path / "x.sqlite"), "web/config.json")[0]
        assert c.estado == doctor.ERROR and "rótala" in c.detalle

    def test_cuenta_los_episodios(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PODCAST_KB_ROOT", str(tmp_path))
        (tmp_path / "config").mkdir()
        ruta = tmp_path / "kb.sqlite"
        conn = db_mod.connect(ruta)
        db_mod.init_schema(conn)
        pid = db_mod.upsert_podcast(conn, {"slug": "x", "title": "X", "rss_url": "r",
                                           "language": "es"})
        db_mod.insert_episode_if_new(conn, {
            "podcast_id": pid, "guid": "g", "enclosure_sha256": "h", "title": "T",
            "published_at": "2026-01-01", "audio_url": "u"})
        conn.close()
        assert "1 episodios" in estados(doctor.comprobar_local(ruta), "SQLite")[0].detalle
