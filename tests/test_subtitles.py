import pytest

from podcast_kb.subtitles import SubtitleError, looks_timestamped, parse_subtitles

VTT = """WEBVTT

NOTE
Este bloque no es diálogo.

1
00:00:00.000 --> 00:00:04.200 align:start position:0%
<v Antonio Ortiz>Hola, bienvenidos a monos estocásticos.

2
00:00:04.200 --> 00:00:09.000
<v Matías S. Zavia>Hoy hablamos de <i>fine-tuning</i>
y de agentes.

00:01:02.500 --> 00:01:08.000
Un cue sin identificador &amp; con entidades.
"""

SRT = """1
00:00:00,000 --> 00:00:04,200
Hola qué tal

2
00:00:04,200 --> 00:00:09,000
Segunda línea
partida en dos
"""


class TestVtt:
    def test_numero_de_segmentos(self):
        assert len(parse_subtitles(VTT)) == 3

    def test_marcas_de_tiempo(self):
        segs = parse_subtitles(VTT)
        assert (segs[0].start, segs[0].end) == (0.0, 4.2)
        assert (segs[2].start, segs[2].end) == (62.5, 68.0)

    def test_hablantes_desde_las_etiquetas_v(self):
        """§5.11 gratis: el subtítulo ya dice quién habla."""
        segs = parse_subtitles(VTT)
        assert segs[0].speaker == "Antonio Ortiz"
        assert segs[1].speaker == "Matías S. Zavia"
        assert segs[2].speaker is None

    def test_quita_etiquetas_y_une_lineas(self):
        assert parse_subtitles(VTT)[1].text == "Hoy hablamos de fine-tuning y de agentes."

    def test_entidades_html(self):
        assert "&" in parse_subtitles(VTT)[2].text

    def test_ignora_cabecera_y_notas(self):
        assert all("WEBVTT" not in s.text and "no es diálogo" not in s.text
                   for s in parse_subtitles(VTT))

    def test_horas_opcionales(self):
        vtt = "WEBVTT\n\n01:02.500 --> 01:08.000\nSin horas\n"
        assert parse_subtitles(vtt)[0].start == 62.5

    def test_bom(self):
        assert len(parse_subtitles("﻿" + VTT)) == 3

    def test_crlf(self):
        assert len(parse_subtitles(VTT.replace("\n", "\r\n"))) == 3


class TestSrt:
    def test_coma_como_separador_decimal(self):
        segs = parse_subtitles(SRT)
        assert (segs[0].start, segs[0].end) == (0.0, 4.2)

    def test_no_confunde_el_indice_con_texto(self):
        segs = parse_subtitles(SRT)
        assert len(segs) == 2
        assert segs[1].text == "Segunda línea partida en dos"

    def test_bytes(self):
        assert len(parse_subtitles(SRT.encode("utf-8"))) == 2


class TestErrores:
    def test_fichero_sin_marcas_de_tiempo(self):
        with pytest.raises(SubtitleError, match="marcas de tiempo"):
            parse_subtitles("Esto es texto plano, sin cues.")

    def test_fichero_vacio(self):
        with pytest.raises(SubtitleError):
            parse_subtitles("")

    def test_cue_vacio_se_descarta(self):
        vtt = "WEBVTT\n\n00:00.000 --> 00:04.000\n\n00:05.000 --> 00:09.000\nTexto\n"
        segs = parse_subtitles(vtt)
        assert len(segs) == 1 and segs[0].text == "Texto"


class TestLooksTimestamped:
    @pytest.mark.parametrize(
        "mimetype,esperado",
        [
            ("text/vtt", True),
            ("application/x-subrip", True),
            ("text/srt", True),
            ("plain/txt", False),
            ("text/html", False),
            (None, False),
        ],
    )
    def test_criterio(self, mimetype, esperado):
        assert looks_timestamped(mimetype) is esperado
