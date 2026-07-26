# -*- coding: utf-8 -*-
"""TikTok-first MVP радара мемов.

Модуль намеренно не скрейпит поисковую выдачу TikTok. Он принимает публичные
ссылки-сигналы, безопасно обогащает TikTok-ссылки через официальный oEmbed,
хранит историю наблюдений в SQLite и отделяет:

* ``radar_first_seen_at`` — когда сигнал впервые увидела наша система;
* ``earliest_published_at`` — самая ранняя подтверждённая публикация.

Без второго значения тренд не может получить NEW/RISING: это защита от ошибки
«старый мем впервые попал в базу сегодня, значит он новый».
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from loguru import logger

_SOURCE_HOSTS = {
    "tiktok": {
        "tiktok.com", "www.tiktok.com", "m.tiktok.com",
        "vm.tiktok.com", "vt.tiktok.com",
    },
    "telegram": {"t.me", "telegram.me", "www.t.me"},
    "youtube": {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
    },
}
_LIFECYCLES = {
    "UNVERIFIED", "NEW", "RISING", "MATURE", "DECLINING", "RESURGENCE",
}
_RUS_STOPWORDS = {
    "этот", "эта", "это", "эти", "такой", "такая", "такие", "когда", "тогда",
    "потом", "просто", "очень", "тоже", "только", "почему", "потому", "который",
    "которая", "которые", "чтобы", "если", "есть", "было", "будет", "сейчас",
    "вообще", "меня", "тебя", "него", "нее", "себя", "здесь", "можно", "нельзя",
    "видео", "ролик", "тикток", "коммент", "комментарий", "автор", "ссылка",
    "the", "this", "that", "with", "from", "your", "have", "just", "like",
}
_TOKEN_RE = re.compile(r"[a-zа-яё][a-zа-яё0-9-]{2,}", re.IGNORECASE)
_AUTHOR_LINE_RE = re.compile(r"^\s*(@?[A-Za-zА-Яа-яЁё0-9_.-]{1,40})\s*:\s*(.+)$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("дата публикации должна быть в ISO-формате") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_key(text: str) -> str:
    value = (text or "").casefold().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def normalize_comment(text: str) -> str:
    value = re.sub(r"https?://\S+", " ", text or "", flags=re.IGNORECASE)
    value = re.sub(r"@\S+", " ", value)
    return canonical_key(value)


def validate_source_url(source_type: str, source_url: str) -> str:
    source_type = (source_type or "").strip().lower()
    if source_type not in _SOURCE_HOSTS:
        raise ValueError("источник должен быть tiktok, telegram или youtube")
    value = (source_url or "").strip()
    if len(value) > 2048:
        raise ValueError("слишком длинная ссылка")
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or host not in _SOURCE_HOSTS[source_type]:
        raise ValueError(f"нужна публичная HTTPS-ссылка {source_type}")
    return value


@dataclass(frozen=True)
class SignalInput:
    term: str
    source_type: str
    source_url: str
    author: str = ""
    caption: str = ""
    comments: str = ""
    published_at: str | datetime | None = None
    views: int = 0
    likes: int = 0
    shares: int = 0
    comments_count: int = 0


def parse_comment_lines(raw: str) -> list[tuple[str, str]]:
    """Формат MVP: ``@автор: текст`` либо просто одна реплика на строку."""
    rows: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = _AUTHOR_LINE_RE.match(line)
        if match:
            author, text = match.group(1).lstrip("@"), match.group(2).strip()
        else:
            author, text = "", line
        if text:
            rows.append((author[:80], text[:1000]))
    return rows[:2000]


def _tokens(text: str) -> set[str]:
    result = set()
    for match in _TOKEN_RE.finditer((text or "").casefold().replace("ё", "е")):
        token = match.group(0).strip("-")
        if len(token) >= 4 and token not in _RUS_STOPWORDS and not token.isdigit():
            result.add(token)
    return result


class TrendRadarStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _ensure_schema(self) -> None:
        with self._schema_lock, self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS trends (
                    id TEXT PRIMARY KEY,
                    canonical_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    radar_first_seen_at TEXT NOT NULL,
                    earliest_published_at TEXT,
                    last_seen_at TEXT NOT NULL,
                    lifecycle TEXT NOT NULL DEFAULT 'UNVERIFIED',
                    score REAL NOT NULL DEFAULT 0,
                    novelty_score REAL NOT NULL DEFAULT 0,
                    burst_score REAL NOT NULL DEFAULT 0,
                    velocity_score REAL NOT NULL DEFAULT 0,
                    spread_score REAL NOT NULL DEFAULT 0,
                    merch_score REAL NOT NULL DEFAULT 0,
                    approved INTEGER NOT NULL DEFAULT 0,
                    rejected INTEGER NOT NULL DEFAULT 0,
                    notes TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS observations (
                    id TEXT PRIMARY KEY,
                    trend_id TEXT NOT NULL REFERENCES trends(id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL,
                    source_url TEXT NOT NULL UNIQUE,
                    author TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL,
                    published_at TEXT,
                    views INTEGER NOT NULL DEFAULT 0,
                    likes INTEGER NOT NULL DEFAULT 0,
                    shares INTEGER NOT NULL DEFAULT 0,
                    comments_count INTEGER NOT NULL DEFAULT 0,
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_observations_trend_time
                    ON observations(trend_id, observed_at);
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
                    trend_id TEXT NOT NULL REFERENCES trends(id) ON DELETE CASCADE,
                    author TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL,
                    normalized_text TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    UNIQUE(observation_id, author, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_comments_trend_time
                    ON comments(trend_id, observed_at);
                CREATE TABLE IF NOT EXISTS metric_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
                    trend_id TEXT NOT NULL REFERENCES trends(id) ON DELETE CASCADE,
                    captured_at TEXT NOT NULL,
                    views INTEGER NOT NULL DEFAULT 0,
                    likes INTEGER NOT NULL DEFAULT 0,
                    shares INTEGER NOT NULL DEFAULT 0,
                    comments_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(observation_id, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_metric_snapshots_trend_time
                    ON metric_snapshots(trend_id, captured_at);
                CREATE TABLE IF NOT EXISTS ingest_jobs (
                    id TEXT PRIMARY KEY,
                    trend_id TEXT NOT NULL REFERENCES trends(id) ON DELETE CASCADE,
                    observation_id TEXT NOT NULL REFERENCES observations(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # SQLite migrations for production databases created by radar MVP v1.
            trend_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(trends)")
            }
            if "velocity_score" not in trend_columns:
                db.execute(
                    "ALTER TABLE trends ADD COLUMN velocity_score REAL NOT NULL DEFAULT 0"
                )
            observation_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(observations)")
            }
            if "comments_count" not in observation_columns:
                db.execute(
                    "ALTER TABLE observations ADD COLUMN comments_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            # Existing observations become the first point in their time series.
            db.execute(
                """
                INSERT OR IGNORE INTO metric_snapshots (
                    observation_id, trend_id, captured_at,
                    views, likes, shares, comments_count
                )
                SELECT id, trend_id, observed_at, views, likes, shares, comments_count
                FROM observations
                """
            )

    @staticmethod
    def _insert_comments(
        db: sqlite3.Connection,
        observation_id: str,
        trend_id: str,
        comments: list[tuple[str, str]],
        observed_at: str,
    ) -> int:
        inserted = 0
        for author, text in comments:
            normalized = normalize_comment(text)
            if not normalized:
                continue
            fingerprint = hashlib.sha256(
                normalized.encode("utf-8")
            ).hexdigest()[:24]
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO comments (
                    observation_id, trend_id, author, text, normalized_text,
                    observed_at, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id, trend_id, author, text, normalized,
                    observed_at, fingerprint,
                ),
            )
            inserted += max(0, cursor.rowcount)
        return inserted

    @staticmethod
    def _record_metric_snapshot(
        db: sqlite3.Connection,
        *,
        observation_id: str,
        trend_id: str,
        captured_at: datetime,
        views: int,
        likes: int,
        shares: int,
        comments_count: int,
        metrics_changed: bool,
    ) -> bool:
        last = db.execute(
            """
            SELECT captured_at FROM metric_snapshots
            WHERE observation_id = ?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (observation_id,),
        ).fetchone()
        last_at = _parse_datetime(last["captured_at"]) if last else None
        # Exact repeats from a double-click remain idempotent. An unchanged
        # measurement is still valuable after five minutes: it proves zero growth.
        if (
            not metrics_changed
            and last_at is not None
            and captured_at - last_at < timedelta(minutes=5)
        ):
            return False
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO metric_snapshots (
                observation_id, trend_id, captured_at,
                views, likes, shares, comments_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id, trend_id, _iso(captured_at),
                views, likes, shares, comments_count,
            ),
        )
        return cursor.rowcount > 0

    def ingest_signal(
        self,
        signal: SignalInput,
        *,
        now: datetime | None = None,
    ) -> dict:
        now = now or utcnow()
        now_text = _iso(now)
        term = " ".join((signal.term or "").strip().split())
        key = canonical_key(term)
        if len(term) < 2 or len(term) > 120 or not key:
            raise ValueError("название мема/слово должно содержать 2–120 символов")
        source_type = (signal.source_type or "").strip().lower()
        source_url = validate_source_url(source_type, signal.source_url)
        published = _parse_datetime(signal.published_at)
        if published and published > now + timedelta(minutes=5):
            raise ValueError("дата публикации не может быть в будущем")
        for metric in (
            signal.views, signal.likes, signal.shares, signal.comments_count,
        ):
            if int(metric or 0) < 0:
                raise ValueError("метрики не могут быть отрицательными")

        trend_id = uuid.uuid4().hex[:16]
        observation_id = uuid.uuid4().hex[:16]
        job_id = uuid.uuid4().hex[:16]
        comments = parse_comment_lines(signal.comments)

        with self._connect() as db:
            existing_observation = db.execute(
                "SELECT * FROM observations WHERE source_url = ?",
                (source_url,),
            ).fetchone()
            if existing_observation:
                existing_trend = existing_observation["trend_id"]
                existing_id = existing_observation["id"]
                new_comments = self._insert_comments(
                    db, existing_id, existing_trend, comments, now_text,
                )
                stored_comment_count = db.execute(
                    "SELECT COUNT(*) AS count FROM comments WHERE observation_id = ?",
                    (existing_id,),
                ).fetchone()["count"]
                metrics_before = (
                    int(existing_observation["views"] or 0),
                    int(existing_observation["likes"] or 0),
                    int(existing_observation["shares"] or 0),
                    int(existing_observation["comments_count"] or 0),
                )
                metrics_after = (
                    max(metrics_before[0], int(signal.views or 0)),
                    max(metrics_before[1], int(signal.likes or 0)),
                    max(metrics_before[2], int(signal.shares or 0)),
                    max(
                        metrics_before[3],
                        int(signal.comments_count or 0),
                        int(stored_comment_count or 0),
                    ),
                )
                metrics_changed = metrics_after != metrics_before
                author = (
                    (signal.author or "").strip()[:120]
                    or existing_observation["author"]
                )
                caption = (
                    (signal.caption or "").strip()[:4000]
                    or existing_observation["caption"]
                )
                existing_published = _parse_datetime(
                    existing_observation["published_at"]
                )
                effective_published = existing_published
                if published and (
                    effective_published is None or published < effective_published
                ):
                    effective_published = published
                metadata_changed = (
                    author != existing_observation["author"]
                    or caption != existing_observation["caption"]
                    or effective_published != existing_published
                )
                db.execute(
                    """
                    UPDATE observations
                    SET author = ?, caption = ?, published_at = ?,
                        views = ?, likes = ?, shares = ?, comments_count = ?
                    WHERE id = ?
                    """,
                    (
                        author, caption,
                        _iso(effective_published) if effective_published else None,
                        *metrics_after, existing_id,
                    ),
                )
                trend_row = db.execute(
                    "SELECT earliest_published_at FROM trends WHERE id = ?",
                    (existing_trend,),
                ).fetchone()
                trend_earliest = _parse_datetime(
                    trend_row["earliest_published_at"]
                )
                if published and (
                    trend_earliest is None or published < trend_earliest
                ):
                    db.execute(
                        "UPDATE trends SET earliest_published_at = ? WHERE id = ?",
                        (_iso(published), existing_trend),
                    )
                db.execute(
                    "UPDATE trends SET last_seen_at = ?, rejected = 0 WHERE id = ?",
                    (now_text, existing_trend),
                )
                measurement_recorded = self._record_metric_snapshot(
                    db,
                    observation_id=existing_id,
                    trend_id=existing_trend,
                    captured_at=now,
                    views=metrics_after[0],
                    likes=metrics_after[1],
                    shares=metrics_after[2],
                    comments_count=metrics_after[3],
                    metrics_changed=metrics_changed,
                )
                job = db.execute(
                    "SELECT id, status FROM ingest_jobs WHERE observation_id = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (existing_id,),
                ).fetchone()
                self.recalculate(existing_trend, now=now, db=db)
                return {
                    "trend_id": existing_trend,
                    "observation_id": existing_id,
                    "job_id": job["id"] if job else None,
                    "job_status": job["status"] if job else "succeeded",
                    "duplicate": True,
                    "updated": bool(
                        metrics_changed or metadata_changed
                        or new_comments or measurement_recorded
                    ),
                    "new_comments": new_comments,
                    "measurement_recorded": measurement_recorded,
                }

            trend = db.execute(
                "SELECT * FROM trends WHERE canonical_key = ?", (key,),
            ).fetchone()
            if trend:
                trend_id = trend["id"]
                earliest = _parse_datetime(trend["earliest_published_at"])
                if published and (earliest is None or published < earliest):
                    db.execute(
                        "UPDATE trends SET earliest_published_at = ? WHERE id = ?",
                        (_iso(published), trend_id),
                    )
                db.execute(
                    "UPDATE trends SET last_seen_at = ?, rejected = 0 WHERE id = ?",
                    (now_text, trend_id),
                )
            else:
                db.execute(
                    """
                    INSERT INTO trends (
                        id, canonical_key, display_name, created_at,
                        radar_first_seen_at, earliest_published_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trend_id, key, term, now_text, now_text,
                        _iso(published) if published else None, now_text,
                    ),
                )

            db.execute(
                """
                INSERT INTO observations (
                    id, trend_id, source_type, source_url, author, caption,
                    observed_at, published_at, views, likes, shares
                    , comments_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id, trend_id, source_type, source_url,
                    (signal.author or "").strip()[:120],
                    (signal.caption or "").strip()[:4000],
                    now_text, _iso(published) if published else None,
                    int(signal.views or 0), int(signal.likes or 0),
                    int(signal.shares or 0),
                    max(int(signal.comments_count or 0), len(comments)),
                ),
            )
            new_comments = self._insert_comments(
                db, observation_id, trend_id, comments, now_text,
            )
            self._record_metric_snapshot(
                db,
                observation_id=observation_id,
                trend_id=trend_id,
                captured_at=now,
                views=int(signal.views or 0),
                likes=int(signal.likes or 0),
                shares=int(signal.shares or 0),
                comments_count=max(int(signal.comments_count or 0), new_comments),
                metrics_changed=True,
            )
            status = "pending" if source_type == "tiktok" else "succeeded"
            db.execute(
                """
                INSERT INTO ingest_jobs (
                    id, trend_id, observation_id, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, trend_id, observation_id, status, now_text, now_text),
            )
            self.recalculate(trend_id, now=now, db=db)

        return {
            "trend_id": trend_id,
            "observation_id": observation_id,
            "job_id": job_id,
            "job_status": status,
            "duplicate": False,
            "updated": True,
            "new_comments": new_comments,
            "measurement_recorded": True,
        }

    def _comment_term_stats(
        self,
        trend_id: str,
        now: datetime,
        db: sqlite3.Connection,
    ) -> list[dict]:
        since = _iso(now - timedelta(days=7))
        rows = db.execute(
            """
            SELECT c.author, c.normalized_text, c.observed_at, c.fingerprint,
                   o.source_type, o.id AS observation_id
            FROM comments c
            JOIN observations o ON o.id = c.observation_id
            WHERE c.trend_id = ? AND c.observed_at >= ?
            """,
            (trend_id, since),
        ).fetchall()
        recent_cutoff = now - timedelta(hours=6)
        recent: dict[str, list[sqlite3.Row]] = defaultdict(list)
        baseline: Counter[str] = Counter()
        for row in rows:
            observed = _parse_datetime(row["observed_at"]) or now
            for token in _tokens(row["normalized_text"]):
                if observed >= recent_cutoff:
                    recent[token].append(row)
                else:
                    baseline[token] += 1

        stats = []
        for token, token_rows in recent.items():
            recent_count = len(token_rows)
            if recent_count < 2:
                continue
            authors = {r["author"] for r in token_rows if r["author"]}
            observations = {r["observation_id"] for r in token_rows}
            sources = {r["source_type"] for r in token_rows}
            fingerprints = {r["fingerprint"] for r in token_rows}
            duplicate_ratio = 1.0 - len(fingerprints) / max(1, recent_count)
            expected = baseline[token] * (6 / 162)
            growth = (recent_count + 1) / (expected + 1)
            author_signal = len(authors) if authors else min(2, len(observations))
            raw = (
                15 * math.log1p(recent_count)
                + 7 * author_signal
                + 10 * len(observations)
                + 8 * len(sources)
                + min(25, 6 * math.log1p(growth))
            )
            score = max(0.0, min(100.0, raw * (1.0 - 0.75 * duplicate_ratio)))
            stats.append(
                {
                    "term": token,
                    "mentions_6h": recent_count,
                    "baseline_mentions": baseline[token],
                    "growth": round(growth, 2),
                    "unique_authors": len(authors),
                    "observation_count": len(observations),
                    "source_count": len(sources),
                    "copy_ratio": round(duplicate_ratio, 3),
                    "score": round(score, 1),
                }
            )
        stats.sort(key=lambda item: (item["score"], item["mentions_6h"]), reverse=True)
        return stats[:8]

    def _velocity_stats(
        self,
        trend_id: str,
        now: datetime,
        db: sqlite3.Connection,
    ) -> dict:
        rows = db.execute(
            """
            SELECT observation_id, captured_at, views, likes, shares, comments_count
            FROM metric_snapshots
            WHERE trend_id = ? AND captured_at >= ?
            ORDER BY observation_id, captured_at
            """,
            (trend_id, _iso(now - timedelta(days=7))),
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            grouped[row["observation_id"]].append(row)

        views_per_hour = 0.0
        likes_per_hour = 0.0
        shares_per_hour = 0.0
        comments_per_hour = 0.0
        growing_observations = 0
        total_first_views = 0
        total_last_views = 0
        for snapshots in grouped.values():
            if len(snapshots) < 2:
                continue
            first, last = snapshots[0], snapshots[-1]
            first_at = _parse_datetime(first["captured_at"])
            last_at = _parse_datetime(last["captured_at"])
            if first_at is None or last_at is None:
                continue
            hours = (last_at - first_at).total_seconds() / 3600
            # Счётчики источника могут уточниться сразу после первого запроса.
            # Сохраняем такой снимок, но не принимаем поправку короче пяти минут
            # за реальную скорость распространения мема.
            if hours < (5 / 60):
                continue
            deltas = {
                name: max(0, int(last[name] or 0) - int(first[name] or 0))
                for name in ("views", "likes", "shares", "comments_count")
            }
            views_per_hour += deltas["views"] / hours
            likes_per_hour += deltas["likes"] / hours
            shares_per_hour += deltas["shares"] / hours
            comments_per_hour += deltas["comments_count"] / hours
            total_first_views += max(0, int(first["views"] or 0))
            total_last_views += max(0, int(last["views"] or 0))
            if any(deltas.values()):
                growing_observations += 1

        growth_ratio = (
            (total_last_views + 1) / (total_first_views + 1)
            if total_first_views or total_last_views else 1.0
        )
        score = min(
            100.0,
            14 * math.log1p(views_per_hour / 100)
            + 8 * math.log1p(likes_per_hour / 25)
            + 12 * math.log1p(shares_per_hour)
            + 9 * math.log1p(comments_per_hour)
            + min(20.0, max(0.0, growth_ratio - 1.0) * 10),
        )
        return {
            "views_per_hour": round(views_per_hour, 1),
            "likes_per_hour": round(likes_per_hour, 1),
            "shares_per_hour": round(shares_per_hour, 1),
            "comments_per_hour": round(comments_per_hour, 1),
            "view_growth_ratio": round(growth_ratio, 2),
            "growing_observations": growing_observations,
            "score": round(score, 1),
        }

    def recalculate(
        self,
        trend_id: str,
        *,
        now: datetime | None = None,
        db: sqlite3.Connection | None = None,
    ) -> dict:
        now = now or utcnow()
        owns_connection = db is None
        connection = db or self._connect()
        try:
            trend = connection.execute(
                "SELECT * FROM trends WHERE id = ?", (trend_id,),
            ).fetchone()
            if not trend:
                raise KeyError(trend_id)
            observations = connection.execute(
                "SELECT * FROM observations WHERE trend_id = ?",
                (trend_id,),
            ).fetchall()
            terms = self._comment_term_stats(trend_id, now, connection)
            velocity = self._velocity_stats(trend_id, now, connection)
            velocity_score = velocity["score"]

            earliest = _parse_datetime(trend["earliest_published_at"])
            last_seen = _parse_datetime(trend["last_seen_at"]) or now
            age_days = (
                max(0.0, (now - earliest).total_seconds() / 86400)
                if earliest else None
            )
            sources = {r["source_type"] for r in observations}
            authors = {r["author"].casefold() for r in observations if r["author"]}
            recent_48h = connection.execute(
                """
                SELECT COUNT(DISTINCT observation_id) AS count
                FROM metric_snapshots
                WHERE trend_id = ? AND captured_at >= ?
                """,
                (trend_id, _iso(now - timedelta(hours=48))),
            ).fetchone()["count"]
            previous = connection.execute(
                """
                SELECT COUNT(DISTINCT observation_id) AS count
                FROM metric_snapshots
                WHERE trend_id = ? AND captured_at >= ? AND captured_at < ?
                """,
                (
                    trend_id,
                    _iso(now - timedelta(days=14)),
                    _iso(now - timedelta(hours=48)),
                ),
            ).fetchone()["count"]
            max_term_burst = max((item["score"] for item in terms), default=0.0)
            burst_score = min(
                100.0,
                max_term_burst * 0.42
                + velocity_score * 0.48
                + min(10.0, 4 * recent_48h),
            )
            spread_score = min(
                100.0,
                12 * len(observations) + 18 * len(sources) + 6 * len(authors),
            )
            novelty_score = (
                20.0 if age_days is None
                else max(0.0, 100.0 - 3.2 * age_days)
            )
            display_len = len(trend["display_name"])
            merch_score = 75.0 if 3 <= display_len <= 30 else 50.0

            # Жёсткий age/evidence gate: дата без второго независимого сигнала
            # тоже не считается доказанным рождением мема.
            independent_evidence = len(observations) >= 2 and (
                len(authors) >= 2 or len(sources) >= 2
            )
            if earliest is None or not independent_evidence:
                lifecycle = "UNVERIFIED"
            elif age_days <= 7:
                lifecycle = (
                    "RISING"
                    if (
                        velocity_score >= 55
                        or (len(observations) >= 3 and burst_score >= 55)
                    )
                    else "NEW"
                )
            elif age_days <= 21 and (
                velocity_score >= 50
                or (recent_48h >= 2 and burst_score >= 45)
            ):
                lifecycle = "RISING"
            elif (
                age_days > 21
                and recent_48h >= 2
                and velocity_score >= 70
                and recent_48h > previous
            ):
                lifecycle = "RESURGENCE"
            elif (now - last_seen) > timedelta(days=7) or (
                previous >= 3 and recent_48h == 0
            ):
                lifecycle = "DECLINING"
            else:
                lifecycle = "MATURE"
            if lifecycle not in _LIFECYCLES:
                lifecycle = "UNVERIFIED"

            score = (
                0.4 * burst_score
                + 0.25 * spread_score
                + 0.2 * novelty_score
                + 0.15 * merch_score
            )
            if lifecycle == "UNVERIFIED":
                score = min(score, 49.0)
            elif lifecycle in {"MATURE", "DECLINING"}:
                score *= 0.72 if lifecycle == "MATURE" else 0.45

            connection.execute(
                """
                UPDATE trends
                SET lifecycle = ?, score = ?, novelty_score = ?,
                    burst_score = ?, velocity_score = ?,
                    spread_score = ?, merch_score = ?
                WHERE id = ?
                """,
                (
                    lifecycle, round(score, 1), round(novelty_score, 1),
                    round(burst_score, 1), round(velocity_score, 1),
                    round(spread_score, 1),
                    round(merch_score, 1), trend_id,
                ),
            )
            if owns_connection:
                connection.commit()
            return {
                "lifecycle": lifecycle,
                "score": round(score, 1),
                "age_days": round(age_days, 1) if age_days is not None else None,
                "emerging_terms": terms,
                "velocity": velocity,
            }
        finally:
            if owns_connection:
                connection.close()

    def run_ingest_job(
        self,
        job_id: str,
        *,
        fetch: Callable = requests.get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Идемпотентное oEmbed-обогащение. Сигнал сохраняется даже при сбое сети."""
        with self._connect() as db:
            job = db.execute(
                "SELECT * FROM ingest_jobs WHERE id = ?", (job_id,),
            ).fetchone()
            if not job or job["status"] == "succeeded":
                return
            observation = db.execute(
                "SELECT * FROM observations WHERE id = ?",
                (job["observation_id"],),
            ).fetchone()
            if not observation:
                return
            if observation["source_type"] != "tiktok":
                db.execute(
                    "UPDATE ingest_jobs SET status = 'succeeded', updated_at = ? WHERE id = ?",
                    (_iso(utcnow()), job_id),
                )
                return
            db.execute(
                "UPDATE ingest_jobs SET status = 'running', updated_at = ? WHERE id = ?",
                (_iso(utcnow()), job_id),
            )

        last_error = ""
        for attempt in range(1, 3):
            try:
                response = fetch(
                    "https://www.tiktok.com/oembed",
                    params={"url": observation["source_url"]},
                    timeout=8,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("type") not in {"video", "rich"}:
                    raise ValueError("TikTok oEmbed вернул неожиданный ответ")
                title = str(payload.get("title") or "").strip()[:4000]
                author = str(payload.get("author_name") or "").strip()[:120]
                thumbnail = str(payload.get("thumbnail_url") or "").strip()[:2048]
                parsed_thumbnail = urlparse(thumbnail)
                if parsed_thumbnail.scheme != "https" or not parsed_thumbnail.hostname:
                    thumbnail = ""
                safe_metadata = {
                    "provider_name": payload.get("provider_name"),
                    "author_url": payload.get("author_url"),
                    "width": payload.get("width"),
                    "height": payload.get("height"),
                }
                with self._connect() as db:
                    db.execute(
                        """
                        UPDATE observations
                        SET caption = CASE WHEN caption = '' THEN ? ELSE caption END,
                            author = CASE WHEN author = '' THEN ? ELSE author END,
                            thumbnail_url = ?,
                            metadata_json = ?
                        WHERE id = ?
                        """,
                        (
                            title, author, thumbnail,
                            json.dumps(safe_metadata, ensure_ascii=False),
                            observation["id"],
                        ),
                    )
                    db.execute(
                        """
                        UPDATE ingest_jobs
                        SET status = 'succeeded', attempts = ?, error = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (attempt, _iso(utcnow()), job_id),
                    )
                    self.recalculate(observation["trend_id"], db=db)
                return
            except Exception as exc:  # noqa: BLE001 — внешний API ненадёжен
                last_error = str(exc)[:500]
                logger.warning(f"radar oEmbed job {job_id}, attempt {attempt}: {last_error}")
                if attempt < 2:
                    sleep(0.25)

        with self._connect() as db:
            db.execute(
                """
                UPDATE ingest_jobs
                SET status = 'failed', attempts = 2, error = ?, updated_at = ?
                WHERE id = ?
                """,
                (last_error or "неизвестная ошибка oEmbed", _iso(utcnow()), job_id),
            )

    def job(self, job_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM ingest_jobs WHERE id = ?", (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def _trend_payload(self, trend_id: str, db: sqlite3.Connection) -> dict:
        trend = db.execute(
            "SELECT * FROM trends WHERE id = ?", (trend_id,),
        ).fetchone()
        if not trend:
            raise KeyError(trend_id)
        observations = [
            dict(row) for row in db.execute(
                """
                SELECT id, source_type, source_url, author, caption, observed_at,
                       published_at, views, likes, shares, comments_count,
                       thumbnail_url
                FROM observations WHERE trend_id = ?
                ORDER BY observed_at DESC
                """,
                (trend_id,),
            ).fetchall()
        ]
        result = dict(trend)
        result["approved"] = bool(result["approved"])
        result["rejected"] = bool(result["rejected"])
        result["observation_count"] = len(observations)
        result["source_count"] = len({item["source_type"] for item in observations})
        result["observations"] = observations
        result["measurement_count"] = db.execute(
            "SELECT COUNT(*) AS count FROM metric_snapshots WHERE trend_id = ?",
            (trend_id,),
        ).fetchone()["count"]
        result["velocity"] = self._velocity_stats(trend_id, utcnow(), db)
        earliest = _parse_datetime(result["earliest_published_at"])
        result["age_days"] = (
            round(max(0.0, (utcnow() - earliest).total_seconds() / 86400), 1)
            if earliest else None
        )
        result["emerging_terms"] = self._comment_term_stats(trend_id, utcnow(), db)
        return result

    def get_trend(self, trend_id: str) -> dict | None:
        with self._connect() as db:
            try:
                return self._trend_payload(trend_id, db)
            except KeyError:
                return None

    def list_trends(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        with self._connect() as db:
            ids = [
                row["id"] for row in db.execute(
                    """
                    SELECT id FROM trends
                    ORDER BY rejected ASC, approved DESC, score DESC, last_seen_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]
            return [self._trend_payload(trend_id, db) for trend_id in ids]

    def set_decision(self, trend_id: str, decision: str) -> dict:
        if decision not in {"approve", "reject", "reset"}:
            raise ValueError("неизвестное решение")
        approved = 1 if decision == "approve" else 0
        rejected = 1 if decision == "reject" else 0
        if decision == "reset":
            approved = rejected = 0
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE trends SET approved = ?, rejected = ? WHERE id = ?",
                (approved, rejected, trend_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(trend_id)
            return self._trend_payload(trend_id, db)

    def generation_prompt(self, trend_id: str) -> str:
        trend = self.get_trend(trend_id)
        if trend is None:
            raise KeyError(trend_id)
        if not trend["approved"]:
            raise PermissionError("сначала подтвердите тренд")
        phrases = [
            item["term"] for item in trend["emerging_terms"][:4]
            if item["score"] >= 25
        ]
        captions = [
            item["caption"] for item in trend["observations"]
            if item["caption"]
        ][:3]
        evidence = "; ".join(captions)[:1200] or "референсная фраза отсутствует"
        phrase_hint = ", ".join(phrases) or trend["display_name"]
        return (
            "Создай самостоятельный коммерческий фигурный мем-принт по подтверждённому "
            f"владельцем тренду «{trend['display_name']}». Смысловые слова аудитории: "
            f"{phrase_hint}. Контекст источников: {evidence}. Не копируй кадр или чужую "
            "композицию буквально: преобразуй идею в новый выразительный образ и одну "
            "короткую точную надпись. Нужен цельный принт, не стикер, не карточка, не "
            "прямоугольный скриншот. Изолированная композиция на ровном green/blue "
            "chroma с открытыми промежутками и свободными краями для GreenKey."
        )
