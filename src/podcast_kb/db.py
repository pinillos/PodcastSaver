"""Estado local del pipeline (SQLite, §7.1 del diseño).

SQLite es caché derivada + estado de ejecución. La fuente de verdad del alta
de podcasts es config/podcasts.yaml (§4.1): si divergen, gana el YAML.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path("db/local.sqlite")

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS podcasts (
  id              INTEGER PRIMARY KEY,
  slug            TEXT NOT NULL UNIQUE,
  title           TEXT NOT NULL,
  authors         TEXT,
  rss_url         TEXT NOT NULL UNIQUE,
  apple_id        TEXT,
  spotify_show_id TEXT,
  website         TEXT,
  language        TEXT NOT NULL,
  glossary_path   TEXT,
  speaker_map     TEXT,
  active          INTEGER NOT NULL DEFAULT 1,
  etag            TEXT,
  last_modified   TEXT,
  last_synced_at  TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
  id             INTEGER PRIMARY KEY,
  podcast_id     INTEGER NOT NULL REFERENCES podcasts(id) ON DELETE CASCADE,
  guid           TEXT NOT NULL,
  enclosure_sha256 TEXT NOT NULL,
  title          TEXT NOT NULL,
  episode_number INTEGER,
  published_at   TEXT NOT NULL,
  duration_sec   INTEGER,
  transcribed_duration_sec INTEGER,
  audio_url      TEXT NOT NULL,
  audio_bytes    INTEGER,
  description    TEXT,
  episode_url    TEXT,
  language       TEXT,
  local_audio    TEXT,
  md_path        TEXT,
  transcript_engine TEXT,
  transcript_model  TEXT,
  checksum_audio TEXT,

  -- Transcripción publicada por el propio feed (§4.4): si existe, nos
  -- ahorramos transcribir.
  feed_transcript_url  TEXT,
  feed_transcript_type TEXT,
  feed_chapters_url    TEXT,
  -- Capítulos del autor extraídos de las show notes (§6). El .md los emite
  -- tal cual; sin ellos habría que pagarle a un LLM por reinventarlos.
  chapters             TEXT,
  chapters_source      TEXT NOT NULL DEFAULT 'none',
  -- Hablantes declarados por el feed (<podcast:person>). Resuelve de gratis
  -- el mapeo de §5.11, que si no hay que hacer a mano por podcast.
  persons              TEXT,

  -- Progreso: stage + timestamps independientes (§4.3). No es una máquina de
  -- estados lineal porque el enriquecimiento es re-ejecutable sobre episodios
  -- ya indexados.
  stage          TEXT NOT NULL DEFAULT 'discovered',
  discovered_at  TEXT NOT NULL,
  downloaded_at  TEXT,
  transcribed_at TEXT,
  diarized_at    TEXT,
  enriched_at    TEXT,
  exported_at    TEXT,
  indexed_at     TEXT,

  -- Errores y calidad
  attempts       INTEGER NOT NULL DEFAULT 0,
  next_retry_at  TEXT,
  last_error     TEXT,
  needs_review   INTEGER NOT NULL DEFAULT 0,

  UNIQUE (podcast_id, guid)
);

-- Dedupe GLOBAL: detecta el mismo audio publicado en dos feeds distintos
-- (§2.2). La clave (podcast_id, guid) por sí sola no puede.
CREATE UNIQUE INDEX IF NOT EXISTS episodes_enclosure_idx
  ON episodes (enclosure_sha256);

CREATE INDEX IF NOT EXISTS episodes_stage_idx ON episodes (stage, next_retry_at);

CREATE TABLE IF NOT EXISTS embeddings_cache (
  text_sha256 TEXT PRIMARY KEY,
  model       TEXT NOT NULL,
  embedding   BLOB NOT NULL
);
"""

# El estado local termina en `exported`. Lo que pasa después vive en
# Postgres (episodes.indexed_at): la etapa de indexado lee solo el
# repositorio y puede correr en un GitHub Action sin acceso a este SQLite
# (§3.2), así que no puede ni debe actualizarlo.
STAGES = (
    "discovered",
    "downloaded",
    "transcribed",
    "diarized",
    "enriched",
    "exported",
)


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    if path.parent != Path(""):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


# Columnas añadidas después de la v1 del esquema. SQLite no tiene
# ALTER TABLE IF NOT EXISTS, así que se comprueba antes.
_MIGRATIONS: dict[str, str] = {
    "chapters": "ALTER TABLE episodes ADD COLUMN chapters TEXT",
    "chapters_source": (
        "ALTER TABLE episodes ADD COLUMN chapters_source TEXT NOT NULL DEFAULT 'none'"
    ),
    "persons": "ALTER TABLE episodes ADD COLUMN persons TEXT",
}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Aplica las columnas que falten en una base de datos ya existente."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)")}
    applied = []
    for column, statement in _MIGRATIONS.items():
        if column not in existing:
            conn.execute(statement)
            applied.append(column)
    if applied:
        conn.commit()
    return applied


# Reintentos (§4.3). Sin esto, un episodio cuyo audio da 404 se reintenta en
# cada ejecución para siempre — y con `--pending N` llega a copar el lote
# entero, impidiendo que avancen los que sí funcionan.
BACKOFF_BASE_MIN = 15
BACKOFF_MAX_HORAS = 24
MAX_INTENTOS = 5


def proximo_reintento(intentos: int) -> str | None:
    """Cuándo volver a intentarlo. `None` = no volver a intentarlo solo."""
    if intentos >= MAX_INTENTOS:
        return None
    minutos = min(BACKOFF_BASE_MIN * (2 ** (intentos - 1)), BACKOFF_MAX_HORAS * 60)
    return (datetime.now(UTC) + timedelta(minutes=minutos)).isoformat(
        timespec="seconds"
    )


def registrar_fallo(conn: sqlite3.Connection, episode_id: int, error: str) -> tuple[int, str | None]:
    """Anota el fallo y programa el siguiente intento. Devuelve (intentos, cuándo)."""
    fila = conn.execute(
        "SELECT attempts FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    intentos = (fila["attempts"] if fila else 0) + 1
    cuando = proximo_reintento(intentos)
    conn.execute(
        "UPDATE episodes SET attempts = ?, last_error = ?, next_retry_at = ?, "
        "  needs_review = CASE WHEN ? THEN 1 ELSE needs_review END "
        "WHERE id = ?",
        (intentos, error[:500], cuando, cuando is None, episode_id),
    )
    conn.commit()
    return intentos, cuando


def limpiar_fallo(conn: sqlite3.Connection, episode_id: int) -> None:
    """Un episodio que termina bien deja de arrastrar su historial de fallos."""
    conn.execute(
        "UPDATE episodes SET attempts = 0, next_retry_at = NULL, last_error = NULL "
        "WHERE id = ?",
        (episode_id,),
    )
    conn.commit()


def upsert_podcast(conn: sqlite3.Connection, podcast: dict) -> int:
    """Reconcilia un podcast del YAML contra SQLite. El YAML manda (§4.1)."""
    cols = (
        "slug", "title", "authors", "rss_url", "apple_id", "spotify_show_id",
        "website", "language", "glossary_path", "speaker_map", "active",
    )
    values = {c: podcast.get(c) for c in cols}
    values["active"] = 1 if podcast.get("active", True) else 0
    values["title"] = podcast.get("title") or podcast["slug"]

    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "slug")
    conn.execute(
        f"INSERT INTO podcasts ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(slug) DO UPDATE SET {updates}",
        values,
    )
    conn.commit()
    row = conn.execute("SELECT id FROM podcasts WHERE slug = ?", (podcast["slug"],)).fetchone()
    return int(row["id"])


def insert_episode_if_new(conn: sqlite3.Connection, episode: dict) -> bool:
    """Inserta un episodio si no se conoce ya. Devuelve True si era nuevo.

    Dos barreras de deduplicación, en este orden:
      1. enclosure_sha256 global — el mismo audio en otro feed (§2.2).
      2. (podcast_id, guid)      — el caso normal entre ejecuciones.
    """
    cols = (
        "podcast_id", "guid", "enclosure_sha256", "title", "episode_number",
        "published_at", "duration_sec", "audio_url", "audio_bytes",
        "description", "episode_url", "language",
        "feed_transcript_url", "feed_transcript_type", "feed_chapters_url",
        "chapters", "chapters_source", "persons", "stage", "discovered_at",
    )
    values = {c: episode.get(c) for c in cols}
    values["stage"] = "discovered"
    values["discovered_at"] = utcnow()
    values["chapters_source"] = episode.get("chapters_source") or "none"

    # ON CONFLICT DO NOTHING en vez de INSERT OR IGNORE: OR IGNORE se traga
    # TODOS los errores de constraint, incluidos los NOT NULL, así que una
    # columna nueva mal rellenada descartaría episodios en silencio. Esto
    # solo silencia los conflictos de unicidad, que es lo que queremos.
    placeholders = ", ".join(f":{c}" for c in cols)
    cur = conn.execute(
        f"INSERT INTO episodes ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING",
        values,
    )
    conn.commit()
    return cur.rowcount > 0


def update_feed_cache(
    conn: sqlite3.Connection, podcast_id: int, etag: str | None, last_modified: str | None
) -> None:
    conn.execute(
        "UPDATE podcasts SET etag = ?, last_modified = ?, last_synced_at = ? WHERE id = ?",
        (etag, last_modified, utcnow(), podcast_id),
    )
    conn.commit()
