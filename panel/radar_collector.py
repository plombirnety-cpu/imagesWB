# -*- coding: utf-8 -*-
"""Автоматический сборщик сигналов для радара мемов.

Google Trends и публичные Telegram-каналы дают поисковые темы. Bright Data
находит по ним TikTok-ролики и комментарии. Все фоновые запуски сохраняются в
SQLite, не пересекаются и безопасно переживают перезапуск веб-процесса.
"""
from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Iterable
from urllib.parse import quote

import requests
from loguru import logger

from trend_radar import (
    SignalInput,
    TrendRadarStore,
    canonical_key,
    viable_seed_term,
)

_POSTS_DATASET = "gd_lu702nij2f790tmv9h"
_COMMENTS_DATASET = "gd_lkf2st302ap89utw5k"
_SCRAPE_URL = "https://api.brightdata.com/datasets/v3/scrape"
_PROGRESS_URL = "https://api.brightdata.com/datasets/v3/progress/{snapshot_id}"
_SNAPSHOT_URL = (
    "https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json"
)
_HASHTAG_RE = re.compile(r"#([a-zа-яё][a-zа-яё0-9_-]{2,})", re.IGNORECASE)
_UNSAFE_SEED_RE = re.compile(
    r"\b(порно|pornhub|эротик|казино|ставк[аи]|наркотик)\b", re.IGNORECASE,
)
_GENERIC_TIKTOK_TAGS = {
    "fyp", "fy", "viral", "trend", "trending", "tiktok", "тикток",
    "мем", "мемы", "прикол", "приколы", "юмор", "смешно",
    "рек", "реки", "рекомендации", "популярное", "тренды",
}

_MEMSEARCH_SKIP_PREFIXES = (
    "друзья", "реклама", "подпис", "розыгрыш", "в приюте", "сбор ",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def tiktok_candidate_terms(record: dict, search_term: str) -> list[str]:
    """Извлекает конкретные хештеги ролика для следующего шага поиска."""
    candidates: list[str] = []
    description = str(record.get("description") or record.get("caption") or "")
    candidates.extend(match.group(1) for match in _HASHTAG_RE.finditer(description))
    raw_hashtags = record.get("hashtags") or record.get("challenges") or []
    if isinstance(raw_hashtags, list):
        for item in raw_hashtags:
            if isinstance(item, dict):
                value = (
                    item.get("name")
                    or item.get("title")
                    or item.get("hashtag_name")
                    or ""
                )
            else:
                value = item
            candidates.append(str(value).lstrip("#"))
    current_key = canonical_key(search_term)
    result: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        display = " ".join(candidate.strip().split())
        key = canonical_key(display)
        if (
            not viable_seed_term(display)
            or len(key.replace(" ", "")) < 7
            or key == current_key
            or key in _GENERIC_TIKTOK_TAGS
            or key.startswith(("fyp", "рек", "recommend"))
            or key in seen
        ):
            continue
        seen.add(key)
        result.append(display)
    return result[:5]


def confirmed_tiktok_candidates(records: list[dict], search_term: str) -> list[str]:
    """Оставляет хештеги, найденные минимум у двух независимых авторов/роликов."""
    labels: dict[str, str] = {}
    sources: dict[str, set[str]] = {}
    for record in records:
        source = canonical_key(str(
            record.get("profile_username")
            or record.get("author_username")
            or record.get("post_id")
            or record.get("id")
            or ""
        ))
        if not source:
            continue
        for candidate in tiktok_candidate_terms(record, search_term):
            key = canonical_key(candidate)
            labels.setdefault(key, candidate)
            sources.setdefault(key, set()).add(source)
    return [
        labels[key]
        for key, identities in sources.items()
        if len(identities) >= 2
    ][:5]


def comment_candidate_is_confirmed(candidate: dict, search_term: str) -> bool:
    """Требует независимое повторение, а не один шумный comments snapshot."""
    term = str(candidate.get("term") or "")
    key = canonical_key(term)
    return bool(
        candidate.get("score", 0) >= 35
        and candidate.get("unique_authors", 0) >= 3
        and candidate.get("observation_count", 0) >= 2
        and len(key.replace(" ", "")) >= 7
        and viable_seed_term(term)
        and key != canonical_key(search_term)
        and key not in _GENERIC_TIKTOK_TAGS
    )


@dataclass(frozen=True)
class SeedTerm:
    term: str
    source_type: str
    source_url: str = ""


@dataclass(frozen=True)
class CollectorConfig:
    enabled: bool = True
    interval_seconds: int = 10_800
    initial_delay_seconds: int = 20
    google_geo: str = "RU"
    telegram_channels: tuple[str, ...] = ()
    discovery_terms_per_run: int = 6
    posts_per_term: int = 5
    comments_posts_per_run: int = 4


class GoogleTrendsSource:
    def __init__(
        self,
        *,
        geo: str = "RU",
        timeout: int = 30,
        get: Callable | None = None,
    ):
        self.geo = geo
        self.timeout = timeout
        self._get = get or requests.get

    def collect(self) -> list[SeedTerm]:
        url = f"https://trends.google.com/trending/rss?geo={quote(self.geo)}"
        response = self._get(
            url,
            timeout=self.timeout,
            headers={"User-Agent": "PrintFactoryTrendRadar/1.0"},
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        result: list[SeedTerm] = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if title:
                result.append(
                    SeedTerm(
                        title[:120],
                        "google_trends",
                        f"https://trends.google.com/trending?geo={quote(self.geo)}",
                    )
                )
        return result


class _TelegramPageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._in_text = 0
        self._parts: list[str] = []
        self._post = ""
        self.messages: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        if "tgme_widget_message" in classes:
            self._post = values.get("data-post") or ""
        if "tgme_widget_message_text" in classes:
            self._in_text = 1
            self._parts = []
        elif self._in_text:
            self._in_text += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._in_text:
            return
        self._in_text -= 1
        if self._in_text == 0:
            text = " ".join("".join(self._parts).split())
            if text:
                self.messages.append((self._post, text))
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_text:
            self._parts.append(data)


class TelegramPublicSource:
    """Извлекает явные хэштеги из публичных Telegram-каналов.

    Обычные повторяющиеся слова в постах намеренно не считаются мемом: это
    порождало бы сотни ложных кандидатов вроде «работы» и «рублей». Повторы
    комментариев оцениваются на TikTok-стадии, где есть авторы и метрики ролика.
    """

    def __init__(
        self,
        channels: Iterable[str],
        *,
        timeout: int = 30,
        get: Callable | None = None,
    ):
        self.channels = tuple(item.strip().lstrip("@") for item in channels if item.strip())
        self.timeout = timeout
        self._get = get or requests.get

    def collect(self) -> list[SeedTerm]:
        candidates: dict[str, SeedTerm] = {}
        for channel in self.channels:
            url = f"https://t.me/s/{quote(channel)}"
            response = self._get(
                url,
                timeout=self.timeout,
                headers={"User-Agent": "Mozilla/5.0 PrintFactoryTrendRadar/1.0"},
            )
            response.raise_for_status()
            parser = _TelegramPageParser()
            parser.feed(response.text)
            for post, text in parser.messages:
                post_url = f"https://t.me/{post}" if post else url
                for hashtag in _HASHTAG_RE.findall(text):
                    key = canonical_key(hashtag)
                    if key:
                        candidates[key] = SeedTerm(hashtag, "telegram", post_url)
                # memsearch — публичный каталог мемов, где первый фрагмент до
                # запятой является названием/цитатой мема, а дальше идут теги.
                # Это качественнее общей частотности слов по Telegram-постам.
                if channel.casefold() == "memsearch":
                    phrase = text.split("👤", 1)[0].split(",", 1)[0].strip()
                    phrase = " ".join(phrase.split())
                    if (
                        3 <= len(phrase) <= 80
                        and not phrase.casefold().startswith(_MEMSEARCH_SKIP_PREFIXES)
                        and not _UNSAFE_SEED_RE.search(phrase)
                    ):
                        key = canonical_key(phrase)
                        if key:
                            candidates[key] = SeedTerm(
                                phrase, "telegram_memes", post_url,
                            )
        return list(candidates.values())


class BrightDataClient:
    def __init__(
        self,
        token: str,
        *,
        timeout: int = 30,
        request: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.token = token.strip()
        self.timeout = timeout
        self._request = request or requests.request
        self._sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _call(self, method: str, url: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.token}"
        request_timeout = int(kwargs.pop("request_timeout", self.timeout))
        attempts = max(1, int(kwargs.pop("attempts", 3)))
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._request(
                    method, url, headers=headers, timeout=request_timeout, **kwargs,
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt < attempts - 1:
                    self._sleep(1.5 * (2**attempt))
        assert last_error is not None
        raise last_error

    def _records(self, response) -> list[dict]:
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        records = payload.get("data") or payload.get("results")
        if isinstance(records, list):
            return [item for item in records if isinstance(item, dict)]
        snapshot_id = payload.get("snapshot_id")
        if not snapshot_id:
            return []
        deadline = time.monotonic() + max(60, self.timeout * 4)
        while time.monotonic() < deadline:
            progress = self._call(
                "GET", _PROGRESS_URL.format(snapshot_id=snapshot_id),
            ).json()
            status = str(progress.get("status", "")).lower()
            if status == "ready":
                downloaded = self._call(
                    "GET", _SNAPSHOT_URL.format(snapshot_id=snapshot_id),
                ).json()
                return downloaded if isinstance(downloaded, list) else []
            if status in {"failed", "error"}:
                raise RuntimeError(f"Bright Data snapshot {snapshot_id}: {status}")
            self._sleep(2)
        raise TimeoutError(f"Bright Data snapshot {snapshot_id} не завершился вовремя")

    def discover(self, keyword: str, limit: int) -> list[dict]:
        if not self.configured:
            return []
        response = self._call(
            "POST",
            _SCRAPE_URL,
            params={
                "dataset_id": _POSTS_DATASET,
                "type": "discover_new",
                "discover_by": "keyword",
                "format": "json",
                "include_errors": "true",
            },
            json={"input": [{"search_keyword": keyword, "num_of_posts": int(limit)}]},
            request_timeout=max(75, self.timeout),
            attempts=1,
        )
        return self._records(response)

    def comments(self, post_url: str) -> list[dict]:
        if not self.configured:
            return []
        response = self._call(
            "POST",
            _SCRAPE_URL,
            params={
                "dataset_id": _COMMENTS_DATASET,
                "format": "json",
                "include_errors": "true",
            },
            json={"input": [{"url": post_url}]},
            request_timeout=max(75, self.timeout),
            attempts=1,
        )
        return self._records(response)

    @staticmethod
    def post_url(record: dict) -> str:
        direct = str(record.get("url") or record.get("post_url") or "").strip()
        if direct.startswith("https://www.tiktok.com/"):
            return direct
        post_id = str(record.get("post_id") or record.get("id") or "").strip()
        username = str(
            record.get("profile_username")
            or record.get("author_username")
            or record.get("username")
            or ""
        ).strip().lstrip("@")
        if post_id and username:
            return f"https://www.tiktok.com/@{username}/video/{post_id}"
        return ""

    @staticmethod
    def published_at(record: dict) -> str | None:
        value = record.get("create_time") or record.get("created_at")
        if isinstance(value, (int, float)):
            return _iso(datetime.fromtimestamp(value, tz=timezone.utc))
        text = str(value or "").strip()
        return text or None


class CollectorCancelled(RuntimeError):
    """Raised between provider calls when the owner stops a radar run."""


class RadarCollector:
    def __init__(
        self,
        store: TrendRadarStore,
        config: CollectorConfig,
        *,
        google: GoogleTrendsSource,
        telegram: TelegramPublicSource,
        tiktok: BrightDataClient,
        executor: ThreadPoolExecutor | None = None,
    ):
        self.store = store
        self.config = config
        self.google = google
        self.telegram = telegram
        self.tiktok = tiktok
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="radar-collector",
        )
        self._run_lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._active_run_id: str | None = None
        self._stop = threading.Event()
        self._cancel_requested = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._next_run_at: datetime | None = None

    def start(self) -> None:
        if not self.config.enabled or (self._scheduler and self._scheduler.is_alive()):
            return
        self._stop.clear()
        self._next_run_at = datetime.fromtimestamp(
            time.time() + self.config.initial_delay_seconds, tz=timezone.utc,
        )
        self._scheduler = threading.Thread(
            target=self._schedule_loop,
            name="radar-scheduler",
            daemon=True,
        )
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel_requested.set()
        if self._scheduler and self._scheduler.is_alive():
            self._scheduler.join(timeout=2)

    def _schedule_loop(self) -> None:
        if self._stop.wait(self.config.initial_delay_seconds):
            return
        while not self._stop.is_set():
            self.queue_run("schedule")
            self._next_run_at = datetime.fromtimestamp(
                time.time() + self.config.interval_seconds, tz=timezone.utc,
            )
            if self._stop.wait(self.config.interval_seconds):
                return

    def queue_run(self, trigger: str = "manual") -> dict:
        with self._queue_lock:
            if self._active_run_id is not None:
                return {
                    "queued": False,
                    "reason": "already_running",
                    "run": self.store.collector_run(self._active_run_id),
                }
            run_id = self.store.create_collector_run(trigger)
            self._cancel_requested.clear()
            self._active_run_id = run_id
        try:
            self._executor.submit(self._run, run_id)
        except Exception:
            with self._queue_lock:
                self._active_run_id = None
            self.store.update_collector_run(
                run_id, status="failed", error="не удалось поставить запуск в очередь",
            )
            raise
        return {"queued": True, "run_id": run_id}

    def cancel_run(self) -> dict:
        with self._queue_lock:
            run_id = self._active_run_id
            if run_id is None:
                return {"cancelled": False, "reason": "not_running"}
            self._cancel_requested.set()
        if not self.store.request_collector_cancel(run_id):
            return {"cancelled": False, "reason": "already_finished"}
        return {"cancelled": True, "run_id": run_id}

    def _check_cancelled(self) -> None:
        if self._cancel_requested.is_set():
            raise CollectorCancelled("остановлено владельцем")

    def _run(self, run_id: str) -> None:
        if not self._run_lock.acquire(blocking=False):
            self.store.update_collector_run(
                run_id, status="failed", error="другой запуск уже выполняется",
            )
            with self._queue_lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None
            return
        created = updated = 0
        errors: list[str] = []
        try:
            self.store.update_collector_run(
                run_id,
                status="running",
                phase="sources",
                current_term="Google Trends и Telegram",
            )
            self._check_cancelled()
            seeds: list[SeedTerm] = []
            for name, source in (("Google Trends", self.google), ("Telegram", self.telegram)):
                self._check_cancelled()
                try:
                    seeds.extend(source.collect())
                except Exception as exc:  # isolated provider failure
                    logger.warning(f"radar {name} source failed: {exc}")
                    errors.append(f"{name}: {exc}")
            unique_seeds: dict[str, SeedTerm] = {}
            for seed in seeds:
                key = canonical_key(seed.term)
                if key and viable_seed_term(seed.term):
                    unique_seeds[key] = seed
            for seed in unique_seeds.values():
                self.store.upsert_seed(
                    seed.term, seed.source_type, seed.source_url,
                )

            self._check_cancelled()
            promoted_seeds = 0
            if self.tiktok.configured:
                seen_urls: set[str] = set()
                comment_budget = self.config.comments_posts_per_run
                terms = self.store.discovery_terms(
                    self.config.discovery_terms_per_run,
                )
                self.store.update_collector_run(
                    run_id,
                    status="running",
                    phase="tiktok_discovery",
                    current_term="",
                    steps_total=len(terms),
                    steps_done=0,
                    seeds_found=len(unique_seeds),
                )
                for term_index, term in enumerate(terms):
                    self._check_cancelled()
                    term_has_comments = False
                    self.store.update_collector_run(
                        run_id, status="running", phase="tiktok_discovery",
                        current_term=term, steps_total=len(terms),
                        steps_done=term_index, seeds_found=len(unique_seeds) + promoted_seeds,
                        signals_created=created, signals_updated=updated,
                    )
                    try:
                        records = self.tiktok.discover(
                            term, self.config.posts_per_term,
                        )
                    except Exception as exc:
                        logger.warning(f"radar TikTok discovery '{term}' failed: {exc}")
                        errors.append(f"TikTok «{term}»: {exc}")
                        self.store.update_collector_run(
                            run_id, status="running", phase="tiktok_discovery",
                            current_term=term, steps_total=len(terms),
                            steps_done=term_index + 1,
                            signals_created=created, signals_updated=updated,
                        )
                        continue
                    self._check_cancelled()
                    records.sort(
                        key=lambda item: (
                            int(item.get("share_count") or 0),
                            int(item.get("comment_count") or 0),
                            int(item.get("play_count") or item.get("views") or 0),
                        ),
                        reverse=True,
                    )
                    confirmed_hashtags = confirmed_tiktok_candidates(
                        records, term,
                    )
                    for record in records:
                        source_url = self.tiktok.post_url(record)
                        if not source_url or source_url in seen_urls:
                            continue
                        seen_urls.add(source_url)
                        comment_rows: list[dict] = []
                        if comment_budget > 0 and not term_has_comments:
                            self._check_cancelled()
                            self.store.update_collector_run(
                                run_id, status="running", phase="tiktok_comments",
                                current_term=term, steps_total=len(terms),
                                steps_done=term_index, seeds_found=len(unique_seeds) + promoted_seeds,
                                signals_created=created, signals_updated=updated,
                            )
                            try:
                                comment_rows = self.tiktok.comments(source_url)
                                term_has_comments = True
                                comment_budget -= 1
                            except Exception as exc:
                                errors.append(f"Комментарии TikTok: {exc}")
                        self._check_cancelled()
                        comment_text = "\n".join(
                            f"@{str(row.get('commenter_user_name') or row.get('username') or '').lstrip('@')}: "
                            f"{row.get('comment_text') or row.get('text') or ''}"
                            for row in comment_rows
                            if row.get("comment_text") or row.get("text")
                        )
                        result = self.store.ingest_signal(
                            SignalInput(
                                term=term,
                                source_type="tiktok",
                                source_url=source_url,
                                author=str(
                                    record.get("profile_username")
                                    or record.get("author_username")
                                    or ""
                                ),
                                caption=str(
                                    record.get("description")
                                    or record.get("caption")
                                    or ""
                                ),
                                comments=comment_text,
                                published_at=self.tiktok.published_at(record),
                                views=int(
                                    record.get("play_count")
                                    or record.get("views")
                                    or 0
                                ),
                                likes=int(
                                    record.get("digg_count")
                                    or record.get("like_count")
                                    or 0
                                ),
                                shares=int(record.get("share_count") or 0),
                                comments_count=int(
                                    record.get("comment_count") or len(comment_rows)
                                ),
                            )
                        )
                        if result["duplicate"]:
                            updated += int(bool(result["updated"]))
                        else:
                            created += 1
                        trend = self.store.get_trend(result["trend_id"])
                        if trend:
                            for candidate in trend["emerging_terms"]:
                                if not comment_candidate_is_confirmed(candidate, term):
                                    continue
                                promoted_seeds += int(self.store.upsert_seed(
                                    candidate["term"],
                                    "tiktok_comments",
                                    source_url,
                                ))
                    for candidate in confirmed_hashtags:
                        promoted_seeds += int(self.store.upsert_seed(
                            candidate, "tiktok_hashtag",
                        ))
                    self.store.update_collector_run(
                        run_id, status="running", phase="tiktok_discovery",
                        current_term=term, steps_total=len(terms),
                        steps_done=term_index + 1,
                        seeds_found=len(unique_seeds) + promoted_seeds,
                        signals_created=created, signals_updated=updated,
                    )

            self._check_cancelled()
            self.store.update_collector_run(
                run_id,
                status="succeeded",
                phase="completed",
                current_term="",
                seeds_found=len(unique_seeds) + promoted_seeds,
                signals_created=created,
                signals_updated=updated,
                error="; ".join(errors),
            )
        except CollectorCancelled as exc:
            self.store.update_collector_run(
                run_id,
                status="cancelled",
                phase="cancelled",
                current_term="",
                signals_created=created,
                signals_updated=updated,
                error=str(exc),
            )
        except Exception as exc:
            logger.exception(f"automatic radar run {run_id} failed")
            self.store.update_collector_run(
                run_id,
                status="failed",
                phase="failed",
                current_term="",
                seeds_found=len(self.store.list_seeds(200)),
                signals_created=created,
                signals_updated=updated,
                error=str(exc),
            )
        finally:
            self._run_lock.release()
            with self._queue_lock:
                if self._active_run_id == run_id:
                    self._active_run_id = None

    def status(self) -> dict:
        latest = self.store.latest_collector_run()
        return {
            "enabled": self.config.enabled,
            "running": self._active_run_id is not None,
            "next_run_at": _iso(self._next_run_at) if self._next_run_at else None,
            "interval_seconds": self.config.interval_seconds,
            "providers": {
                "google_trends": {
                    "configured": True,
                    "geo": self.config.google_geo,
                },
                "telegram": {
                    "configured": bool(self.config.telegram_channels),
                    "channels": len(self.config.telegram_channels),
                },
                "tiktok": {
                    "configured": self.tiktok.configured,
                    "provider": "Bright Data",
                },
            },
            "latest_run": latest,
        }
