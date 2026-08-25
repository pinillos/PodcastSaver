"""Etapa 2: speech-to-text (§5).

Dos motores tras la misma interfaz, porque cuál gana es una pregunta empírica
que se responde en la Fase 1 midiendo, no opinando (§15, TODO 2):

  - whisper.cpp  → Metal + VAD integrado, el default.
  - mlx-whisper  → MLX, el framework de Apple; a menudo más rápido con
                   modelos grandes, pero sin VAD integrado.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .segments import Segment, parse_transcript_json

DEFAULT_MODEL = "models/ggml-large-v3-turbo-q5_0.bin"
DEFAULT_VAD_MODEL = "models/ggml-silero-v5.1.2.bin"


class TranscribeError(RuntimeError):
    pass


@dataclass
class TranscriptionResult:
    segments: list[Segment]
    engine: str
    model: str
    elapsed_sec: float
    audio_duration_sec: float | None = None
    command: list[str] = field(default_factory=list)

    @property
    def realtime_factor(self) -> float | None:
        """Cuántas veces más rápido que el tiempo real (§5.9)."""
        if not self.audio_duration_sec or self.elapsed_sec <= 0:
            return None
        return self.audio_duration_sec / self.elapsed_sec


class Engine:
    name = "abstract"

    def build_command(self, wav: Path, out_prefix: Path, **kw) -> list[str]:
        raise NotImplementedError

    def output_json_path(self, out_prefix: Path, wav: Path) -> Path:
        raise NotImplementedError

    def available(self) -> bool:
        raise NotImplementedError


class WhisperCppEngine(Engine):
    """`whisper-cli` de whisper.cpp."""

    name = "whisper.cpp"

    def __init__(self, binary: str = "whisper-cli") -> None:
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def build_command(
        self,
        wav: Path,
        out_prefix: Path,
        *,
        model: str = DEFAULT_MODEL,
        language: str = "es",
        prompt: str | None = None,
        vad: bool = True,
        vad_model: str = DEFAULT_VAD_MODEL,
        threads: int | None = None,
        word_timestamps: bool = False,
        **_,
    ) -> list[str]:
        cmd = [
            self.binary,
            "-m", str(model),
            "-f", str(wav),
            "-l", language,
            "-oj",
            "-of", str(out_prefix),
        ]
        if prompt:
            cmd += ["--prompt", prompt]
        if vad:
            cmd += ["--vad", "--vad-model", str(vad_model)]
        if threads:
            cmd += ["-t", str(threads)]
        if word_timestamps:
            # Deliberado y documentado (§5.6): -ml es longitud MÁXIMA EN
            # CARACTERES, así que -ml 1 -sow parte por palabra. Nunca poner
            # -ml 1 a solas esperando segmentos normales.
            cmd += ["-ml", "1", "-sow"]
        return cmd

    def output_json_path(self, out_prefix: Path, wav: Path) -> Path:
        return Path(str(out_prefix) + ".json")


class MlxWhisperEngine(Engine):
    """`mlx_whisper` de Apple MLX, vía su CLI."""

    name = "mlx-whisper"

    def __init__(self, binary: str = "mlx_whisper") -> None:
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def build_command(
        self,
        wav: Path,
        out_prefix: Path,
        *,
        model: str = "mlx-community/whisper-large-v3-turbo",
        language: str = "es",
        prompt: str | None = None,
        word_timestamps: bool = False,
        **_,
    ) -> list[str]:
        cmd = [
            self.binary,
            str(wav),
            "--model", str(model),
            "--language", language,
            "--output-format", "json",
            "--output-dir", str(out_prefix.parent),
        ]
        if prompt:
            cmd += ["--initial-prompt", prompt]
        if word_timestamps:
            cmd += ["--word-timestamps", "True"]
        return cmd

    def output_json_path(self, out_prefix: Path, wav: Path) -> Path:
        # mlx_whisper nombra la salida por el fichero de ENTRADA, no por el
        # prefijo. Derivarla del prefijo funcionaba de casualidad en el
        # pipeline (donde coinciden) y fallaba siempre en el benchmark, que
        # añade sufijos de motor y modelo al prefijo.
        return out_prefix.parent / (wav.stem + ".json")


ENGINES: dict[str, type[Engine]] = {
    "whisper.cpp": WhisperCppEngine,
    "mlx-whisper": MlxWhisperEngine,
}


def get_engine(name: str) -> Engine:
    try:
        return ENGINES[name]()
    except KeyError as exc:
        raise TranscribeError(
            f"Motor desconocido: {name!r}. Disponibles: {', '.join(ENGINES)}"
        ) from exc


def transcribe(
    wav: Path | str,
    out_prefix: Path | str,
    *,
    engine: str | Engine = "whisper.cpp",
    model: str = DEFAULT_MODEL,
    language: str = "es",
    prompt: str | None = None,
    audio_duration_sec: float | None = None,
    **kwargs,
) -> TranscriptionResult:
    eng = engine if isinstance(engine, Engine) else get_engine(engine)
    wav, out_prefix = Path(wav), Path(out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    if not eng.available():
        raise TranscribeError(
            f"No se encuentra el binario de {eng.name} en el PATH. "
            "Ver README para instalarlo."
        )

    cmd = eng.build_command(
        wav, out_prefix, model=model, language=language, prompt=prompt, **kwargs
    )
    started = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.monotonic() - started

    if result.returncode != 0:
        raise TranscribeError(f"{eng.name} falló ({result.returncode}): {result.stderr[-800:]}")

    json_path = eng.output_json_path(out_prefix, wav)
    if not json_path.exists():
        raise TranscribeError(f"{eng.name} no generó {json_path}")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    return TranscriptionResult(
        segments=parse_transcript_json(payload),
        engine=eng.name,
        model=str(model),
        elapsed_sec=elapsed,
        audio_duration_sec=audio_duration_sec,
        command=cmd,
    )
