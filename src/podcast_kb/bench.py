"""Banco de pruebas de la Fase 1 (§5.3, §5.10, §15 TODO 1-3).

Tres decisiones se toman midiendo, no opinando:
  2. whisper.cpp vs mlx-whisper  → cuál es más rápido en esta máquina.
  3. large-v3-turbo vs large-v3  → si turbo aguanta el habla solapada.
  1. timestamps por segmento o por palabra → cuánto crece el JSON.

La comparativa de calidad tiene que hacerse sobre un tramo de tertulia con
gente hablando encima, no sobre un monólogo limpio: es ahí donde turbo se
degrada, y son dos de los tres podcasts.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import audio, segments as seg_mod, transcribe


@dataclass
class BenchRun:
    engine: str
    model: str
    elapsed_sec: float
    realtime_factor: float | None
    n_segments: int
    n_words: int
    segments_bytes_gz: int
    error: str | None = None
    text_head: str = ""


@dataclass
class BenchReport:
    wav: str
    audio_duration_sec: float | None
    runs: list[BenchRun] = field(default_factory=list)


def _measure(segments: list[seg_mod.Segment]) -> tuple[int, int, int]:
    n_words = sum(len(s.text.split()) for s in segments)
    payload = json.dumps(
        {"schema": 1, "segments": [s.as_dict() for s in segments]}, ensure_ascii=False
    ).encode("utf-8")
    return len(segments), n_words, len(gzip.compress(payload))


def run_bench(
    wav: Path | str,
    combos: list[tuple[str, str]],
    *,
    language: str = "es",
    prompt: str | None = None,
    vad: bool = True,
    word_timestamps: bool = False,
    workdir: Path = Path("cache/bench"),
) -> BenchReport:
    wav = Path(wav)
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        duration = audio.probe_duration(wav)
    except audio.AudioError:
        duration = None

    report = BenchReport(wav=str(wav), audio_duration_sec=duration)

    for engine_name, model in combos:
        prefix = workdir / f"{wav.stem}--{engine_name.replace('.', '_')}--{Path(model).stem}"
        try:
            result = transcribe.transcribe(
                wav, prefix,
                engine=engine_name, model=model, language=language,
                prompt=prompt, vad=vad, word_timestamps=word_timestamps,
                audio_duration_sec=duration,
            )
        except transcribe.TranscribeError as exc:
            report.runs.append(
                BenchRun(engine_name, model, 0.0, None, 0, 0, 0, error=str(exc))
            )
            continue

        n_seg, n_words, size_gz = _measure(result.segments)
        report.runs.append(
            BenchRun(
                engine=result.engine,
                model=Path(model).stem or model,
                elapsed_sec=result.elapsed_sec,
                realtime_factor=result.realtime_factor,
                n_segments=n_seg,
                n_words=n_words,
                segments_bytes_gz=size_gz,
                text_head=" ".join(s.text for s in result.segments[:6])[:400],
            )
        )
    return report
