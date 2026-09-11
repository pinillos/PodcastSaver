"""Prueba la PWA en un navegador real.

Se salta si no hay Playwright o Chromium: el resto de la suite no debe
depender de ellos. Para ejecutarla:

    uv pip install playwright && playwright install chromium
    uv run pytest tests/e2e -v
"""

from __future__ import annotations

import json
import math
import shutil
import socket
import struct
import threading
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright", reason="Playwright no está instalado")

WEB = Path(__file__).resolve().parents[2] / "web"

RESPUESTA = {
    "query": "protocolo MCP",
    "semantic": True,
    "results": [
        {
            "episode_id": "ep-1",
            "title": "Agentes IA: destripando los enigmas",
            "episode_number": 121,
            "published_at": "2026-07-15T06:00:00Z",
            "duration_sec": 1800,
            "podcast": "El Test de Turing",
            "podcast_slug": "test-de-turing",
            "audio_url": "/ep.wav",
            "episode_url": None,
            "score": 0.03,
            "moments": [
                {
                    "start_sec": 932,
                    "end_sec": 1020,
                    "speaker": "Álvaro Peña",
                    "score": 0.03,
                    "content": (
                        "relleno inicial " * 20
                        + "hablamos del protocolo MCP y de cómo conecta herramientas "
                        + "relleno final " * 20
                    ),
                },
                {
                    "start_sec": 300,
                    "end_sec": 390,
                    "speaker": None,
                    "score": 0.02,
                    "content": "el protocolo MCP en producción",
                },
            ],
        }
    ],
}


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servidor(tmp_path_factory):
    raiz = tmp_path_factory.mktemp("web")
    for archivo in WEB.iterdir():
        if archivo.is_file():
            shutil.copy(archivo, raiz / archivo.name)

    # WAV de 30 minutos para que el salto tenga a dónde ir.
    with wave.open(str(raiz / "ep.wav"), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(
            b"".join(struct.pack("<h", int(2000 * math.sin(i / 40))) for i in range(8000 * 1800))
        )

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(raiz), **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/config.json":
                return self._json({
                    "supabaseUrl": "/auth-falso",
                    "anonKey": "anon",
                    "functionsUrl": "/functions/v1",
                    "podcasts": [],
                })
            if self.path == "/ep.wav" and "Range" in self.headers:
                # Sin 206 el navegador no puede buscar dentro del audio (§9).
                import re

                ruta = raiz / "ep.wav"
                tam = ruta.stat().st_size
                m = re.match(r"bytes=(\d*)-(\d*)", self.headers["Range"])
                ini = int(m.group(1) or 0)
                fin = min(int(m.group(2) or tam - 1), tam - 1)
                self.send_response(206)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
                self.send_header("Content-Length", str(fin - ini + 1))
                self.end_headers()
                with ruta.open("rb") as fh:
                    fh.seek(ini)
                    self.wfile.write(fh.read(fin - ini + 1))
                return
            return super().do_GET()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            return self._json(RESPUESTA)

        def _json(self, obj):
            raw = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    puerto = _puerto_libre()
    httpd = ThreadingHTTPServer(("127.0.0.1", puerto), Handler)
    hilo = threading.Thread(target=httpd.serve_forever, daemon=True)
    hilo.start()
    yield f"http://127.0.0.1:{puerto}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def pagina(servidor):
    from playwright.sync_api import sync_playwright

    chromium = "/opt/pw-browsers/chromium"
    with sync_playwright() as p:
        try:
            navegador = p.chromium.launch(
                executable_path=chromium if Path(chromium).exists() else None
            )
        except Exception as exc:  # pragma: no cover - depende del entorno
            pytest.skip(f"no hay Chromium disponible: {exc}")
        pagina = navegador.new_page(viewport={"width": 420, "height": 880})
        pagina.errores = []
        pagina.on("pageerror", lambda e: pagina.errores.append(str(e)))
        yield pagina, servidor
        navegador.close()


def _con_sesion(pg, base):
    """Inyecta una sesión válida sin pasar por el correo."""
    pg.goto(f"{base}/index.html", wait_until="domcontentloaded")
    pg.evaluate(
        "localStorage.setItem('podcast-kb.session', JSON.stringify("
        "{access_token:'t', refresh_token:'r', expires_at: Date.now()+3600000}))"
    )


class TestAutenticacion:
    """§7.5: sin sesión no hay búsqueda, y eso es lo que evita que el corpus
    sea público."""

    def test_sin_sesion_pide_entrar(self, pagina):
        pg, base = pagina
        pg.goto(f"{base}/index.html", wait_until="networkidle")
        pg.wait_for_selector("#entrar:not([hidden])", timeout=5000)
        assert pg.locator("#buscador").get_attribute("hidden") is not None

    def test_con_sesion_muestra_el_buscador(self, pagina):
        pg, base = pagina
        _con_sesion(pg, base)
        pg.goto(f"{base}/index.html", wait_until="networkidle")
        pg.wait_for_selector("#buscador:not([hidden])", timeout=5000)
        assert pg.locator("#entrar").get_attribute("hidden") is not None

    def test_el_token_del_fragmento_no_queda_en_la_url(self, pagina):
        """Un token en la barra acaba en el historial y en enlaces copiados."""
        pg, base = pagina
        pg.evaluate("localStorage.clear()")
        # El `?nuevo` fuerza una carga de documento: navegar cambiando solo el
        # fragmento no reejecuta el módulo. Un enlace de correo siempre abre
        # documento nuevo, así que esto reproduce el caso real.
        pg.goto(
            f"{base}/index.html?nuevo=1#access_token=abc123&refresh_token=r&expires_in=3600",
            wait_until="networkidle",
        )
        pg.wait_for_selector("#buscador:not([hidden])", timeout=5000)
        # Leído desde la página: pg.url puede quedar obsoleto tras un
        # replaceState, que no es una navegación.
        assert "access_token" not in pg.evaluate("location.href")
        assert pg.evaluate("location.hash") == ""
        assert pg.evaluate("localStorage.getItem('podcast-kb.session')")

    def test_cerrar_sesion(self, pagina):
        pg, base = pagina
        _con_sesion(pg, base)
        pg.goto(f"{base}/index.html", wait_until="networkidle")
        pg.wait_for_selector("#buscador:not([hidden])", timeout=5000)
        pg.click("#salir")
        pg.wait_for_selector("#entrar:not([hidden])", timeout=5000)


class TestBusqueda:
    def test_muestra_resultados(self, pagina):
        pg, base = pagina
        _con_sesion(pg, base)
        pg.goto(f"{base}/index.html?q=protocolo%20MCP", wait_until="networkidle")
        pg.wait_for_selector(".episodio", timeout=10000)
        assert pg.locator(".episodio").count() == 1
        assert pg.locator(".momento").count() == 2

    def test_resalta_los_terminos(self, pagina):
        pg, _ = pagina
        assert pg.locator("mark").count() > 0

    def test_recorta_el_chunk_en_un_extracto(self, pagina):
        """Un chunk son ~200 palabras: enseñarlo entero es ilegible en móvil."""
        pg, _ = pagina
        largos = pg.evaluate(
            "[...document.querySelectorAll('.momento .texto')].map(e => e.textContent.length)"
        )
        assert max(largos) < 400
        assert pg.locator(".mas").count() >= 1

    def test_se_puede_desplegar_el_fragmento(self, pagina):
        pg, _ = pagina
        antes = pg.evaluate("document.querySelector('.momento .texto').textContent.length")
        pg.locator(".mas").first.click()
        despues = pg.evaluate("document.querySelector('.momento .texto').textContent.length")
        assert despues > antes * 2

    def test_muestra_el_hablante(self, pagina):
        pg, _ = pagina
        assert pg.locator(".quien").count() >= 1


class TestReproductor:
    def test_salta_al_segundo_correcto(self, pagina):
        """§9: el margen de 10 s cubre la deriva por publicidad dinámica."""
        pg, base = pagina
        _con_sesion(pg, base)
        pg.goto(f"{base}/index.html?q=protocolo%20MCP", wait_until="networkidle")
        pg.wait_for_selector(".episodio", timeout=10000)
        pg.locator(".momento").first.click()
        pg.wait_for_selector("#reproductor:not([hidden])", timeout=5000)
        pg.wait_for_function("document.getElementById('audio').readyState >= 1", timeout=15000)
        pg.wait_for_timeout(600)
        actual = pg.evaluate("document.getElementById('audio').currentTime")
        assert abs(actual - (932 - 10)) < 3

    def test_no_recarga_al_saltar_dentro_del_mismo_episodio(self, pagina):
        pg, _ = pagina
        antes = pg.evaluate("document.getElementById('audio').src")
        pg.locator(".momento").nth(1).click()
        pg.wait_for_timeout(500)
        estado = pg.evaluate(
            "({src: document.getElementById('audio').src,"
            "  t: document.getElementById('audio').currentTime})"
        )
        assert estado["src"] == antes
        assert abs(estado["t"] - (300 - 10)) < 3

    def test_se_cierra(self, pagina):
        pg, _ = pagina
        pg.click("#cerrar")
        assert pg.locator("#reproductor").get_attribute("hidden") is not None


class TestSinErrores:
    def test_no_hay_excepciones_de_javascript(self, pagina):
        pg, _ = pagina
        assert pg.errores == []
