"""Camino completo sobre un episodio: descarga → WAV → Whisper → .md (Fase 1)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import audio, db, export, fixups as fixups_mod, segments as seg_mod, transcribe
from .paths import config_path

CACHE_AUDIO = Path("cache/audio")


@dataclass
class EpisodeOutcome:
    slug: str
    title: str
    md_path: Path | None = None
    segments_path: Path | None = None
    realtime_factor: float | None = None
    elapsed_sec: float | None = None
    fixups_applied: int = 0
    prompt_echo_stripped: bool = False
    missing_config: list[str] = field(default_factory=list)
    needs_review: bool = False
    review_reason: str | None = None


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def load_prompt(path: Path | str | None) -> str | None:
    if not path:
        return None
    path = Path(path)
    if not path.exists():
        return None
    return " ".join(path.read_text(encoding="utf-8").split())


def process_episode(
    conn: sqlite3.Connection,
    episode_id: int,
    *,
    engine: str = "whisper.cpp",
    model: str = transcribe.DEFAULT_MODEL,
    vad: bool = True,
    word_timestamps: bool = False,
    keep_wav: bool = False,
    transcripts_root: Path = export.TRANSCRIPTS_DIR,
    cache_dir: Path = CACHE_AUDIO,
) -> EpisodeOutcome:
    row = conn.execute(
        "SELECT e.*, p.slug AS podcast_slug, p.title AS podcast_title, "
        "       p.authors AS podcast_authors, p.glossary_path AS glossary_path "
        "FROM episodes e JOIN podcasts p ON p.id = e.podcast_id WHERE e.id = ?",
        (episode_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No existe el episodio {episode_id}")

    outcome = EpisodeOutcome(slug=row["podcast_slug"], title=row["title"])
    language = row["language"] or "es"
    stem = f"{row['published_at'][:10]}-{seg_mod.slugify(row['title'])}"

    # 1. Descarga (idempotente).
    mp3 = Path(cache_dir) / row["podcast_slug"] / f"{stem}.mp3"
    audio.download_audio(row["audio_url"], mp3, expected_bytes=row["audio_bytes"])
    conn.execute(
        "UPDATE episodes SET local_audio = ?, downloaded_at = ?, stage = 'downloaded' "
        "WHERE id = ?",
        (str(mp3), db.utcnow(), episode_id),
    )
    conn.commit()

    # 2. Normalización a 16 kHz mono PCM.
    wav = mp3.with_suffix(".wav")
    audio.normalize_audio(mp3, wav)
    # La duración del audio realmente transcrito, que no es la del feed (§9.1).
    transcribed_duration = audio.probe_duration(wav)

    # 3. Transcripción.
    glossary_path = Path(row["glossary_path"]) if row["glossary_path"] else config_path(
        f"glossary.{language}.txt"
    )
    prompt = load_prompt(glossary_path)
    if prompt is None:
        # Un glosario que no se carga degrada la transcripción sin dar error.
        outcome.missing_config.append(str(glossary_path))
    result = transcribe.transcribe(
        wav,
        Path(cache_dir) / row["podcast_slug"] / stem,
        engine=engine,
        model=model,
        language=language,
        prompt=prompt,
        vad=vad,
        word_timestamps=word_timestamps,
        audio_duration_sec=transcribed_duration,
    )
    outcome.elapsed_sec = result.elapsed_sec
    outcome.realtime_factor = result.realtime_factor

    # 4. Post-proceso: eco del prompt, fixups y detector de bucles (§5.7, §5.8).
    segments = result.segments
    if prompt:
        outcome.prompt_echo_stripped = fixups_mod.strip_prompt_echo(segments, prompt)
    fixups_path = fixups_mod.default_fixups_path()
    if not fixups_path.exists():
        outcome.missing_config.append(str(fixups_path))
    segments, outcome.fixups_applied = fixups_mod.apply_fixups(
        segments, fixups_mod.load_fixups(fixups_path)
    )
    outcome.needs_review, outcome.review_reason = seg_mod.detect_repetition(segments)

    # 5. Artefactos: .md para humanos e indexado, .segments.json.gz como
    #    materia prima para rehacer el chunking sin retranscribir (§7.2).
    md_path = export.md_path_for(
        row["podcast_slug"], row["published_at"], row["title"], root=transcripts_root
    )
    segments_path = md_path.parent / (md_path.stem + seg_mod.SEGMENTS_SUFFIX)
    outcome.segments_path = seg_mod.save_segments(segments, segments_path)

    authors = [a.strip() for a in (row["podcast_authors"] or "").split(",") if a.strip()]
    data = export.ExportInput(
        podcast=row["podcast_title"],
        podcast_slug=row["podcast_slug"],
        episode_title=row["title"],
        published_at=row["published_at"],
        language=language,
        audio_url=row["audio_url"],
        guid=row["guid"],
        segments=segments,
        authors=authors,
        episode_number=row["episode_number"],
        duration_sec=row["duration_sec"],
        transcribed_duration_sec=int(transcribed_duration),
        episode_url=row["episode_url"],
        checksum_audio=f"sha256:{sha256_file(mp3)}",
        engine=result.engine,
        model=Path(result.model).stem or result.model,
        glossary=glossary_path.stem if prompt else None,
        vad=vad,
        needs_review=outcome.needs_review,
        review_reason=outcome.review_reason,
        chapters=json.loads(row["chapters"]) if row["chapters"] else None,
        chapters_source=row["chapters_source"] or "none",
    )
    outcome.md_path = export.write_episode(data, root=transcripts_root)

    conn.execute(
        "UPDATE episodes SET md_path = ?, transcript_engine = ?, transcript_model = ?, "
        "  transcribed_duration_sec = ?, checksum_audio = ?, needs_review = ?, "
        "  transcribed_at = ?, exported_at = ?, stage = 'exported' WHERE id = ?",
        (
            str(outcome.md_path), result.engine, data.model, int(transcribed_duration),
            data.checksum_audio, 1 if outcome.needs_review else 0,
            db.utcnow(), db.utcnow(), episode_id,
        ),
    )
    conn.commit()

    if not keep_wav and wav.exists():
        wav.unlink()

    return outcome
