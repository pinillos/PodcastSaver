"""Regresiones de la revisión de seguridad.

Estos tests reactivan la validación anti-SSRF que `conftest.py` desactiva
para el resto de la suite: aquí lo que se comprueba es precisamente que
bloquea.
"""

from __future__ import annotations

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
