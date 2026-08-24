"""Regresión contra el feed real de El Test de Turing (Anchor/Spotify).

Fixture recortado a 2 items del original de 166. Este feed destapó dos cosas
que el de lamesalimon no tenía: la URL real incrustada en el enclosure y un
índice de capítulos en las show notes.
"""

from pathlib import Path

import pytest

from podcast_kb.chapters import extract_chapters
from podcast_kb.feeds import parse_feed
from podcast_kb.urls import enclosure_sha256, unwrap_trackers

FIXTURE = Path(__file__).parent / "fixtures" / "feed_test_de_turing.xml"
FEED_URL = "https://anchor.fm/s/e1671d44/podcast/rss"

ANCHOR_URL = (
    "https://anchor.fm/s/e1671d44/podcast/play/124413788/"
    "https%3A%2F%2Fd3ctxlq1ktw2nl.cloudfront.net%2Fstaging%2F2026-7-19%2F"
    "430103226-44100-2-418a80c485e72.mp3"
)
CDN_URL = (
    "https://d3ctxlq1ktw2nl.cloudfront.net/staging/2026-7-19/"
    "430103226-44100-2-418a80c485e72.mp3"
)


@pytest.fixture(scope="module")
def feed():
    return parse_feed(FIXTURE.read_bytes(), base_url=FEED_URL)


class TestCanal:
    def test_metadatos(self, feed):
        assert feed.title.startswith("El Test de Turing")
        assert feed.language == "es"

    def test_atom_link_self(self, feed):
        assert feed.self_link == FEED_URL

    def test_titulos_en_cdata(self, feed):
        assert "Ep. 167" in feed.items[0].title


class TestUrlIncrustada:
    """Anchor mete la URL real percent-encoded tras un id que cambia por
    episodio. Quedarse con la externa rompería el dedupe de §2.2."""

    def test_desenvuelve_hasta_el_cdn(self):
        assert unwrap_trackers(ANCHOR_URL) == CDN_URL

    def test_la_identidad_no_depende_del_id_de_reproduccion(self):
        otro = ANCHOR_URL.replace("124413788", "999999999")
        assert enclosure_sha256(ANCHOR_URL) == enclosure_sha256(otro)
        assert enclosure_sha256(ANCHOR_URL) == enclosure_sha256(CDN_URL)

    def test_la_url_de_descarga_se_conserva_intacta(self, feed):
        """Se desenvuelve para identificar, no para descargar: el hosting
        espera su propia URL."""
        assert feed.items[0].audio_url.startswith("https://anchor.fm/s/e1671d44/podcast/play/")

    def test_una_url_normal_no_se_toca(self):
        normal = "https://podcast.lamesalimon.com/episodes/podcast_20250107.mp3"
        assert unwrap_trackers(normal) == normal


class TestCapitulosEnShowNotes:
    """§6: preferir siempre los capítulos del autor."""

    def test_los_extrae(self, feed):
        ep = feed.items[0]
        assert ep.chapters_source == "notes"
        assert len(ep.chapters) >= 5
        assert ep.chapters[0] == {"start": 0, "title": "Introducción"}

    def test_avanzan_en_el_tiempo(self, feed):
        starts = [c["start"] for c in feed.items[0].chapters]
        assert starts == sorted(starts)
        assert len(set(starts)) == len(starts)

    def test_no_se_salen_del_episodio(self, feed):
        ep = feed.items[0]
        assert ep.chapters[-1]["start"] < ep.duration_sec

    def test_un_episodio_sin_indice_no_inventa_capitulos(self, feed):
        ep = feed.items[1]
        assert ep.chapters == [] and ep.chapters_source == "none"


class TestExtractChapters:
    def test_formato_hh_mm_ss(self):
        notes = "<p>00:00:00 - Intro</p><p>00:01:16 - Tema uno</p><p>01:02:03 - Cierre</p>"
        assert extract_chapters(notes) == [
            {"start": 0, "title": "Intro"},
            {"start": 76, "title": "Tema uno"},
            {"start": 3723, "title": "Cierre"},
        ]

    def test_formato_mm_ss(self):
        notes = "0:00 - Intro\n5:30 - Medio\n42:10 - Final"
        assert [c["start"] for c in extract_chapters(notes)] == [0, 330, 2530]

    @pytest.mark.parametrize("sep", ["-", "–", "—", ":", "·", "|"])
    def test_separadores(self, sep):
        notes = f"00:00 {sep} Intro\n01:00 {sep} Dos\n02:00 {sep} Tres"
        assert len(extract_chapters(notes)) == 3

    def test_menos_de_tres_no_es_un_indice(self):
        assert extract_chapters("<p>Escúchalo en el 12:30 - es lo mejor</p>") == []

    def test_descarta_un_indice_que_retrocede(self):
        """Menciones sueltas de minutos, no un índice."""
        assert extract_chapters("10:00 - uno\n05:00 - dos\n01:00 - tres") == []

    def test_descarta_marcas_fuera_del_episodio(self):
        notes = "00:00 - Intro\n10:00 - Medio\n99:00:00 - Imposible"
        assert extract_chapters(notes, duration_sec=1800) == []

    def test_sin_descripcion(self):
        assert extract_chapters(None) == [] and extract_chapters("") == []

    def test_entidades_html(self):
        notes = "00:00 - Intro &amp; bienvenida\n01:00 - Qu&eacute; hay\n02:00 - Fin"
        assert extract_chapters(notes)[0]["title"] == "Intro & bienvenida"
