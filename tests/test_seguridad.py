"""Regresiones de la revisión de seguridad.

Estos tests reactivan la validación anti-SSRF que `conftest.py` desactiva
para el resto de la suite: aquí lo que se comprueba es precisamente que
bloquea.
"""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml

from podcast_kb import config, net
from podcast_kb.export import md_path_for


@pytest.fixture
def sin_escape(monkeypatch):
    monkeypatch.setattr(net, "PERMITIR_PRIVADAS_POR_DEFECTO", False)


@pytest.mark.usefixtures("sin_escape")
class TestSsrf:
    """Las URLs de feed, audio y subtítulos vienen de terceros."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",  # metadatos de nube
            "http://127.0.0.1:5432/",                    # Postgres local
            "http://localhost/admin",
            "http://10.0.0.5/interno",
            "http://192.168.1.1/",
            "http://[::1]/",
        ],
    )
    def test_bloquea_destinos_no_publicos(self, url):
        with pytest.raises(net.UrlNoPermitida):
            net.validar_url(url)

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/y"])
    def test_bloquea_esquemas_no_http(self, url):
        with pytest.raises(net.UrlNoPermitida):
            net.validar_url(url)

    def test_permite_una_url_publica(self):
        net.validar_url("https://example.com/feed.xml")

    def test_un_host_que_no_resuelve_no_revienta(self, monkeypatch):
        def falso(*a, **kw):
            raise socket.gaierror("Name or service not known")
        monkeypatch.setattr(socket, "getaddrinfo", falso)
        with pytest.raises(net.UrlNoPermitida, match="no se resuelve"):
            net.validar_url("https://no-existe.example/f.xml")

    def test_valida_tambien_tras_una_redireccion(self):
        """Validar solo la primera URL no sirve: basta redirigir."""
        def handler(request: httpx.Request) -> httpx.Response:
            if "publico" in str(request.url):
                return httpx.Response(302, headers={"Location": "http://127.0.0.1:5432/"})
            return httpx.Response(200, content=b"secreto")

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        with pytest.raises(net.UrlNoPermitida):
            net.get_validado(client, "https://example.com/publico")

    def test_corta_un_bucle_de_redirecciones(self):
        def handler(request):
            return httpx.Response(302, headers={"Location": "https://example.com/otra"})

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        with pytest.raises(net.UrlNoPermitida, match="demasiadas redirecciones"):
            net.get_validado(client, "https://example.com/una")


class TestTopesDeTamano:
    """Un feed hostil puede servir un fichero enorme y agotar memoria o disco."""

    def test_rechaza_por_content_length_declarado(self):
        resp = httpx.Response(200, headers={"Content-Length": "999999999"}, content=b"x")
        with pytest.raises(net.DemasiadoGrande):
            net.leer_acotado(resp, 1024)

    def test_rechaza_aunque_el_servidor_mienta(self):
        """Sin Content-Length, o con uno falso, hay que cortar al leer."""
        resp = httpx.Response(200, content=b"x" * 5000)
        with pytest.raises(net.DemasiadoGrande):
            net.leer_acotado(resp, 1024)

    def test_deja_pasar_lo_que_cabe(self):
        resp = httpx.Response(200, content=b"x" * 100)
        assert len(net.leer_acotado(resp, 1024)) == 100


class TestRecorridoDeRutas:
    """El slug se usa como nombre de directorio."""

    @pytest.mark.parametrize(
        "slug", ["../../evadido", "/absoluto", "con espacios", "MAYUS", "a" * 100, ""]
    )
    def test_slug_invalido_rechazado_en_la_configuracion(self, slug, tmp_path):
        ruta = tmp_path / "p.yaml"
        ruta.write_text(
            yaml.safe_dump({"podcasts": [{"slug": slug, "language": "es", "rss_url": "https://x/f"}]}),
            encoding="utf-8",
        )
        with pytest.raises(config.ConfigError):
            config.load_podcasts(ruta)

    def test_slug_valido_aceptado(self, tmp_path):
        ruta = tmp_path / "p.yaml"
        ruta.write_text(
            yaml.safe_dump(
                {"podcasts": [{"slug": "monos-estocasticos", "language": "es",
                               "rss_url": "https://x/f"}]}
            ),
            encoding="utf-8",
        )
        assert config.load_podcasts(ruta)[0]["slug"] == "monos-estocasticos"

    def test_la_ruta_no_puede_salir_de_la_raiz(self):
        raiz = Path(tempfile.mkdtemp())
        with pytest.raises(ValueError, match="se sale de"):
            md_path_for("../../../tmp/evadido", "2026-07-15", "T", root=raiz)

    def test_una_ruta_normal_funciona(self):
        raiz = Path(tempfile.mkdtemp())
        ruta = md_path_for("test-de-turing", "2026-07-15", "Agentes", root=raiz)
        assert ruta.is_relative_to(raiz)


@pytest.fixture
def sin_proxy(monkeypatch):
    """Sin proxy en el entorno, que es cuando se fija la IP."""
    for nombre in net._VARIABLES_PROXY:
        monkeypatch.delenv(nombre, raising=False)
    monkeypatch.delenv("PODCAST_KB_PIN_DNS", raising=False)


def _resolver_a(monkeypatch, ip: str):
    familia = socket.AF_INET6 if ":" in ip else socket.AF_INET
    def falso(host, port, *args, **kwargs):
        return [(familia, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port or 0))]
    monkeypatch.setattr(socket, "getaddrinfo", falso)


@pytest.mark.usefixtures("sin_escape", "sin_proxy")
class TestFijarIp:
    """DNS rebinding: comprobar con una IP y conectar a otra.

    Resolver para validar y dejar que el cliente resuelva otra vez al
    conectar deja una ventana. Se conecta a la IP ya validada, con el Host
    y el SNI originales para que el certificado se siga verificando.
    """

    def _cliente(self, vistas, respuesta=None):
        def handler(request: httpx.Request) -> httpx.Response:
            vistas.append(request)
            return respuesta or httpx.Response(200, content=b"ok")
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)

    def test_conecta_a_la_ip_validada_no_al_nombre(self, monkeypatch):
        _resolver_a(monkeypatch, "93.184.216.34")
        vistas: list[httpx.Request] = []
        resp = net.get_validado(self._cliente(vistas), "https://example.com/feed.xml")
        resp.close()

        peticion = vistas[0]
        assert peticion.url.host == "93.184.216.34"
        assert peticion.headers["Host"] == "example.com"
        assert peticion.extensions["sni_hostname"] == "example.com"
        assert peticion.url.path == "/feed.xml"

    def test_ipv6_va_entre_corchetes(self, monkeypatch):
        _resolver_a(monkeypatch, "2606:2800:220:1:248:1893:25c8:1946")
        vistas: list[httpx.Request] = []
        resp = net.get_validado(self._cliente(vistas), "https://example.com/f.xml")
        resp.close()
        assert vistas[0].url.host == "2606:2800:220:1:248:1893:25c8:1946"

    def test_conserva_el_puerto(self, monkeypatch):
        _resolver_a(monkeypatch, "93.184.216.34")
        vistas: list[httpx.Request] = []
        resp = net.get_validado(self._cliente(vistas), "https://example.com:8443/f.xml")
        resp.close()
        assert vistas[0].url.port == 8443
        assert vistas[0].headers["Host"] == "example.com:8443"

    def test_conserva_las_credenciales_de_un_feed_privado(self, monkeypatch):
        """Con usuario y contraseña en la URL, perderlas se ve como un 401."""
        _resolver_a(monkeypatch, "93.184.216.34")
        vistas: list[httpx.Request] = []
        resp = net.get_validado(self._cliente(vistas), "https://ana:s3cr3t@example.com/f.xml")
        resp.close()
        assert vistas[0].url.host == "93.184.216.34"
        assert vistas[0].headers["Host"] == "example.com"
        assert "Authorization" in vistas[0].headers

    def test_la_url_final_es_el_nombre_no_la_ip(self, monkeypatch):
        """§2.3: lo que se persiste es la URL, y sirve de base para relativas."""
        _resolver_a(monkeypatch, "93.184.216.34")
        resp = net.get_validado(self._cliente([]), "https://example.com/feed.xml")
        resp.close()
        assert net.url_final(resp) == "https://example.com/feed.xml"

    def test_una_redireccion_relativa_se_resuelve_contra_el_nombre(self, monkeypatch):
        """Contra la IP daría https://93.184.216.34/otra: host equivocado."""
        _resolver_a(monkeypatch, "93.184.216.34")
        vistas: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            vistas.append(request)
            if len(vistas) == 1:
                return httpx.Response(302, headers={"Location": "/otra.xml"})
            return httpx.Response(200, content=b"ok")

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        resp = net.get_validado(client, "https://example.com/feed.xml")
        resp.close()
        assert vistas[1].headers["Host"] == "example.com"
        assert vistas[1].url.path == "/otra.xml"
        assert net.url_final(resp) == "https://example.com/otra.xml"

    def test_con_proxy_no_se_fija_la_ip(self, monkeypatch):
        """Detrás de un proxy resuelve el proxy: fijar la IP rompería el CONNECT."""
        _resolver_a(monkeypatch, "93.184.216.34")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:3128")
        vistas: list[httpx.Request] = []
        resp = net.get_validado(self._cliente(vistas), "https://example.com/f.xml")
        resp.close()
        assert vistas[0].url.host == "example.com"

    def test_se_puede_desactivar_explicitamente(self, monkeypatch):
        _resolver_a(monkeypatch, "93.184.216.34")
        monkeypatch.setenv("PODCAST_KB_PIN_DNS", "0")
        vistas: list[httpx.Request] = []
        resp = net.get_validado(self._cliente(vistas), "https://example.com/f.xml")
        resp.close()
        assert vistas[0].url.host == "example.com"

    def test_sigue_bloqueando_lo_privado_antes_de_conectar(self, monkeypatch):
        _resolver_a(monkeypatch, "127.0.0.1")
        vistas: list[httpx.Request] = []
        with pytest.raises(net.UrlNoPermitida):
            net.get_validado(self._cliente(vistas), "https://interno.example.com/f.xml")
        assert vistas == []


class TestLecturaEnStreaming:
    """El tope solo acota si se lee en streaming.

    Con el cuerpo ya descargado entero en memoria, comprobar el tamaño
    después no evita nada: el daño (RAM) ya está hecho.
    """

    def test_la_respuesta_llega_sin_leer(self):
        emitidos = []

        def cuerpo():
            emitidos.append(1)
            yield b"x" * 10

        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=cuerpo())),
            follow_redirects=False,
        )
        resp = net.get_validado(client, "https://example.com/f.xml")
        try:
            assert emitidos == []       # nadie ha tocado el cuerpo todavía
            assert net.leer_acotado(resp, 1024) == b"x" * 10
        finally:
            resp.close()

    def test_corta_sin_haber_leido_el_resto(self):
        emitidos = []

        def cuerpo():
            for _ in range(1000):           # 64 MB si se leyera entero
                emitidos.append(1)
                yield b"x" * 65536

        resp = httpx.Response(200, content=cuerpo())
        with pytest.raises(net.DemasiadoGrande):
            net.leer_acotado(resp, 128 * 1024, que="el audio")
        assert len(emitidos) <= 4          # se paró al pasar el tope
