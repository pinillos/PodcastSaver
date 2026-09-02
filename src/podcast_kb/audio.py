"""Descarga y normalización de audio (§5.4).

La descarga es idempotente: si el fichero ya está con el tamaño esperado, no
se vuelve a pedir (§4.2). Ojo con la inserción dinámica de publicidad (§9.1):
el mismo enclosure devuelve audios distintos en cada descarga, así que el
tamaño del feed es solo orientativo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx

from . import net
from .feeds import USER_AGENT

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

SAMPLE_RATE = 16000  # formato nativo de whisper.cpp


class AudioError(RuntimeError):
    pass


def require_binaries(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise AudioError(
            f"No se encuentra {', '.join(missing)} en el PATH. "
            "En macOS: `brew install ffmpeg`."
        )


def download_audio(
    url: str,
    dest: Path | str,
    *,
    expected_bytes: int | None = None,
    client: httpx.Client | None = None,
    allow_private: bool | None = None,
    max_bytes: int = net.MAX_AUDIO_BYTES,
) -> Path:
    """Descarga el enclosure si hace falta. Devuelve la ruta local."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and dest.stat().st_size > 0:
        if expected_bytes is None or dest.stat().st_size == expected_bytes:
            return dest

    owns_client = client is None
    client = client or httpx.Client(timeout=300, follow_redirects=False)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        # La URL sale del feed: se valida el destino y se acota el tamaño.
        resp = net.get_validado(
            client, url, headers={"User-Agent": USER_AGENT}, permitir_privadas=allow_private
        )
        try:
            resp.raise_for_status()
            escrito = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    escrito += len(chunk)
                    if escrito > max_bytes:
                        raise net.DemasiadoGrande(
                            f"el audio supera el tope de {max_bytes:,} bytes"
                        )
                    fh.write(chunk)
        finally:
            resp.close()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if owns_client:
            client.close()

    tmp.replace(dest)
    return dest


def ffmpeg_normalize_cmd(src: Path | str, dest: Path | str) -> list[str]:
    """16 kHz, mono, PCM 16-bit: el formato nativo de whisper.cpp (§5.4)."""
    return [
        FFMPEG, "-nostdin", "-y",
        "-i", str(src),
        "-ar", str(SAMPLE_RATE),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(dest),
    ]


def normalize_audio(src: Path | str, dest: Path | str) -> Path:
    require_binaries(FFMPEG)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ffmpeg_normalize_cmd(src, dest), capture_output=True, text=True
    )
    if result.returncode != 0:
        raise AudioError(f"ffmpeg falló ({result.returncode}): {result.stderr[-800:]}")
    return dest


def probe_duration(path: Path | str) -> float:
    """Duración real del fichero, que no es la que declara el feed (§9.1)."""
    require_binaries(FFPROBE)
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AudioError(f"ffprobe falló ({result.returncode}): {result.stderr[-400:]}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise AudioError(f"ffprobe devolvió algo inesperado: {result.stdout[:200]}") from exc
