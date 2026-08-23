from pathlib import Path

import pytest

from podcast_kb.feeds import (
    extract_episode_number,
    parse_duration,
    parse_feed,
)
from podcast_kb.urls import enclosure_sha256

FIXTURE = Path(__file__).parent / "fixtures" / "feed_ejemplo.xml"


@pytest.fixture(scope="module")
def feed():
    return parse_feed(FIXTURE.read_bytes())


class TestParseDuration:
    @pytest.mark.parametrize(
        "raw,esperado",
        [
            ("1:03:02", 3782),
            ("45:10", 2710),
            ("2705", 2705),
            ("2705.0", 2705),
            ("", None),
            (None, None),
            ("no es una duración", None),
            ("1:2:3:4", None),
        ],
    )
    def test_formatos(self, raw, esperado):
        assert parse_duration(raw) == esperado


class TestEpisodeNumber:
    def test_desde_titulo(self):
        assert extract_episode_number("MCP en producción — Ep. 120") == 120

    def test_con_almohadilla(self):
        assert extract_episode_number("La Tertul-IA #36: La IA controla tu ordenador") == 36

    def test_sin_numero(self):
        assert extract_episode_number("Agentes IA: destripando los enigmas") is None


class TestParseFeed:
    def test_metadatos_del_canal(self, feed):
        assert feed.title == "El Test de Turing - IA Aplicada a Negocio"
        assert feed.language == "es"  # 'es-ES' se normaliza a 'es'

    def test_ignora_items_sin_audio(self, feed):
        assert len(feed.items) == 3
        assert all("ignorarse" not in i.title for i in feed.items)

    def test_campos_del_primer_episodio(self, feed):
        ep = feed.items[0]
        assert ep.guid == "e1671d44-0001"
        assert ep.published_at == "2026-07-15T06:00:00+00:00"
        assert ep.duration_sec == 3782
        assert ep.audio_bytes == 60512000
        assert ep.episode_number == 121  # de <itunes:episode>, no del título
        assert ep.episode_url == "https://open.spotify.com/episode/17erE3notlcJ3pyFXqY3YW"

    def test_enclosure_con_tracking_se_normaliza_para_el_hash(self, feed):
        ep = feed.items[0]
        # La URL de descarga se conserva tal cual la publica el feed...
        assert ep.audio_url.startswith("https://dts.podtrac.com/")
        # ...pero la identidad es la del fichero real que hay detrás.
        assert ep.enclosure_sha256 == enclosure_sha256("https://traffic.megaphone.fm/EP121.mp3")

    def test_podcasting20_transcript_y_chapters(self, feed):
        """§4.4: si el feed ya publica transcripción, nos ahorramos Whisper."""
        ep = feed.items[0]
        assert ep.feed_transcript_url == "https://ejemplo.com/ep121.vtt"
        assert ep.feed_transcript_type == "text/vtt"
        assert ep.feed_chapters_url == "https://ejemplo.com/ep121-chapters.json"

    def test_episodio_sin_podcasting20(self, feed):
        ep = feed.items[1]
        assert ep.feed_transcript_url is None
        assert ep.feed_chapters_url is None
        assert ep.episode_number == 120  # del título, no hay <itunes:episode>

    def test_guid_ausente_cae_al_hash_del_enclosure(self, feed):
        """§4.2, regla 2: sin <guid>, la identidad es el enclosure."""
        ep = feed.items[2]
        assert ep.guid == f"sha256:{enclosure_sha256('https://traffic.megaphone.fm/EP119.mp3')}"
        assert ep.duration_sec is None
