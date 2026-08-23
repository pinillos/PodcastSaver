import pytest

from podcast_kb.segments import (
    Segment,
    detect_repetition,
    format_timestamp,
    group_into_blocks,
    load_segments,
    parse_transcript_json,
    render_body,
    save_segments,
    slugify,
)


class TestFormatTimestamp:
    @pytest.mark.parametrize(
        "seconds,esperado",
        [(0, "00:00:00"), (62, "00:01:02"), (3782, "01:03:02"), (-5, "00:00:00"), (59.9, "00:00:59")],
    )
    def test_formatos(self, seconds, esperado):
        assert format_timestamp(seconds) == esperado


class TestParseTranscriptJson:
    def test_whisper_cpp_usa_milisegundos(self):
        payload = {
            "transcription": [
                {"offsets": {"from": 0, "to": 5120}, "text": " Hola qué tal"},
                {"offsets": {"from": 5120, "to": 9000}, "text": " Bienvenidos"},
            ]
        }
        segs = parse_transcript_json(payload)
        assert [(s.start, s.end) for s in segs] == [(0.0, 5.12), (5.12, 9.0)]
        assert segs[0].text == "Hola qué tal"  # se recorta el espacio inicial

    def test_mlx_whisper_usa_segundos(self):
        payload = {"segments": [{"start": 0.0, "end": 5.12, "text": " Hola"}], "language": "es"}
        segs = parse_transcript_json(payload)
        assert (segs[0].start, segs[0].end, segs[0].text) == (0.0, 5.12, "Hola")

    def test_descarta_segmentos_vacios(self):
        payload = {"transcription": [{"offsets": {"from": 0, "to": 100}, "text": "   "}]}
        assert parse_transcript_json(payload) == []

    def test_formato_desconocido(self):
        with pytest.raises(ValueError, match="no reconocido"):
            parse_transcript_json({"algo": "raro"})


class TestGroupIntoBlocks:
    def test_agrupa_hasta_el_objetivo(self):
        segs = [Segment(i * 5.0, i * 5.0 + 5, f"t{i}") for i in range(20)]
        blocks = group_into_blocks(segs, target_sec=45, max_sec=75)
        assert len(blocks) > 1
        assert blocks[0].start == 0.0  # timestamp = start del primero (§7.2)
        for block in blocks:
            assert block.text  # ningún bloque vacío

    def test_no_parte_un_segmento(self):
        segs = [Segment(0, 200, "un segmento larguísimo")]
        blocks = group_into_blocks(segs)
        assert len(blocks) == 1
        assert blocks[0].text == "un segmento larguísimo"

    def test_cambio_de_hablante_corta_el_bloque(self):
        segs = [
            Segment(0, 4, "Hola", speaker="Frankie"),
            Segment(4, 8, "Qué tal", speaker="Frankie"),
            Segment(8, 12, "Muy bien", speaker="Corti"),
        ]
        blocks = group_into_blocks(segs)
        assert len(blocks) == 2
        assert blocks[0].speaker == "Frankie" and blocks[1].speaker == "Corti"

    def test_lista_vacia(self):
        assert group_into_blocks([]) == []


class TestRenderBody:
    def test_incluye_hablante_si_existe(self):
        blocks = group_into_blocks([Segment(165, 170, "Hola", speaker="Frankie")])
        assert render_body(blocks) == "[00:02:45] (Frankie) Hola"

    def test_sin_hablante(self):
        assert render_body(group_into_blocks([Segment(0, 5, "Hola")])) == "[00:00:00] Hola"


class TestRoundTrip:
    def test_guardar_y_cargar_conserva_todo(self, tmp_path):
        segs = [Segment(0, 5, "Hola qué tal", "Frankie"), Segment(5, 9, "Adiós")]
        path = save_segments(segs, tmp_path / "ep.segments.json.gz")
        assert path.exists()
        assert load_segments(path) == segs

    def test_se_guarda_comprimido(self, tmp_path):
        """§7.2: el JSON crudo pesa y comprime bien; el .md va sin comprimir."""
        segs = [Segment(i, i + 1, "texto repetitivo de relleno " * 10) for i in range(200)]
        path = save_segments(segs, tmp_path / "ep.segments.json.gz")
        crudo = sum(len(s.text) for s in segs)
        assert path.stat().st_size < crudo / 5


class TestSlugify:
    @pytest.mark.parametrize(
        "titulo,esperado",
        [
            ("Agentes IA: Destripando los enigmas", "agentes-ia-destripando-los-enigmas"),
            ("¿Qué es la IA General?", "que-es-la-ia-general"),
            ("La Tertul-IA #36", "la-tertul-ia-36"),
            ("Ñandú, año y camión", "nandu-ano-y-camion"),
            ("   ", "sin-titulo"),
            ("!!!", "sin-titulo"),
        ],
    )
    def test_ascii_minusculas_sin_espacios(self, titulo, esperado):
        assert slugify(titulo) == esperado

    def test_recorta_por_palabra(self):
        slug = slugify("palabra " * 40)
        assert len(slug) <= 60 and not slug.endswith("-")


class TestDetectRepetition:
    def test_detecta_el_bucle(self):
        """§5.8: un episodio degenerado no puede publicarse en silencio."""
        segs = [Segment(i, i + 1, "gracias por ver el vídeo hasta la próxima") for i in range(8)]
        detectado, motivo = detect_repetition(segs)
        assert detectado and "×" in motivo

    def test_texto_normal_no_dispara(self):
        segs = [
            Segment(0, 5, "Hoy hablamos de agentes y de cómo se orquestan"),
            Segment(5, 10, "El protocolo MCP permite conectar herramientas"),
            Segment(10, 15, "Veremos un ejemplo con un servidor local"),
        ]
        assert detect_repetition(segs) == (False, None)

    def test_texto_corto_no_dispara(self):
        assert detect_repetition([Segment(0, 1, "hola")]) == (False, None)
