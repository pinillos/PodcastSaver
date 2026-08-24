"""El camino de indexado, sin base de datos: un cursor falso registra el SQL."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from podcast_kb.embeddings import HashingEmbedder, to_pgvector
from podcast_kb.export import ExportInput, write_episode
from podcast_kb.index import IndexReport, chunks_for, discover, index_all, load_document
from podcast_kb.segments import SEGMENTS_SUFFIX, Segment, save_segments


class FakeCursor:
    """Cursor mínimo que devuelve ids y recuerda lo ejecutado."""

    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []
        self._next = None
        self.chunks_borrados = 0
        self.chunks_insertados: list[tuple] = []
        self.embeddings_existentes: dict[str, str] = {}

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        low = " ".join(sql.split()).lower()
        if "insert into podcasts" in low:
            self._next = ("11111111-1111-1111-1111-111111111111",)
        elif low.startswith("select id, transcript_checksum from episodes"):
            self._next = None
        elif "insert into episodes" in low:
            self._next = ("22222222-2222-2222-2222-222222222222",)
        elif "select content, embedding from chunks" in low:
            self._rows = [(c, e) for c, e in self.embeddings_existentes.items()
                          if c in (params[1] or [])]
            self._next = None
        elif low.startswith("delete from chunks"):
            self.chunks_borrados += 1
        elif "insert into chunks" in low:
            self.chunks_insertados.append(params)
        return self

    def fetchone(self):
        return self._next

    def fetchall(self):
        return getattr(self, "_rows", [])


@pytest.fixture
def repo(tmp_path):
    """Dos episodios .md con sus segmentos crudos."""
    for n, titulo in enumerate(["Agentes y MCP", "Fine-tuning en local"], start=1):
        segs = [Segment(i * 5.0, i * 5.0 + 5, f"contenido del episodio {n} parte {i}")
                for i in range(60)]
        data = ExportInput(
            podcast="El Test de Turing", podcast_slug="test-de-turing",
            episode_title=titulo, published_at=f"2026-0{n}-15T06:00:00Z",
            language="es", audio_url=f"https://x/{n}.mp3", guid=f"g{n}",
            segments=segs, authors=["Álvaro Peña"], duration_sec=300,
            transcribed_at="2026-08-24T12:00:00+00:00",
        )
        md = write_episode(data, root=tmp_path)
        save_segments(segs, md.parent / (md.stem + SEGMENTS_SUFFIX))
    return tmp_path


class TestDescubrimiento:
    def test_encuentra_los_md(self, repo):
        assert len(discover(repo)) == 2

    def test_ignora_los_segments(self, repo):
        assert all(p.suffix == ".md" for p in discover(repo))


class TestCarga:
    def test_lee_el_front_matter(self, repo):
        doc = load_document(discover(repo)[0])
        assert doc.podcast_slug == "test-de-turing"
        assert doc.guid == "g1"

    def test_calcula_los_dos_checksums(self, repo):
        doc = load_document(discover(repo)[0])
        assert doc.transcript_checksum != doc.meta_checksum
        assert len(doc.transcript_checksum) == 64

    def test_front_matter_incompleto(self, tmp_path):
        malo = tmp_path / "x" / "malo.md"
        malo.parent.mkdir()
        malo.write_text("---\npodcast_slug: x\n---\n\ncuerpo\n", encoding="utf-8")
        with pytest.raises(ValueError, match="falta `guid`"):
            load_document(malo)


class TestChunksFor:
    def test_parte_de_los_segmentos_crudos(self, repo):
        """§7.2: el .md agrupa para humanos; el índice usa la materia prima."""
        doc = load_document(discover(repo)[0])
        assert doc.segments_path.exists()
        assert len(chunks_for(doc)) > 1

    def test_cae_al_markdown_si_falta_el_json(self, repo):
        doc = load_document(discover(repo)[0])
        doc.segments_path.unlink()
        piezas = chunks_for(doc)
        assert piezas and all(p.content for p in piezas)


class TestIndexAll:
    def test_indexa_los_dos_episodios(self, repo):
        cur = FakeCursor()
        report = index_all(cur, HashingEmbedder(), root=repo)
        assert report.scanned == 2 and report.inserted == 2
        assert report.chunks == len(cur.chunks_insertados)

    def test_borra_antes_de_insertar(self, repo):
        cur = FakeCursor()
        index_all(cur, HashingEmbedder(), root=repo)
        assert cur.chunks_borrados == 2

    def test_los_chunks_llevan_las_columnas_denormalizadas(self, repo):
        """B.1: sin ellas, filtrar destruye el recall de HNSW."""
        cur = FakeCursor()
        index_all(cur, HashingEmbedder(), root=repo)
        params = cur.chunks_insertados[0]
        assert params[6] == "11111111-1111-1111-1111-111111111111"  # podcast_id
        assert params[8] == "es"                                     # language
        assert params[9].startswith("[")                             # embedding

    def test_reutiliza_los_embeddings_ya_almacenados(self, repo):
        """B.4: re-embeder texto idéntico es pagar dos veces."""
        cur = FakeCursor()
        primero = index_all(cur, HashingEmbedder(), root=repo)
        assert primero.embedded > 0 and primero.cached == 0

        cur2 = FakeCursor()
        cur2.embeddings_existentes = {p[5]: p[9] for p in cur.chunks_insertados}
        segundo = index_all(cur2, HashingEmbedder(), root=repo)
        assert segundo.embedded == 0
        assert segundo.cached == segundo.chunks

    def test_un_md_corrupto_no_para_el_resto(self, repo):
        (repo / "test-de-turing" / "roto.md").write_text(
            "---\nno: valido\n---\n\ncuerpo\n", encoding="utf-8"
        )
        cur = FakeCursor()
        report = index_all(cur, HashingEmbedder(), root=repo)
        assert report.inserted == 2 and len(report.errors) == 1


class TestEmbedder:
    def test_dimensiones_y_norma(self):
        v = HashingEmbedder().embed_passages(["hola"])[0]
        assert len(v) == 1024
        assert abs(sum(x * x for x in v) ** 0.5 - 1.0) < 1e-6

    def test_prefijos_e5_distintos(self):
        """§8.2: usar uno sin el otro degrada el recall en silencio."""
        e = HashingEmbedder()
        assert e.embed_query("hola") != e.embed_passages(["hola"])[0]

    def test_literal_de_pgvector(self):
        assert to_pgvector([1.0, -0.5]) == "[1,-0.5]"
