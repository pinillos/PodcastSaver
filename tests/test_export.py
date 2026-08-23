import yaml

from podcast_kb.export import (
    ExportInput,
    build_front_matter,
    episode_filename,
    md_path_for,
    meta_checksum,
    render_markdown,
    split_front_matter,
    transcript_checksum,
    write_episode,
)
from podcast_kb.segments import Segment


def make(**kw) -> ExportInput:
    base = dict(
        podcast="El Test de Turing",
        podcast_slug="test-de-turing",
        episode_title="Agentes IA: Destripando los enigmas",
        published_at="2026-07-15T06:00:00Z",
        language="es",
        audio_url="https://ejemplo.com/ep.mp3",
        guid="e1671d44-0001",
        segments=[Segment(0, 5, "Hola qué tal"), Segment(62, 70, "Hablemos de MCP")],
        authors=["Álvaro Peña", "Arnau Vendrell"],
        episode_number=121,
        duration_sec=3782,
        transcribed_duration_sec=3821,
        transcribed_at="2026-08-21T10:22:00+00:00",
    )
    base.update(kw)
    return ExportInput(**base)


class TestRutas:
    def test_nombre_de_fichero(self):
        assert (
            episode_filename("2026-07-15T06:00:00Z", "Agentes IA: Destripando los enigmas")
            == "2026-07-15-agentes-ia-destripando-los-enigmas.md"
        )

    def test_ruta_completa(self):
        path = md_path_for("test-de-turing", "2026-07-15T06:00:00Z", "Agentes IA")
        assert path.as_posix() == "transcripts/test-de-turing/2026-07-15-agentes-ia.md"


class TestFrontMatter:
    def test_es_yaml_valido_y_completo(self):
        md = render_markdown(make())
        raw, _ = split_front_matter(md)
        fm = yaml.safe_load(raw)
        assert fm["schema_version"] == 2
        assert fm["podcast_slug"] == "test-de-turing"
        assert fm["authors"] == ["Álvaro Peña", "Arnau Vendrell"]
        assert fm["language"] == "es"
        assert fm["transcript"]["engine"] == "whisper.cpp"
        assert fm["transcript"]["vad"] is True

    def test_duraciones_separadas(self):
        """§9.1: la del feed y la del audio transcrito no son la misma."""
        fm = build_front_matter(make())
        assert fm["duration_sec"] == 3782
        assert fm["transcribed_duration_sec"] == 3821

    def test_los_checksums_de_reindexado_no_van_dentro(self):
        """§8.5: serían autorreferenciales; viven en Postgres."""
        fm = build_front_matter(make())
        assert "transcript_checksum" not in fm
        assert "meta_checksum" not in fm

    def test_marca_de_revision(self):
        fm = build_front_matter(make(needs_review=True, review_reason="7× «gracias por ver»"))
        assert fm["transcript"]["needs_review"] is True
        assert fm["transcript"]["review_reason"] == "7× «gracias por ver»"

    def test_campos_opcionales_se_omiten(self):
        fm = build_front_matter(
            make(episode_number=None, duration_sec=None, transcribed_duration_sec=None,
                 authors=[], episode_url=None)
        )
        for ausente in ("episode_number", "duration_sec", "transcribed_duration_sec",
                        "authors", "episode_url"):
            assert ausente not in fm


class TestCuerpo:
    def test_titulo_y_timestamps(self):
        md = render_markdown(make())
        assert "# Agentes IA: Destripando los enigmas (Ep. 121)" in md
        assert "## Transcripción" in md
        assert "[00:00:00] Hola qué tal" in md
        assert "[00:01:02] Hablemos de MCP" in md

    def test_titulo_sin_numero_de_episodio(self):
        assert "(Ep." not in render_markdown(make(episode_number=None))


class TestChecksums:
    def test_reenriquecer_no_invalida_los_embeddings(self):
        """§8.5: es justo el motivo de tener dos checksums en vez de uno."""
        original = render_markdown(make())
        reenriquecido = render_markdown(
            make(enrichment={"summary": "Un resumen nuevo", "topics": ["agentes"]})
        )
        assert transcript_checksum(original) == transcript_checksum(reenriquecido)
        assert meta_checksum(original) != meta_checksum(reenriquecido)

    def test_retranscribir_si_invalida(self):
        original = render_markdown(make())
        otro = render_markdown(make(segments=[Segment(0, 5, "Texto distinto")]))
        assert transcript_checksum(original) != transcript_checksum(otro)

    def test_sin_front_matter(self):
        raw, body = split_front_matter("# Solo cuerpo\n")
        assert raw == "" and body == "# Solo cuerpo\n"


class TestEscritura:
    def test_escribe_en_la_ruta_correcta(self, tmp_path):
        path = write_episode(make(), root=tmp_path)
        assert path.relative_to(tmp_path).as_posix() == (
            "test-de-turing/2026-07-15-agentes-ia-destripando-los-enigmas.md"
        )
        contenido = path.read_text(encoding="utf-8")
        assert contenido.startswith("---\n")
        assert yaml.safe_load(split_front_matter(contenido)[0])["guid"] == "e1671d44-0001"
