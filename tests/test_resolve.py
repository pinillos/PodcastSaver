import httpx
import pytest

from podcast_kb.feeds import FeedError, inspect_feed, resolve_feed_from_apple_id

FEED = (b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
 <channel><title>El Test de Turing</title><language>es-ES</language>
  <item><title>A</title><guid>g1</guid>
   <pubDate>Tue, 15 Jul 2026 06:00:00 GMT</pubDate>
   <enclosure url="https://dts.podtrac.com/redirect.mp3/traffic.megaphone.fm/A.mp3"
              type="audio/mpeg"/>
   <podcast:transcript url="https://x/a.vtt" type="text/vtt"/></item>
  <item><title>B</title><guid>g2</guid>
   <pubDate>Tue, 08 Jul 2026 06:00:00 GMT</pubDate>
   <enclosure url="https://traffic.megaphone.fm/B.mp3" type="audio/mpeg"/></item>
 </channel></rss>""")

APPLE_OK = {"resultCount": 1, "results": [{
    "collectionName": "El Test de Turing - IA Aplicada a Negocio",
    "artistName": "Álvaro Peña", "feedUrl": "https://anchor.fm/s/e1671d44/podcast/rss",
    "collectionViewUrl": "https://podcasts.apple.com/x"}]}


def client_for(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for key, response in routes.items():
            if key in str(request.url):
                return response
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class TestResolveDesdeApple:
    def test_devuelve_feedurl_y_metadatos(self):
        c = client_for({"itunes.apple.com": httpx.Response(200, json=APPLE_OK)})
        out = resolve_feed_from_apple_id("1771978939", client=c)
        assert out["rss_url"] == "https://anchor.fm/s/e1671d44/podcast/rss"
        assert out["title"] == "El Test de Turing - IA Aplicada a Negocio"

    def test_sin_resultados(self):
        c = client_for({"itunes": httpx.Response(200, json={"resultCount": 0, "results": []})})
        with pytest.raises(FeedError, match="no devuelve resultados"):
            resolve_feed_from_apple_id("1723256857", client=c)

    def test_podcast_exclusivo_sin_feedurl(self):
        """Un show solo-Spotify no expone feedUrl: hay que decirlo claro."""
        payload = {"resultCount": 1, "results": [{"collectionName": "Exclusivo"}]}
        c = client_for({"itunes": httpx.Response(200, json=payload)})
        with pytest.raises(FeedError, match="no expone feedUrl"):
            resolve_feed_from_apple_id("1723736263", client=c)


class TestInspectFeed:
    def test_diagnostico_completo(self):
        c = client_for({"ejemplo.com": httpx.Response(200, content=FEED)})
        info = inspect_feed("https://ejemplo.com/f.xml", client=c)
        assert info.ok
        assert info.title == "El Test de Turing"
        assert info.language == "es"
        assert info.n_items == 2
        assert info.first_published[:10] == "2026-07-08"
        assert info.last_published[:10] == "2026-07-15"

    def test_cuenta_transcripciones_publicadas(self):
        """§4.4: si el feed las trae, nos ahorramos transcribir."""
        c = client_for({"ejemplo.com": httpx.Response(200, content=FEED)})
        assert inspect_feed("https://ejemplo.com/f.xml", client=c).n_transcripts == 1

    def test_detecta_el_hosting_real_tras_el_tracker(self):
        c = client_for({"ejemplo.com": httpx.Response(200, content=FEED)})
        info = inspect_feed("https://ejemplo.com/f.xml", client=c)
        assert "dts.podtrac.com" in info.trackers
        assert "→ traffic.megaphone.fm" in info.trackers

    def test_feed_caido_no_rompe(self):
        c = client_for({})
        info = inspect_feed("https://ejemplo.com/f.xml", client=c)
        assert not info.ok and info.status == 404

    def test_xml_corrupto_no_rompe(self):
        c = client_for({"ejemplo.com": httpx.Response(200, content=b"no soy xml")})
        info = inspect_feed("https://ejemplo.com/f.xml", client=c)
        assert info.n_items == 0

    def test_registra_la_url_final_tras_redireccion(self):
        """§2.3: se persiste la final, no la inicial."""
        def handler(request):
            if "vieja" in str(request.url):
                return httpx.Response(301, headers={"Location": "https://nueva.com/f.xml"})
            return httpx.Response(200, content=FEED)

        c = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        info = inspect_feed("https://vieja.com/f.xml", client=c)
        assert info.ok and info.final_url == "https://nueva.com/f.xml"


class TestExtractAppleId:
    """pod.link y Apple comparten identificador: aceptar ambas formas."""

    @pytest.mark.parametrize(
        "entrada",
        [
            "1723256857",
            "https://pod.link/1723256857",
            "http://pod.link/1723256857",
            "https://pod.link/1723256857/episode/abc123",
            "pod.link/1723256857",
            "https://podcasts.apple.com/es/podcast/inteligencia-artificial-semanal/id1723256857",
            "https://podcasts.apple.com/us/podcast/x/id1723256857?i=1000675294760",
            "https://podcasts.apple.com/podcast/id1723256857",
        ],
    )
    def test_todas_las_formas_dan_el_mismo_id(self, entrada):
        from podcast_kb.feeds import extract_apple_id

        assert extract_apple_id(entrada) == "1723256857"

    @pytest.mark.parametrize("entrada", ["", "   ", "no-hay-id-aqui", "https://ejemplo.com/feed.xml"])
    def test_entrada_invalida_da_un_error_util(self, entrada):
        from podcast_kb.feeds import extract_apple_id

        with pytest.raises(FeedError, match="No encuentro un ID"):
            extract_apple_id(entrada)

    def test_resolve_acepta_una_url_directamente(self):
        c = client_for({"itunes.apple.com": httpx.Response(200, json=APPLE_OK)})
        out = resolve_feed_from_apple_id("https://pod.link/1723256857", client=c)
        assert out["apple_id"] == "1723256857"
        assert out["rss_url"] == "https://anchor.fm/s/e1671d44/podcast/rss"
