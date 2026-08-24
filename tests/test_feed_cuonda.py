"""Regresión contra el feed real de monos estocásticos (Cuonda).

Este feed usa Podcasting 2.0 en serio: declara la transcripción en cuatro
formatos por episodio, capítulos, y los hablantes con <podcast:person>.
Destapó que se estaba eligiendo el formato equivocado.
"""

from pathlib import Path

import pytest

from podcast_kb.feeds import parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "feed_monos.xml"
FEED_URL = "https://cuonda.com/monos-estocasticos/feed"


@pytest.fixture(scope="module")
def feed():
    return parse_feed(FIXTURE.read_bytes(), base_url=FEED_URL)


class TestCanal:
    def test_metadatos(self, feed):
        assert feed.title == "monos estocásticos"
        assert feed.self_link == FEED_URL

    def test_stylesheet_y_crlf_no_estorban(self, feed):
        """El feed trae <?xml-stylesheet?> antes de la raíz y saltos CRLF."""
        assert len(feed.items) >= 2


class TestPreferenciaDeFormato:
    """Cada item declara plain/txt, x-subrip, srt y vtt. `find()` devolvía
    el primero del documento —plain/txt, sin marcas de tiempo—, que es el
    peor para lo que hacemos (§5.12)."""

    def test_elige_el_formato_con_marcas_de_tiempo(self, feed):
        ep = feed.items[0]
        assert ep.feed_transcript_type == "text/vtt"
        assert ep.transcript_has_timestamps

    def test_orden_de_preferencia(self):
        import xml.etree.ElementTree as ET

        from podcast_kb.feeds import _pick_transcript

        item = ET.fromstring(
            '<item xmlns:podcast="https://podcastindex.org/namespace/1.0">'
            '<podcast:transcript type="plain/txt" url="https://x/t.txt"/>'
            '<podcast:transcript type="application/x-subrip" url="https://x/t.srt"/>'
            '<podcast:transcript type="text/vtt" url="https://x/t.vtt"/>'
            "</item>"
        )
        assert _pick_transcript(item) == ("https://x/t.vtt", "text/vtt")

    def test_sin_vtt_se_queda_con_el_subrip(self):
        import xml.etree.ElementTree as ET

        from podcast_kb.feeds import _pick_transcript

        item = ET.fromstring(
            '<item xmlns:podcast="https://podcastindex.org/namespace/1.0">'
            '<podcast:transcript type="plain/txt" url="https://x/t.txt"/>'
            '<podcast:transcript type="application/x-subrip" url="https://x/t.srt"/>'
            "</item>"
        )
        assert _pick_transcript(item)[1] == "application/x-subrip"

    def test_texto_plano_no_cuenta_como_utilizable(self):
        """Sin marcas de tiempo no hay salto al minuto: hay que transcribir igual."""
        from podcast_kb.feeds import EpisodeItem

        ep = EpisodeItem(
            guid="g", title="t", published_at="2026-01-01", audio_url="u",
            enclosure_sha256="h", feed_transcript_type="plain/txt",
        )
        assert not ep.transcript_has_timestamps

    def test_sin_transcripcion(self):
        import xml.etree.ElementTree as ET

        from podcast_kb.feeds import _pick_transcript

        assert _pick_transcript(ET.fromstring("<item/>")) == (None, None)


class TestPersonas:
    """<podcast:person> resuelve de gratis el mapeo de hablantes de §5.11."""

    def test_extrae_los_hablantes(self, feed):
        assert feed.items[0].persons == ["Antonio Ortiz", "Matías S. Zavia"]

    def test_todos_los_episodios_los_declaran(self, feed):
        assert all(len(i.persons) == 2 for i in feed.items)


class TestEnclosures:
    def test_sin_tracking_ni_url_incrustada(self, feed):
        from podcast_kb.urls import unwrap_trackers

        for item in feed.items:
            assert item.audio_url.startswith("https://cuonda.com/download/")
            assert unwrap_trackers(item.audio_url) == item.audio_url
