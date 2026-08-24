"""Regresión contra el feed real de Inteligencia Artificial Semanal.

Feed autoalojado en podcast.lamesalimon.com. Fixture recortado a 4 items
representativos del original de 95. Al pasarlo por el parser aparecieron dos
fallos que ninguna prueba sintética había cazado.
"""

from pathlib import Path

import pytest

from podcast_kb.feeds import parse_feed

FIXTURE = Path(__file__).parent / "fixtures" / "feed_lamesalimon.xml"
FEED_URL = "https://podcast.lamesalimon.com/lamesalimon.xml"


@pytest.fixture(scope="module")
def feed():
    return parse_feed(FIXTURE.read_bytes(), base_url=FEED_URL)


class TestCanal:
    def test_metadatos(self, feed):
        assert feed.title == "Inteligencia Artificial Semanal"
        assert feed.language == "es"  # 'es-es' se normaliza

    def test_todos_los_items_se_parsean(self, feed):
        assert len(feed.items) == 4


class TestNumeroDeEpisodio:
    """Este podcast NO numera episodios; los números del título son
    versiones de modelos."""

    def test_no_inventa_numeros_de_versiones_de_modelos(self, feed):
        for item in feed.items:
            assert item.episode_number is None, (
                f"falso positivo en {item.title!r}: {item.episode_number}"
            )

    @pytest.mark.parametrize(
        "titulo",
        [
            "Llama 4, Reward Models, Test de Turing",
            "[DESARROLLO] Veo 3, Gemini Difussion, Hybrid LRM",
            "Burbuja sí o no, Gemini 3 y Antigravity, TiDAR Híbrido",
            "PrismML modelos de 1 bit, Aprendizajes sobre LRMs",
            "Errores en GenAI, Helix 02 con Locomanipulation",
        ],
    )
    def test_titulos_con_numeros_que_no_son_episodios(self, titulo):
        from podcast_kb.feeds import extract_episode_number

        assert extract_episode_number(titulo) is None

    @pytest.mark.parametrize(
        "titulo,esperado",
        [
            ("MCP en producción — Ep. 120", 120),
            ("La Tertul-IA #36: La IA controla tu ordenador", 36),
            ("Episodio 7: agentes", 7),
            ("Episode 42", 42),
        ],
    )
    def test_sigue_leyendo_los_marcadores_explicitos(self, titulo, esperado):
        from podcast_kb.feeds import extract_episode_number

        assert extract_episode_number(titulo) == esperado


class TestEpisodeUrl:
    """El <guid> es `20250107`, sin isPermaLink="false". Por la spec eso lo
    hace permalink, y feedparser lo promueve a entry.link."""

    def test_no_se_cuela_el_guid_como_url(self, feed):
        for item in feed.items:
            assert item.episode_url is None, f"guid promovido a URL: {item.episode_url}"

    def test_el_guid_si_se_conserva_como_identidad(self, feed):
        assert feed.items[0].guid == "20250107"

    @pytest.mark.parametrize(
        "raw,esperado",
        [
            ("20250107", None),
            ("ep1", None),
            ("javascript:alert(1)", None),
            ("https://x.com/ep1", "https://x.com/ep1"),
            ("/episodios/ep1", "https://podcast.lamesalimon.com/episodios/ep1"),
        ],
    )
    def test_criterio(self, raw, esperado):
        from podcast_kb.feeds import _clean_episode_url

        assert _clean_episode_url(raw, FEED_URL) == esperado


class TestAudio:
    def test_autoalojado_sin_tracking(self, feed):
        """Sin prefijos de medición: checksum_audio sirve y no hay deriva (§9.1)."""
        from podcast_kb.urls import unwrap_trackers

        for item in feed.items:
            assert item.audio_url.startswith("https://podcast.lamesalimon.com/episodes/")
            assert unwrap_trackers(item.audio_url) == item.audio_url

    def test_duracion_en_segundos(self, feed):
        assert feed.items[0].duration_sec == 1528

    def test_tamano_del_enclosure(self, feed):
        assert feed.items[0].audio_bytes == 23164800

    def test_sin_transcripcion_publicada(self, feed):
        """No hay atajo de §4.4 aquí: hay que transcribirlo todo."""
        assert all(i.feed_transcript_url is None for i in feed.items)
