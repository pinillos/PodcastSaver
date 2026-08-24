"""Regresión contra el feed real de La Tertul-IA (Anchor).

Es el podcast del aviso de §2.1 (existe un homónimo) y §2.2 (sus episodios
salen también dentro de Growth, con otro guid).
"""

from pathlib import Path

import pytest

from podcast_kb.feeds import parse_feed
from podcast_kb.urls import unwrap_trackers

FIXTURE = Path(__file__).parent / "fixtures" / "feed_tertulia.xml"
FEED_URL = "https://anchor.fm/s/107b28204/podcast/rss"


@pytest.fixture(scope="module")
def feed():
    return parse_feed(FIXTURE.read_bytes(), base_url=FEED_URL)


class TestIdentidad:
    def test_es_el_podcast_correcto_no_el_homonimo(self, feed):
        """§2.1: no confundir con 'La TERTULia de la Inteligencia Artificial'."""
        assert feed.title == "La Tertul-IA: Inteligencia Artificial y más"

    def test_su_propio_feed(self, feed):
        assert feed.self_link == FEED_URL


class TestEnclosures:
    def test_anchor_incrusta_la_url_del_cdn(self, feed):
        for item in feed.items:
            assert item.audio_url.startswith("https://anchor.fm/s/")
            assert "cloudfront.net" in unwrap_trackers(item.audio_url)

    def test_la_identidad_es_la_del_cdn(self, feed):
        """§2.2: si el mismo audio sale en el feed de Growth con otro id de
        reproducción, tiene que dar el mismo hash."""
        from podcast_kb.urls import enclosure_sha256

        item = feed.items[0]
        real = unwrap_trackers(item.audio_url)
        assert item.enclosure_sha256 == enclosure_sha256(real)


class TestCapitulos:
    def test_extrae_los_del_indice_de_las_notas(self, feed):
        con = [i for i in feed.items if i.chapters]
        assert con, "el fixture incluye un episodio con índice"
        assert con[0].chapters_source == "notes"
        assert len(con[0].chapters) >= 3
