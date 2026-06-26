import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from pipeline_logging import StructuredPipelineLogger
from pipeline_state import default_db_path, utc_now


RSS_SOURCE_KEY = "azuracast_podcast"
RSS_SOURCE_RUN_ID = "rss-source"
DEFAULT_SOURCE_NAME = "AzuraCast Podcast RSS"
DEFAULT_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class RssSourceConfig:
    source_name: str = DEFAULT_SOURCE_NAME
    feed_url: str | None = None
    station_identifier: str | None = None
    podcast_identifier: str | None = None
    enabled: bool = False
    last_refresh_at: str | None = None
    last_refresh_status: str | None = None
    last_error_message: str | None = None
    latest_item_title: str | None = None
    latest_item_guid: str | None = None
    latest_item_pub_date: str | None = None
    latest_enclosure_url: str | None = None
    latest_enclosure_type: str | None = None
    latest_enclosure_length: str | None = None

    def as_dict(self):
        return {
            "source_name": self.source_name,
            "feed_url": self.feed_url,
            "station_identifier": self.station_identifier,
            "podcast_identifier": self.podcast_identifier,
            "enabled": self.enabled,
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_status": self.last_refresh_status,
            "last_error_message": self.last_error_message,
            "latest_item_title": self.latest_item_title,
            "latest_item_guid": self.latest_item_guid,
            "latest_item_pub_date": self.latest_item_pub_date,
            "latest_enclosure_url": self.latest_enclosure_url,
            "latest_enclosure_type": self.latest_enclosure_type,
            "latest_enclosure_length": self.latest_enclosure_length,
        }


@dataclass(frozen=True)
class RssFeedItem:
    item_id: str
    title: str | None = None
    pub_date: str | None = None
    guid: str | None = None
    enclosure_url: str | None = None
    enclosure_type: str | None = None
    enclosure_length: str | None = None

    def as_dict(self):
        return {
            "item_id": self.item_id,
            "title": self.title,
            "pub_date": self.pub_date,
            "guid": self.guid,
            "enclosure_url": self.enclosure_url,
            "enclosure_type": self.enclosure_type,
            "enclosure_length": self.enclosure_length,
        }


class RssSourceStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or default_db_path()

    def connect(self):
        path = Path(self.db_path)
        if path.parent and str(path.parent) != ".":
            path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with closing(self.connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rss_source_config (
                    source_key TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    feed_url TEXT,
                    station_identifier TEXT,
                    podcast_identifier TEXT,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    last_refresh_at TEXT,
                    last_refresh_status TEXT,
                    last_error_message TEXT,
                    latest_item_title TEXT,
                    latest_item_guid TEXT,
                    latest_item_pub_date TEXT,
                    latest_enclosure_url TEXT,
                    latest_enclosure_type TEXT,
                    latest_enclosure_length TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rss_source_items (
                    source_key TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    title TEXT,
                    pub_date TEXT,
                    guid TEXT,
                    enclosure_url TEXT,
                    enclosure_type TEXT,
                    enclosure_length TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source_key, item_id)
                );
                """
            )

    def get_config(self):
        self.initialize()
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM rss_source_config
                WHERE source_key = ?
                """,
                (RSS_SOURCE_KEY,),
            ).fetchone()
        return config_from_row(row) if row else RssSourceConfig()

    def save_config(self, values):
        errors, normalized = validate_rss_source_config(values)
        if errors:
            return None, errors

        self.initialize()
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO rss_source_config (
                    source_key, source_name, feed_url, station_identifier,
                    podcast_identifier, enabled, last_refresh_at,
                    last_refresh_status, last_error_message, latest_item_title,
                    latest_item_guid, latest_item_pub_date, latest_enclosure_url,
                    latest_enclosure_type, latest_enclosure_length, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    source_name = excluded.source_name,
                    feed_url = excluded.feed_url,
                    station_identifier = excluded.station_identifier,
                    podcast_identifier = excluded.podcast_identifier,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    RSS_SOURCE_KEY,
                    normalized["source_name"],
                    normalized["feed_url"],
                    normalized["station_identifier"],
                    normalized["podcast_identifier"],
                    1 if normalized["enabled"] else 0,
                    now,
                    now,
                ),
            )
        return self.get_config(), []

    def update_refresh_state(self, *, status, error_message=None, latest_item=None):
        self.initialize()
        now = utc_now()
        latest = latest_item.as_dict() if latest_item else {}
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO rss_source_config (
                    source_key, source_name, enabled, last_refresh_at,
                    last_refresh_status, last_error_message, latest_item_title,
                    latest_item_guid, latest_item_pub_date, latest_enclosure_url,
                    latest_enclosure_type, latest_enclosure_length, created_at, updated_at
                )
                VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    last_refresh_at = excluded.last_refresh_at,
                    last_refresh_status = excluded.last_refresh_status,
                    last_error_message = excluded.last_error_message,
                    latest_item_title = excluded.latest_item_title,
                    latest_item_guid = excluded.latest_item_guid,
                    latest_item_pub_date = excluded.latest_item_pub_date,
                    latest_enclosure_url = excluded.latest_enclosure_url,
                    latest_enclosure_type = excluded.latest_enclosure_type,
                    latest_enclosure_length = excluded.latest_enclosure_length,
                    updated_at = excluded.updated_at
                """,
                (
                    RSS_SOURCE_KEY,
                    DEFAULT_SOURCE_NAME,
                    now,
                    status,
                    error_message,
                    latest.get("title"),
                    latest.get("guid") or latest.get("item_id"),
                    latest.get("pub_date"),
                    latest.get("enclosure_url"),
                    latest.get("enclosure_type"),
                    latest.get("enclosure_length"),
                    now,
                    now,
                ),
            )
        return self.get_config()

    def replace_items(self, items):
        self.initialize()
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM rss_source_items WHERE source_key = ?", (RSS_SOURCE_KEY,))
            for item in items:
                connection.execute(
                    """
                    INSERT INTO rss_source_items (
                        source_key, item_id, title, pub_date, guid, enclosure_url,
                        enclosure_type, enclosure_length, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        RSS_SOURCE_KEY,
                        item.item_id,
                        item.title,
                        item.pub_date,
                        item.guid,
                        item.enclosure_url,
                        item.enclosure_type,
                        item.enclosure_length,
                        now,
                        now,
                    ),
                )

    def list_items(self, limit=10):
        self.initialize()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM rss_source_items
                WHERE source_key = ?
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (RSS_SOURCE_KEY, limit),
            ).fetchall()
        return [item_from_row(row).as_dict() for row in rows]


def validate_rss_source_config(values):
    normalized = {
        "source_name": clean_text(values.get("source_name")) or DEFAULT_SOURCE_NAME,
        "feed_url": clean_text(values.get("feed_url")),
        "station_identifier": clean_text(values.get("station_identifier")),
        "podcast_identifier": clean_text(values.get("podcast_identifier")),
        "enabled": bool(values.get("enabled")),
    }
    errors = []
    if normalized["feed_url"] and not is_http_url(normalized["feed_url"]):
        errors.append("RSS feed URL must start with http:// or https://.")
    if normalized["enabled"] and not normalized["feed_url"]:
        errors.append("RSS feed URL is required when the source is enabled.")
    return errors, normalized


def refresh_rss_source(store=None, http_get=None, event_store=None):
    store = store or RssSourceStore()
    event_store = event_store or StructuredPipelineLogger()
    config = store.get_config()

    if not config.enabled:
        message = "RSS source refresh skipped: source is disabled."
        config = store.update_refresh_state(status="skipped", error_message=message)
        _emit_refresh_event(event_store, config, "rss_source.refresh_skipped", "skipped", message)
        return {"ok": False, "status": "skipped", "message": message, "config": config, "items": []}

    if not config.feed_url:
        message = "RSS source refresh skipped: feed URL is not configured."
        config = store.update_refresh_state(status="skipped", error_message=message)
        _emit_refresh_event(event_store, config, "rss_source.refresh_skipped", "skipped", message)
        return {"ok": False, "status": "skipped", "message": message, "config": config, "items": []}

    http_get = http_get or requests.get
    try:
        response = http_get(config.feed_url, timeout=DEFAULT_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:
        message = f"RSS source refresh failed: {exc.__class__.__name__}"
        config = store.update_refresh_state(status="failed", error_message=message)
        _emit_refresh_event(event_store, config, "rss_source.refresh_failed", "failed", message)
        return {"ok": False, "status": "failed", "message": message, "config": config, "items": []}

    try:
        items = parse_rss_feed(response.text)
    except ValueError as exc:
        message = f"RSS source refresh failed: {exc}"
        config = store.update_refresh_state(status="failed", error_message=message)
        _emit_refresh_event(event_store, config, "rss_source.refresh_failed", "failed", message)
        return {"ok": False, "status": "failed", "message": message, "config": config, "items": []}

    store.replace_items(items)
    latest_item = items[0] if items else None
    config = store.update_refresh_state(status="success", error_message=None, latest_item=latest_item)
    message = f"RSS source refresh succeeded with {len(items)} item{'s' if len(items) != 1 else ''}."
    _emit_refresh_event(
        event_store,
        config,
        "rss_source.refresh_succeeded",
        "success",
        message,
        details={
            "item_count": len(items),
            "latest_item_title": latest_item.title if latest_item else None,
            "latest_enclosure_url": latest_item.enclosure_url if latest_item else None,
            "latest_enclosure_type": latest_item.enclosure_type if latest_item else None,
            "latest_enclosure_length": latest_item.enclosure_length if latest_item else None,
        },
    )
    return {"ok": True, "status": "success", "message": message, "config": config, "items": [item.as_dict() for item in items]}


def parse_rss_feed(feed_xml):
    try:
        root = ElementTree.fromstring(feed_xml)
    except ElementTree.ParseError as exc:
        raise ValueError("feed XML could not be parsed") from exc

    items = [element for element in root.iter() if local_name(element.tag) in {"item", "entry"}]
    return [item for item in (parse_feed_item(element, index) for index, element in enumerate(items, start=1)) if item]


def parse_feed_item(element, index):
    title = child_text(element, "title")
    pub_date = child_text(element, "pubDate") or child_text(element, "published") or child_text(element, "updated")
    guid = child_text(element, "guid") or child_text(element, "id")
    link = child_text(element, "link") or child_link_href(element)
    enclosure = first_child(element, "enclosure")
    enclosure_url = enclosure.get("url") if enclosure is not None else None
    enclosure_type = enclosure.get("type") if enclosure is not None else None
    enclosure_length = enclosure.get("length") if enclosure is not None else None
    item_id = clean_text(guid) or clean_text(link) or clean_text(enclosure_url) or f"item-{index}"

    return RssFeedItem(
        item_id=item_id,
        title=clean_text(title),
        pub_date=clean_text(pub_date),
        guid=clean_text(guid),
        enclosure_url=clean_text(enclosure_url),
        enclosure_type=clean_text(enclosure_type),
        enclosure_length=clean_text(enclosure_length),
    )


def child_text(element, wanted_name):
    child = first_child(element, wanted_name)
    if child is None or child.text is None:
        return None
    return child.text.strip() or None


def first_child(element, wanted_name):
    for child in list(element):
        if local_name(child.tag) == wanted_name:
            return child
    return None


def child_link_href(element):
    for child in list(element):
        if local_name(child.tag) == "link" and child.get("href"):
            return child.get("href")
    return None


def local_name(tag):
    return str(tag).split("}", 1)[-1]


def _emit_refresh_event(event_store, config, event_name, status, message, details=None):
    event_details = {
        "source_name": config.source_name,
        "feed_url": config.feed_url,
        "station_identifier": config.station_identifier,
        "podcast_identifier": config.podcast_identifier,
        "last_refresh_status": config.last_refresh_status,
    }
    event_details.update(details or {})
    return event_store.emit(
        run_id=RSS_SOURCE_RUN_ID,
        session_id=config.station_identifier,
        step_key="acquire_mp3",
        event_name=event_name,
        status=status,
        message=message,
        details=event_details,
        level="ERROR" if status == "failed" else "INFO",
    )


def config_from_row(row):
    return RssSourceConfig(
        source_name=row["source_name"],
        feed_url=row["feed_url"],
        station_identifier=row["station_identifier"],
        podcast_identifier=row["podcast_identifier"],
        enabled=bool(row["enabled"]),
        last_refresh_at=row["last_refresh_at"],
        last_refresh_status=row["last_refresh_status"],
        last_error_message=row["last_error_message"],
        latest_item_title=row["latest_item_title"],
        latest_item_guid=row["latest_item_guid"],
        latest_item_pub_date=row["latest_item_pub_date"],
        latest_enclosure_url=row["latest_enclosure_url"],
        latest_enclosure_type=row["latest_enclosure_type"],
        latest_enclosure_length=row["latest_enclosure_length"],
    )


def item_from_row(row):
    return RssFeedItem(
        item_id=row["item_id"],
        title=row["title"],
        pub_date=row["pub_date"],
        guid=row["guid"],
        enclosure_url=row["enclosure_url"],
        enclosure_type=row["enclosure_type"],
        enclosure_length=row["enclosure_length"],
    )


def clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def is_http_url(value):
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
