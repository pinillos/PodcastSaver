"""Etapa 1: reconciliar el YAML, pedir los feeds y dar de alta lo nuevo (§4)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import httpx

from . import db, feeds


@dataclass
class PodcastSyncReport:
    slug: str
    ok: bool = True
    not_modified: bool = False
    error: str | None = None
    seen: int = 0
    new: list[feeds.EpisodeItem] = field(default_factory=list)
    with_feed_transcript: int = 0

    @property
    def new_count(self) -> int:
        return len(self.new)


@dataclass
class SyncReport:
    podcasts: list[PodcastSyncReport] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return sum(p.new_count for p in self.podcasts)

    @property
    def failed(self) -> list[PodcastSyncReport]:
        return [p for p in self.podcasts if not p.ok]


def sync_podcast(
    conn: sqlite3.Connection,
    entry: dict,
    *,
    dry_run: bool = False,
    client: httpx.Client | None = None,
    limit: int | None = None,
) -> PodcastSyncReport:
    report = PodcastSyncReport(slug=entry["slug"])

    resolved = dict(entry)
    try:
        if not resolved.get("rss_url"):
            resolved.update(feeds.resolve_feed_from_apple_id(resolved["apple_id"], client=client))

        podcast_id = db.upsert_podcast(conn, resolved)
        row = conn.execute(
            "SELECT etag, last_modified FROM podcasts WHERE id = ?", (podcast_id,)
        ).fetchone()

        raw, etag, last_modified, final_url = feeds.fetch_feed(
            resolved["rss_url"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            client=client,
        )
        if raw is None:
            report.not_modified = True
            return report

        # La URL final resuelve los enclosures relativos de los feeds
        # autoalojados; sin ella salían rutas inservibles.
        parsed = feeds.parse_feed(raw, base_url=final_url)
    except (feeds.FeedError, httpx.HTTPError) as exc:
        report.ok = False
        report.error = f"{type(exc).__name__}: {exc}"
        return report

    items = parsed.items[:limit] if limit else parsed.items
    report.seen = len(items)
    default_language = resolved.get("language") or parsed.language

    for item in items:
        if item.feed_transcript_url:
            report.with_feed_transcript += 1
        if dry_run:
            if not _is_known(conn, podcast_id, item):
                report.new.append(item)
            continue
        payload = {
            **item.__dict__,
            "podcast_id": podcast_id,
            "language": default_language,
        }
        if db.insert_episode_if_new(conn, payload):
            report.new.append(item)

    if not dry_run:
        db.update_feed_cache(conn, podcast_id, etag, last_modified)

    return report


def _is_known(conn: sqlite3.Connection, podcast_id: int, item: feeds.EpisodeItem) -> bool:
    """Las dos barreras de dedupe de §4.2, sin escribir nada."""
    hit = conn.execute(
        "SELECT 1 FROM episodes WHERE enclosure_sha256 = ? "
        "   OR (podcast_id = ? AND guid = ?) LIMIT 1",
        (item.enclosure_sha256, podcast_id, item.guid),
    ).fetchone()
    return hit is not None


def sync_all(
    conn: sqlite3.Connection,
    entries: list[dict],
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> SyncReport:
    report = SyncReport()
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for entry in entries:
            if not entry.get("active", True):
                continue
            report.podcasts.append(
                sync_podcast(conn, entry, dry_run=dry_run, client=client, limit=limit)
            )
    return report
