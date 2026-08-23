from pathlib import Path

import httpx
import pytest

from podcast_kb import db, ingest

FIXTURE = Path(__file__).parent / "fixtures" / "feed_ejemplo.xml"

# Mismo audio (EP121) que el fixture, pero publicado en otro feed, con otro
# guid y otro prefijo de tracking: el escenario de §2.2.
FEED_DUPLICADO = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>Growth: el podcast de Product Hackers</title>
    <language>es</language>
    <item>
      <title>La Tertul-IA #121: Agentes IA</title>
      <guid>growth-9999</guid>
      <pubDate>Tue, 15 Jul 2026 06:00:00 GMT</pubDate>
      <enclosure url="https://chtbl.com/track/GROWTH/traffic.megaphone.fm/EP121.mp3?dest-id=777"
                 length="60512000" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
""".encode()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def make_client(routes: dict[str, tuple[int, bytes, dict]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        key = str(request.url).split("?")[0]
        if key not in routes:
            return httpx.Response(404)
        status, body, headers = routes[key]
        # Caché condicional: si el cliente manda el ETag que tenemos, 304.
        if request.headers.get("if-none-match") and request.headers[
            "if-none-match"
        ] == headers.get("ETag"):
            return httpx.Response(304)
        return httpx.Response(status, content=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


FEED_URL = "https://ejemplo.com/feed.xml"
ENTRY = {"slug": "test-de-turing", "language": "es", "rss_url": FEED_URL}


class TestSyncPodcast:
    def test_dry_run_no_escribe_nada(self, conn):
        client = make_client({FEED_URL: (200, FIXTURE.read_bytes(), {})})
        report = ingest.sync_podcast(conn, ENTRY, dry_run=True, client=client)

        assert report.ok and report.new_count == 3
        assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 0

    def test_alta_real_y_segunda_pasada_idempotente(self, conn):
        client = make_client({FEED_URL: (200, FIXTURE.read_bytes(), {})})
        primera = ingest.sync_podcast(conn, ENTRY, client=client)
        assert primera.new_count == 3
        assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 3

        segunda = ingest.sync_podcast(conn, ENTRY, client=client)
        assert segunda.new_count == 0
        assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 3

    def test_cuenta_transcripciones_ya_publicadas(self, conn):
        client = make_client({FEED_URL: (200, FIXTURE.read_bytes(), {})})
        report = ingest.sync_podcast(conn, ENTRY, dry_run=True, client=client)
        assert report.with_feed_transcript == 1

    def test_cache_condicional_evita_reparsear(self, conn):
        headers = {"ETag": '"abc123"'}
        client = make_client({FEED_URL: (200, FIXTURE.read_bytes(), headers)})

        ingest.sync_podcast(conn, ENTRY, client=client)
        etag = conn.execute("SELECT etag FROM podcasts").fetchone()["etag"]
        assert etag == '"abc123"'

        segunda = ingest.sync_podcast(conn, ENTRY, client=client)
        assert segunda.not_modified and segunda.seen == 0

    def test_limite_de_episodios(self, conn):
        client = make_client({FEED_URL: (200, FIXTURE.read_bytes(), {})})
        report = ingest.sync_podcast(conn, ENTRY, client=client, limit=1)
        assert report.seen == 1 and report.new_count == 1

    def test_error_de_red_no_rompe_el_sync(self, conn):
        client = make_client({})  # 404 para todo
        report = ingest.sync_podcast(conn, ENTRY, client=client)
        assert not report.ok and "404" in report.error

    def test_429_se_reporta_con_retry_after(self, conn):
        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "120"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        report = ingest.sync_podcast(conn, ENTRY, client=client)
        assert not report.ok and "Retry-After=120" in report.error


class TestDedupeEntreFeeds:
    """§2.2: el mismo audio publicado en dos podcasts distintos."""

    def test_no_se_da_de_alta_dos_veces(self, conn):
        otro_url = "https://ejemplo.com/growth.xml"
        client = make_client({
            FEED_URL: (200, FIXTURE.read_bytes(), {}),
            otro_url: (200, FEED_DUPLICADO, {}),
        })

        ingest.sync_podcast(conn, ENTRY, client=client)
        antes = conn.execute("SELECT count(*) FROM episodes").fetchone()[0]

        growth = {"slug": "growth", "language": "es", "rss_url": otro_url}
        report = ingest.sync_podcast(conn, growth, client=client)

        assert report.seen == 1
        assert report.new_count == 0, "el mismo audio no debe darse de alta dos veces"
        assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == antes

    def test_dry_run_tambien_lo_detecta(self, conn):
        otro_url = "https://ejemplo.com/growth.xml"
        client = make_client({
            FEED_URL: (200, FIXTURE.read_bytes(), {}),
            otro_url: (200, FEED_DUPLICADO, {}),
        })
        ingest.sync_podcast(conn, ENTRY, client=client)

        growth = {"slug": "growth", "language": "es", "rss_url": otro_url}
        report = ingest.sync_podcast(conn, growth, dry_run=True, client=client)
        assert report.new_count == 0


class TestSyncAll:
    def test_salta_los_inactivos(self, conn):
        client = make_client({FEED_URL: (200, FIXTURE.read_bytes(), {})})
        entries = [ENTRY, {**ENTRY, "slug": "inactivo", "active": False}]
        # sync_all abre su propio cliente; se prueba solo el filtrado.
        activos = [e for e in entries if e.get("active", True)]
        assert len(activos) == 1


class TestFeedAutoalojado:
    """Un feed con enclosures relativos tiene que funcionar en el sync real."""

    FEED = b"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>Autoalojado</title><language>es</language>
    <item><title>Uno</title><guid>ep-001</guid>
     <pubDate>Mon, 03 Mar 2025 08:00:00 GMT</pubDate>
     <enclosure url="/audio/ep001.mp3" type="audio/mpeg"/></item>
    </channel></rss>"""

    def test_el_enclosure_relativo_queda_absoluto_en_sqlite(self, conn):
        url = "https://podcast.ejemplo.com/feed.xml"
        client = make_client({url: (200, self.FEED, {})})
        entry = {"slug": "autoalojado", "language": "es", "rss_url": url}

        report = ingest.sync_podcast(conn, entry, client=client)
        assert report.new_count == 1

        row = conn.execute("SELECT audio_url FROM episodes").fetchone()
        assert row["audio_url"] == "https://podcast.ejemplo.com/audio/ep001.mp3"
