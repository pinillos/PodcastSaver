"""Feeds autoalojados: convenciones que Anchor/iVoox no traen.

Un feed hecho a mano o por un generador estático usa cosas que los hostings
grandes normalizan: rutas relativas, Media RSS, items sin guid, el namespace
de Podcasting 2.0 con la otra URI. Cada una de estas se rompía en silencio.
"""

from pathlib import Path

import pytest

from podcast_kb.feeds import parse_feed
from podcast_kb.urls import enclosure_sha256, normalize_enclosure_url, resolve_relative

FIXTURE = Path(__file__).parent / "fixtures" / "feed_autoalojado.xml"
FEED_URL = "https://podcast.ejemplo.com/feed.xml"


@pytest.fixture(scope="module")
def feed():
    return parse_feed(FIXTURE.read_bytes(), base_url=FEED_URL)


class TestRutasRelativas:
    def test_absoluta_desde_la_raiz(self):
        assert (
            resolve_relative("/audio/ep1.mp3", "https://podcast.ejemplo.com/rss/feed.xml")
            == "https://podcast.ejemplo.com/audio/ep1.mp3"
        )

    def test_relativa_al_directorio_del_feed(self):
        assert (
            resolve_relative("audio/ep1.mp3", "https://podcast.ejemplo.com/rss/feed.xml")
            == "https://podcast.ejemplo.com/rss/audio/ep1.mp3"
        )

    def test_una_url_absoluta_no_se_toca(self):
        assert resolve_relative("https://otro.com/a.mp3", FEED_URL) == "https://otro.com/a.mp3"

    def test_sin_base_no_inventa_nada(self):
        assert resolve_relative("/audio/ep1.mp3", None) == "/audio/ep1.mp3"

    def test_el_hash_de_identidad_ya_no_sale_corrupto(self):
        """Antes producía `https:///audio/ep1.mp3`, que ni descarga ni identifica."""
        canonical = normalize_enclosure_url("/audio/ep1.mp3", FEED_URL)
        assert canonical == "https://podcast.ejemplo.com/audio/ep1.mp3"
        assert enclosure_sha256("/audio/ep1.mp3", FEED_URL) == enclosure_sha256(canonical)


class TestParseAutoalojado:
    def test_no_se_pierde_ningun_episodio(self, feed):
        assert len(feed.items) == 4

    def test_atom_link_self(self, feed):
        assert feed.self_link == FEED_URL

    def test_resuelve_el_enclosure_relativo(self, feed):
        assert feed.items[0].audio_url == "https://podcast.ejemplo.com/audio/ep001.mp3"

    def test_relativa_al_directorio(self, feed):
        assert feed.items[3].audio_url == "https://podcast.ejemplo.com/audio/ep004.mp3"

    def test_prefiere_el_audio_sobre_el_video(self, feed):
        """Un podcast que también sale en YouTube publica el .mp4 primero."""
        assert feed.items[1].audio_url.endswith(".mp3")

    def test_soporta_media_content(self, feed):
        ep = feed.items[2]
        assert ep.audio_url == "https://podcast.ejemplo.com/a/ep003.mp3"
        assert ep.audio_bytes == 999

    def test_transcripcion_con_la_otra_uri_del_namespace(self, feed):
        assert feed.items[0].feed_transcript_url.endswith("ep001.vtt")

    def test_transcripcion_en_un_item_sin_guid(self, feed):
        """Indexar por guid perdía esto en silencio; ahora va por posición."""
        ep = feed.items[3]
        assert ep.guid.startswith("sha256:")
        assert ep.feed_transcript_url.endswith("ep004.srt")
        assert ep.feed_transcript_type == "application/srt"

    def test_capitulos_psc_en_linea(self, feed):
        assert feed.items[2].feed_chapters_url == "psc:inline"


class TestRobustez:
    def test_bom_al_principio(self):
        """El BOM rompe ElementTree pero no feedparser: desalineaba los dos."""
        raw = b"\xef\xbb\xbf" + FIXTURE.read_bytes()
        parsed = parse_feed(raw, base_url=FEED_URL)
        assert len(parsed.items) == 4
        assert parsed.items[0].feed_transcript_url is not None

    def test_xml_corrupto_no_lanza(self):
        assert parse_feed(b"esto no es xml").items == []

    def test_feed_sin_items(self):
        raw = b'<?xml version="1.0"?><rss version="2.0"><channel><title>V</title></channel></rss>'
        assert parse_feed(raw).items == []

    def test_base_desde_el_link_del_canal_si_no_hay_otra(self):
        """Sin base_url explícita, vale el atom:link self o el <link>."""
        parsed = parse_feed(FIXTURE.read_bytes())
        assert parsed.items[0].audio_url == "https://podcast.ejemplo.com/audio/ep001.mp3"
