"""Camino completo de la Fase 1 con dobles de ffmpeg/ffprobe/whisper-cli.

No hay binarios reales en CI, pero sí se ejercita todo lo que es nuestro: la
construcción de los comandos, el parseo de la salida, el post-proceso y los
artefactos. Los dobles imitan el formato exacto que producen los de verdad.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest
import yaml

from podcast_kb import db, pipeline
from podcast_kb.export import split_front_matter
from podcast_kb.segments import load_segments
from podcast_kb.transcribe import get_engine

WHISPER_JSON = {
    "result": {"language": "es"},
    "transcription": [
        {"offsets": {"from": 0, "to": 4200}, "text": " Muy buenas, bienvenidos una semana más."},
        {"offsets": {"from": 4200, "to": 9000}, "text": " Hoy hablamos de fain tuning."},
        {"offsets": {"from": 62000, "to": 68000}, "text": " Y del protocolo MCP."},
        {"offsets": {"from": 68000, "to": 71000},
         "text": " Subtítulos realizados por la comunidad de Amara.org"},
    ],
}


def _script(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """PATH con ffmpeg, ffprobe y whisper-cli falsos."""
    bindir = tmp_path / "bin"
    bindir.mkdir()

    # ffmpeg: crea el WAV de salida (último argumento).
    _script(bindir / "ffmpeg", '#!/bin/sh\nfor a in "$@"; do last="$a"; done\n'
                               'printf "RIFFfake" > "$last"\nexit 0\n')
    # ffprobe: duración fija, en el JSON que espera audio.probe_duration.
    _script(bindir / "ffprobe",
            '#!/bin/sh\necho \'{"format":{"duration":"3821.5"}}\'\nexit 0\n')
    # whisper-cli: escribe <-of>.json con el formato de -oj.
    payload = json.dumps(WHISPER_JSON).replace("'", "'\\''")
    _script(bindir / "whisper-cli",
            '#!/bin/sh\nwhile [ $# -gt 0 ]; do\n'
            '  if [ "$1" = "-of" ]; then shift; out="$1"; fi\n  shift\ndone\n'
            f"printf '%s' '{payload}' > \"$out.json\"\nexit 0\n")

    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return bindir


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "kb.sqlite")
    db.init_schema(c)
    pid = db.upsert_podcast(c, {
        "slug": "test-de-turing", "title": "El Test de Turing",
        "authors": "Álvaro Peña, Arnau Vendrell", "rss_url": "https://ejemplo.com/f.xml",
        "language": "es",
    })
    db.insert_episode_if_new(c, {
        "podcast_id": pid, "guid": "g1", "enclosure_sha256": "abc",
        "title": "Agentes IA: Destripando los enigmas",
        "published_at": "2026-07-15T06:00:00+00:00", "audio_url": "https://ejemplo.com/ep.mp3",
        "language": "es", "episode_number": 121, "duration_sec": 3782,
    })
    return c


@pytest.fixture
def sin_red(monkeypatch):
    """download_audio no debe salir a internet.

    No es autouse a propósito: TestDescarga prueba la descarga de verdad
    contra un transporte simulado, y parchearla se la comería.
    """
    def fake_download(url, dest, *, expected_bytes=None, client=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"mp3 de mentira")
        return dest

    monkeypatch.setattr(pipeline.audio, "download_audio", fake_download)


@pytest.mark.usefixtures("sin_red")
class TestProcessEpisode:
    def test_camino_completo(self, conn, tmp_path, fake_bin):
        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "transcripts",
            cache_dir=tmp_path / "cache",
        )

        assert outcome.md_path.exists()
        assert outcome.md_path.name == "2026-07-15-agentes-ia-destripando-los-enigmas.md"
        assert outcome.segments_path.exists()
        assert outcome.segments_path.name.endswith(".segments.json.gz")

    def test_front_matter_del_artefacto(self, conn, tmp_path, fake_bin):
        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        fm = yaml.safe_load(split_front_matter(outcome.md_path.read_text(encoding="utf-8"))[0])

        assert fm["podcast_slug"] == "test-de-turing"
        assert fm["episode_number"] == 121
        assert fm["authors"] == ["Álvaro Peña", "Arnau Vendrell"]
        assert fm["duration_sec"] == 3782             # la que declara el feed
        assert fm["transcribed_duration_sec"] == 3821  # la del audio real (§9.1)
        assert fm["checksum_audio"].startswith("sha256:")
        assert fm["transcript"]["vad"] is True

    def test_aplica_fixups_y_borra_la_alucinacion(self, conn, tmp_path, fake_bin):
        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        cuerpo = outcome.md_path.read_text(encoding="utf-8")
        assert "fine-tuning" in cuerpo and "fain tuning" not in cuerpo
        assert "Amara.org" not in cuerpo
        assert outcome.fixups_applied >= 2

    def test_los_segmentos_crudos_se_conservan(self, conn, tmp_path, fake_bin):
        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        segs = load_segments(outcome.segments_path)
        assert len(segs) == 3  # 4 menos la alucinación borrada
        assert segs[0].start == 0.0 and segs[2].start == 62.0

    def test_agrupa_en_bloques_legibles(self, conn, tmp_path, fake_bin):
        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        cuerpo = outcome.md_path.read_text(encoding="utf-8")
        assert "[00:00:00]" in cuerpo and "[00:01:02]" in cuerpo

    def test_actualiza_el_estado_en_sqlite(self, conn, tmp_path, fake_bin):
        pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        row = conn.execute("SELECT * FROM episodes WHERE id = 1").fetchone()
        assert row["stage"] == "exported"
        assert row["transcript_engine"] == "whisper.cpp"
        assert row["transcribed_duration_sec"] == 3821
        assert row["md_path"].endswith(".md")
        assert row["downloaded_at"] and row["transcribed_at"] and row["exported_at"]

    def test_mide_la_velocidad(self, conn, tmp_path, fake_bin):
        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        assert outcome.realtime_factor and outcome.realtime_factor > 0

    def test_episodio_inexistente(self, conn, tmp_path, fake_bin):
        with pytest.raises(ValueError, match="No existe el episodio"):
            pipeline.process_episode(conn, 999, transcripts_root=tmp_path / "t")


class TestComandos:
    def test_whisper_cpp_no_lleva_ml_1(self):
        """§5.6: -ml es longitud MÁXIMA EN CARACTERES. -ml 1 suelto rompe todo."""
        cmd = get_engine("whisper.cpp").build_command(Path("a.wav"), Path("out/a"))
        assert "-ml" not in cmd
        assert "-oj" in cmd and "--vad" in cmd

    def test_word_timestamps_es_deliberado(self):
        cmd = get_engine("whisper.cpp").build_command(
            Path("a.wav"), Path("out/a"), word_timestamps=True
        )
        i = cmd.index("-ml")
        assert cmd[i + 1] == "1" and "-sow" in cmd  # nunca -ml 1 a solas

    def test_vad_desactivable(self):
        cmd = get_engine("whisper.cpp").build_command(Path("a.wav"), Path("out/a"), vad=False)
        assert "--vad" not in cmd

    def test_mlx_whisper_usa_su_propia_sintaxis(self):
        cmd = get_engine("mlx-whisper").build_command(
            Path("a.wav"), Path("out/a"), prompt="glosario"
        )
        assert cmd[0] == "mlx_whisper"
        assert "--initial-prompt" in cmd and "--output-format" in cmd


class TestFfmpeg:
    def test_normaliza_al_formato_nativo_de_whisper(self):
        from podcast_kb.audio import ffmpeg_normalize_cmd

        cmd = ffmpeg_normalize_cmd("in.mp3", "out.wav")
        assert "-ar" in cmd and cmd[cmd.index("-ar") + 1] == "16000"
        assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "1"
        assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "pcm_s16le"


class TestDescarga:
    def test_no_redescarga_si_ya_esta_completo(self, tmp_path):
        from podcast_kb.audio import download_audio

        dest = tmp_path / "ep.mp3"
        dest.write_bytes(b"x" * 100)

        def handler(request):  # pragma: no cover - no debe llamarse
            raise AssertionError("no debería descargar de nuevo")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert download_audio("https://x/ep.mp3", dest, expected_bytes=100, client=client) == dest

    def test_redescarga_si_el_tamano_no_cuadra(self, tmp_path):
        from podcast_kb.audio import download_audio

        dest = tmp_path / "ep.mp3"
        dest.write_bytes(b"x" * 5)
        client = httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"y" * 100))
        )
        download_audio("https://x/ep.mp3", dest, expected_bytes=100, client=client)
        assert dest.read_bytes() == b"y" * 100


@pytest.mark.usefixtures("sin_red")
class TestRutasDeConfiguracion:
    """Regresión: ejecutar el CLI desde otro directorio no debe desactivar
    el glosario ni los fixups en silencio."""

    def test_los_fixups_se_aplican_desde_cualquier_cwd(
        self, conn, tmp_path, fake_bin, monkeypatch
    ):
        ajeno = tmp_path / "otro-sitio"
        ajeno.mkdir()
        monkeypatch.chdir(ajeno)

        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        cuerpo = outcome.md_path.read_text(encoding="utf-8")
        assert "fine-tuning" in cuerpo, "los fixups no se aplicaron fuera del repo"
        assert "Amara.org" not in cuerpo
        assert outcome.missing_config == []

    def test_avisa_si_falta_la_configuracion(
        self, conn, tmp_path, fake_bin, monkeypatch
    ):
        vacio = tmp_path / "raiz-vacia"
        (vacio / "config").mkdir(parents=True)
        (vacio / "pyproject.toml").write_text("", encoding="utf-8")
        monkeypatch.setenv("PODCAST_KB_ROOT", str(vacio))

        outcome = pipeline.process_episode(
            conn, 1, transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c"
        )
        assert len(outcome.missing_config) == 2  # glosario y fixups
        assert any("glossary" in m for m in outcome.missing_config)
        assert any("fixups" in m for m in outcome.missing_config)


VTT_DEL_FEED = """WEBVTT

00:00:00.000 --> 00:00:04.200
<v Antonio Ortiz>Muy buenas, bienvenidos una semana más.

00:00:04.200 --> 00:00:11.000
<v Matías S. Zavia>Hoy hablamos de agentes y de fain tuning.

00:01:02.000 --> 00:01:09.000
<v Antonio Ortiz>El protocolo MCP conecta herramientas externas.
"""


@pytest.fixture
def conn_con_subtitulos(tmp_path):
    """Un episodio cuyo feed ya publica la transcripción en VTT."""
    c = db.connect(tmp_path / "kb2.sqlite")
    db.init_schema(c)
    pid = db.upsert_podcast(c, {
        "slug": "monos-estocasticos", "title": "monos estocásticos",
        "authors": "Antonio Ortiz, Matías S. Zavia",
        "rss_url": "https://cuonda.com/monos-estocasticos/feed", "language": "es",
    })
    db.insert_episode_if_new(c, {
        "podcast_id": pid, "guid": "g-vtt", "enclosure_sha256": "abc-vtt",
        "title": "Es el momento de desacelerar la IA",
        "published_at": "2026-07-30T06:00:00+00:00",
        "audio_url": "https://cuonda.com/download/1670584.mp3",
        "language": "es", "duration_sec": 4520,
        "feed_transcript_url": "https://cuonda.com/x/transcription",
        "feed_transcript_type": "text/vtt",
    })
    return c


@pytest.fixture
def sin_descarga_de_subtitulos(monkeypatch):
    monkeypatch.setattr(pipeline, "fetch_subtitles", lambda url, client=None: VTT_DEL_FEED)


@pytest.mark.usefixtures("sin_red", "sin_descarga_de_subtitulos")
class TestTranscripcionDelFeed:
    """§4.4: si el feed la publica con marcas de tiempo, no se transcribe."""

    def test_no_toca_whisper_ni_ffmpeg(self, conn_con_subtitulos, tmp_path, monkeypatch):
        def explota(*a, **kw):  # pragma: no cover
            raise AssertionError("no debería llamarse: el feed ya trae la transcripción")

        monkeypatch.setattr(pipeline.transcribe, "transcribe", explota)
        monkeypatch.setattr(pipeline.audio, "normalize_audio", explota)
        monkeypatch.setattr(pipeline.audio, "download_audio", explota)

        outcome = pipeline.process_episode(
            conn_con_subtitulos, 1,
            transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c",
        )
        assert outcome.source == "feed"
        assert outcome.md_path.exists()

    def test_el_front_matter_lo_declara(self, conn_con_subtitulos, tmp_path):
        outcome = pipeline.process_episode(
            conn_con_subtitulos, 1,
            transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c",
        )
        fm = yaml.safe_load(split_front_matter(outcome.md_path.read_text(encoding="utf-8"))[0])
        assert fm["transcript"]["engine"] == "feed"
        assert fm["transcript"]["model"] == "text/vtt"
        assert fm["transcript"]["diarized"] is True
        assert fm["transcript"]["vad"] is False
        assert fm["source"] == "feed-transcript"

    def test_no_inventa_datos_del_audio(self, conn_con_subtitulos, tmp_path):
        """Los timestamps son los del máster del autor, no los de una copia
        nuestra: medir su duración o su checksum sería mentir (§9.1)."""
        outcome = pipeline.process_episode(
            conn_con_subtitulos, 1,
            transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c",
        )
        fm = yaml.safe_load(split_front_matter(outcome.md_path.read_text(encoding="utf-8"))[0])
        assert "checksum_audio" not in fm
        assert "transcribed_duration_sec" not in fm
        assert fm["duration_sec"] == 4520  # la que declara el feed sí se conserva

    def test_conserva_los_hablantes(self, conn_con_subtitulos, tmp_path):
        outcome = pipeline.process_episode(
            conn_con_subtitulos, 1,
            transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c",
        )
        assert outcome.diarized
        cuerpo = outcome.md_path.read_text(encoding="utf-8")
        assert "(Antonio Ortiz)" in cuerpo and "(Matías S. Zavia)" in cuerpo

    def test_los_fixups_tambien_se_aplican(self, conn_con_subtitulos, tmp_path):
        outcome = pipeline.process_episode(
            conn_con_subtitulos, 1,
            transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c",
        )
        cuerpo = outcome.md_path.read_text(encoding="utf-8")
        assert "fine-tuning" in cuerpo and "fain tuning" not in cuerpo

    def test_force_whisper_lo_ignora(self, conn_con_subtitulos, tmp_path, fake_bin):
        outcome = pipeline.process_episode(
            conn_con_subtitulos, 1, prefer_feed_transcript=False,
            transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c",
        )
        assert outcome.source == "whisper"
        assert outcome.realtime_factor is not None

    def test_texto_plano_no_vale_como_transcripcion(self, conn_con_subtitulos, tmp_path, fake_bin):
        """Sin marcas de tiempo hay que transcribir igual."""
        conn_con_subtitulos.execute(
            "UPDATE episodes SET feed_transcript_type = 'plain/txt' WHERE id = 1"
        )
        conn_con_subtitulos.commit()
        outcome = pipeline.process_episode(
            conn_con_subtitulos, 1,
            transcripts_root=tmp_path / "t", cache_dir=tmp_path / "c",
        )
        assert outcome.source == "whisper"


@pytest.mark.usefixtures("sin_red")
class TestErroresDeRed:
    def test_un_subtitulo_inalcanzable_no_revienta_el_cli(
        self, conn_con_subtitulos, tmp_path, monkeypatch
    ):
        """§4.3: los fallos se registran para reintento, no se propagan crudos."""
        from typer.testing import CliRunner

        from podcast_kb.cli import app

        def falla(url, client=None):
            raise httpx.ConnectError("no hay ruta al host")

        monkeypatch.setattr(pipeline, "fetch_subtitles", falla)
        monkeypatch.chdir(tmp_path)

        db_path = tmp_path / "kb2.sqlite"
        result = CliRunner().invoke(app, ["process", "1", "--db", str(db_path)])

        assert result.exit_code == 1
        assert "ConnectError" in result.output
        assert "Traceback" not in result.output

        conn_con_subtitulos.close()
        import sqlite3

        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT attempts, last_error FROM episodes WHERE id = 1").fetchone()
        assert row["attempts"] == 1
        assert "ConnectError" in row["last_error"]
