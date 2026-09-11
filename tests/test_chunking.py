
from podcast_kb.chunking import chunk_segments, describe
from podcast_kb.segments import Segment


def hacer(n=240, dur=5.0, texto="frase con contenido suficiente para el índice", speaker=None):
    return [Segment(i * dur, i * dur + dur, f"{texto} {i}", speaker) for i in range(n)]


class TestTamano:
    def test_ronda_el_objetivo(self):
        chunks = chunk_segments(hacer(), target_sec=90, max_sec=150)
        st = describe(chunks)
        assert 80 <= st.mean_sec <= 120

    def test_cubre_el_episodio_entero(self):
        segs = hacer()
        chunks = chunk_segments(segs)
        assert chunks[0].start_sec == segs[0].start
        assert chunks[-1].end_sec == segs[-1].end

    def test_indices_correlativos(self):
        chunks = chunk_segments(hacer())
        assert [c.idx for c in chunks] == list(range(len(chunks)))

    def test_lista_vacia(self):
        assert chunk_segments([]) == []

    def test_un_solo_segmento(self):
        chunks = chunk_segments([Segment(0, 5, "corto")])
        assert len(chunks) == 1 and chunks[0].content == "corto"


class TestSolape:
    def test_los_chunks_se_solapan(self):
        """§8.1: sin solape se parte una idea a la mitad."""
        chunks = chunk_segments(hacer())
        assert all(chunks[i + 1].start_sec < chunks[i].end_sec for i in range(len(chunks) - 1))

    def test_el_solape_ronda_el_20_por_ciento(self):
        chunks = chunk_segments(hacer(), overlap_ratio=0.20)
        ratios = [
            (chunks[i].end_sec - chunks[i + 1].start_sec) / chunks[i].duration_sec
            for i in range(len(chunks) - 1)
        ]
        assert 0.10 <= sum(ratios) / len(ratios) <= 0.35

    def test_siempre_avanza(self):
        """Un solape mal calculado podría dejar el bucle parado."""
        chunks = chunk_segments(hacer(n=50), overlap_ratio=0.95)
        assert len(chunks) < 200
        assert all(chunks[i + 1].start_sec > chunks[i].start_sec for i in range(len(chunks) - 1))


class TestFronteras:
    def test_nunca_parte_un_segmento(self):
        """§8.1: las fronteras caen entre segmentos de whisper, no dentro."""
        segs = hacer()
        validos = {s.start for s in segs}
        for chunk in chunk_segments(segs):
            assert chunk.start_sec in validos

    def test_se_alinea_con_los_capitulos(self):
        segs = hacer(n=120)
        capitulos = [
            {"start": 0.0, "title": "Intro"},
            {"start": 150.0, "title": "Tema"},
            {"start": 300.0, "title": "Cierre"},
        ]
        chunks = chunk_segments(segs, chapters=capitulos)
        inicios = {c.start_sec for c in chunks}
        assert 150.0 in inicios and 300.0 in inicios

    def test_etiqueta_el_capitulo(self):
        chunks = chunk_segments(
            hacer(n=120), chapters=[{"start": 0.0, "title": "Intro"}, {"start": 150.0, "title": "Tema"}]
        )
        assert chunks[0].chapter == "Intro"
        assert any(c.chapter == "Tema" for c in chunks)

    def test_un_segmento_larguisimo_no_se_parte(self):
        chunks = chunk_segments([Segment(0, 400, "un segmento de siete minutos")])
        assert len(chunks) == 1


class TestHablante:
    def test_atribuye_si_domina(self):
        segs = [Segment(i * 5.0, i * 5.0 + 5, "texto largo del mismo hablante", "Corti")
                for i in range(20)]
        assert all(c.speaker == "Corti" for c in chunk_segments(segs))

    def test_no_atribuye_si_esta_repartido(self):
        """En una tertulia, atribuir por mayoría simple sería ruido."""
        voces = ["Lu Martín", "Frankie Carrero", "Corti"]
        segs = [Segment(i * 5.0, i * 5.0 + 5, "texto de longitud pareja", voces[i % 3])
                for i in range(30)]
        assert all(c.speaker is None for c in chunk_segments(segs))

    def test_sin_diarizacion(self):
        assert all(c.speaker is None for c in chunk_segments(hacer()))


class TestColas:
    def test_una_cola_corta_se_pega_al_anterior(self):
        segs = [*hacer(n=40), Segment(200.5, 201.0, "ok")]
        chunks = chunk_segments(segs)
        assert all(len(c.content) >= 40 for c in chunks)
