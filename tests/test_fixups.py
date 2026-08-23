import pytest

from podcast_kb.fixups import apply_fixups, load_fixups, strip_prompt_echo
from podcast_kb.segments import Segment

TABLA = """# comentario que se ignora
fain tuning\tfine-tuning\tanglicismo
fine tuning\tfine-tuning
Subtítulos realizados por la comunidad de Amara.org\t\talucinación
"""


@pytest.fixture
def fixups(tmp_path):
    path = tmp_path / "fixups.tsv"
    path.write_text(TABLA, encoding="utf-8")
    return load_fixups(path)


class TestLoad:
    def test_ignora_comentarios_y_vacios(self, fixups):
        assert len(fixups) == 3

    def test_fichero_ausente_no_rompe(self, tmp_path):
        assert load_fixups(tmp_path / "no-existe.tsv") == []

    def test_patron_invalido_da_error_util(self, tmp_path):
        path = tmp_path / "malo.tsv"
        path.write_text("[sin-cerrar\tx\n", encoding="utf-8")
        with pytest.raises(ValueError, match="patrón inválido"):
            load_fixups(path)


class TestApply:
    def test_corrige_anglicismos(self, fixups):
        segs = [Segment(0, 5, "hicimos fain tuning del modelo")]
        out, n = apply_fixups(segs, fixups)
        assert out[0].text == "hicimos fine-tuning del modelo"
        assert n == 1

    def test_insensible_a_mayusculas(self, fixups):
        out, _ = apply_fixups([Segment(0, 5, "Fine Tuning aplicado")], fixups)
        assert out[0].text == "fine-tuning aplicado"

    def test_reemplazo_vacio_borra_la_alucinacion(self, fixups):
        """§5.8: la frase fantasma clásica de los silencios."""
        segs = [
            Segment(0, 5, "Contenido real"),
            Segment(5, 9, "Subtítulos realizados por la comunidad de Amara.org"),
        ]
        out, n = apply_fixups(segs, fixups)
        assert len(out) == 1 and out[0].text == "Contenido real"
        assert n == 1

    def test_conserva_timestamps_y_hablante(self, fixups):
        segs = [Segment(10.5, 20.25, "fain tuning", speaker="Corti")]
        out, _ = apply_fixups(segs, fixups)
        assert (out[0].start, out[0].end, out[0].speaker) == (10.5, 20.25, "Corti")


class TestStripPromptEcho:
    PROMPT = "En este episodio hablamos de LLMs, embeddings, fine-tuning, RAG y agentes"

    def test_descarta_el_eco(self):
        """§5.7: Whisper a veces continúa el prompt en vez de condicionarse."""
        segs = [
            Segment(0, 3, "embeddings fine-tuning RAG agentes LLMs"),
            Segment(3, 8, "Bienvenidos al episodio de hoy"),
        ]
        assert strip_prompt_echo(segs, self.PROMPT) is True
        assert len(segs) == 1 and segs[0].text.startswith("Bienvenidos")

    def test_no_toca_una_apertura_normal(self):
        segs = [Segment(0, 5, "Muy buenas a todos, bienvenidos una semana más")]
        assert strip_prompt_echo(segs, self.PROMPT) is False
        assert len(segs) == 1

    def test_sin_prompt_no_hace_nada(self):
        segs = [Segment(0, 5, "embeddings fine-tuning RAG agentes")]
        assert strip_prompt_echo(segs, "") is False
        assert len(segs) == 1

    def test_segmento_muy_corto_no_se_descarta(self):
        segs = [Segment(0, 1, "embeddings RAG")]
        assert strip_prompt_echo(segs, self.PROMPT) is False
