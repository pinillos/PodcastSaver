"""Regresiones de la revisión completa.

Cada test aquí corresponde a un fallo real encontrado revisando el sistema
entero, no a una función nueva.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from podcast_kb.chunking import chunk_segments, describe
from podcast_kb.export import episode_filename
from podcast_kb.index import load_document
from podcast_kb.segments import Segment
from podcast_kb.subtitles import parse_subtitles
from podcast_kb.transcribe import get_engine


class TestSubtituloNumerico:
    """Una línea de diálogo que es solo un número se confundía con el índice
    de orden de SRT y se descartaba."""

    def test_no_se_pierde_una_linea_que_es_solo_un_numero(self):
        vtt = (
            "WEBVTT\n\n00:00:00.000 --> 00:00:04.000\nEn 2024 pasó de todo\n\n"
            "00:00:04.000 --> 00:00:08.000\n2024\n\n"
            "00:00:08.000 --> 00:00:12.000\nfue el año de los agentes\n"
        )
        segs = parse_subtitles(vtt)
        assert len(segs) == 3
        assert segs[1].text == "2024"

    def test_el_indice_de_srt_sigue_descartandose(self):
        srt = "1\n00:00:00,000 --> 00:00:04,000\nHola\n\n2\n00:00:04,000 --> 00:00:08,000\nAdiós\n"
        segs = parse_subtitles(srt)
        assert [s.text for s in segs] == ["Hola", "Adiós"]


class TestRutaDeSalidaDeMlx:
    """mlx_whisper nombra la salida por el fichero de ENTRADA. Derivarla del
    prefijo coincidía por casualidad en el pipeline y fallaba en el bench."""

    def test_bench_encuentra_la_salida(self):
        engine = get_engine("mlx-whisper")
        wav = Path("/cache/ep.wav")
        prefijo = Path("/cache/bench/ep--mlx-whisper--turbo")
        assert engine.output_json_path(prefijo, wav) == Path("/cache/bench/ep.json")

    def test_whisper_cpp_usa_el_prefijo(self):
        engine = get_engine("whisper.cpp")
        assert engine.output_json_path(Path("/c/ep"), Path("/c/ep.wav")) == Path("/c/ep.json")


class TestColisionDeNombres:
    """El slug se recorta a 60 caracteres: dos títulos largos que empiezan
    igual producían el mismo fichero y uno sobrescribía al otro."""

    LARGO_A = "Analizamos a fondo el nuevo modelo de lenguaje que presenta OpenAI esta semana"
    LARGO_B = "Analizamos a fondo el nuevo modelo de lenguaje que presenta Google esta semana"

    def test_titulos_largos_no_colisionan(self):
        a = episode_filename("2026-07-15", self.LARGO_A, "guid-a")
        b = episode_filename("2026-07-15", self.LARGO_B, "guid-b")
        assert a != b

    def test_es_estable_entre_ejecuciones(self):
        assert episode_filename("2026-07-15", self.LARGO_A, "guid-a") == episode_filename(
            "2026-07-15", self.LARGO_A, "guid-a"
        )

    def test_un_titulo_corto_no_lleva_sufijo(self):
        assert episode_filename("2026-07-15", "Agentes IA", "g") == "2026-07-15-agentes-ia.md"


class TestFechaSinComillas:
    """YAML convierte una fecha ISO sin comillas en datetime."""

    def test_se_normaliza_a_texto(self, tmp_path):
        md = tmp_path / "p" / "ep.md"
        md.parent.mkdir()
        md.write_text(
            "---\npodcast_slug: x\nguid: g\npublished_at: 2026-07-15T06:00:00Z\n"
            "language: es\n---\n\n[00:00:00] hola qué tal\n",
            encoding="utf-8",
        )
        doc = load_document(md)
        assert isinstance(doc.front_matter["published_at"], str)
        assert doc.front_matter["published_at"].startswith("2026-07-15")

    def test_una_fecha_entrecomillada_no_se_toca(self, tmp_path):
        md = tmp_path / "p" / "ep.md"
        md.parent.mkdir()
        md.write_text(
            "---\npodcast_slug: x\nguid: g\npublished_at: '2026-07-15T06:00:00Z'\n"
            "language: es\n---\n\n[00:00:00] hola\n",
            encoding="utf-8",
        )
        assert load_document(md).front_matter["published_at"] == "2026-07-15T06:00:00Z"


def _segmentos(n=60):
    return [Segment(i * 5.0, i * 5.0 + 5, f"frase {i} con contenido suficiente") for i in range(n)]


class TestChunkingRobusto:
    def test_capitulo_sin_start_no_revienta(self):
        chunks = chunk_segments(_segmentos(), chapters=[{"title": "sin start"}, {"start": 0}])
        assert chunks

    def test_capitulo_con_start_no_numerico(self):
        chunks = chunk_segments(_segmentos(), chapters=[{"start": "ayer", "title": "x"}])
        assert chunks

    def test_capitulos_desordenados_se_etiquetan_bien(self):
        caps = [
            {"start": 300.0, "title": "C"},
            {"start": 0.0, "title": "A"},
            {"start": 150.0, "title": "B"},
        ]
        assert chunk_segments(_segmentos(), chapters=caps)[0].chapter == "A"

    def test_segmentos_desordenados_se_ordenan(self):
        desordenados = [Segment(50, 55, "tarde"), Segment(0, 5, "pronto"), Segment(25, 30, "medio")]
        chunks = chunk_segments(desordenados)
        assert chunks[0].start_sec == 0

    def test_capitulos_muy_juntos_no_fragmentan_el_indice(self):
        """Un capítulo cada 10 s producía chunks de 8 s: inservibles para
        recuperar y multiplicando por diez el tamaño del índice."""
        caps = [{"start": float(i * 10), "title": f"c{i}"} for i in range(30)]
        stats = describe(chunk_segments(_segmentos(), chapters=caps, target_sec=90))
        assert stats.mean_sec >= 40

    def test_los_capitulos_separados_si_marcan_frontera(self):
        caps = [{"start": 0.0, "title": "A"}, {"start": 150.0, "title": "B"}]
        inicios = {c.start_sec for c in chunk_segments(_segmentos(120), chapters=caps)}
        assert 150.0 in inicios
