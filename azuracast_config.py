import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pipeline_state import default_db_path, utc_now


AZURACAST_CONFIG_KEY = "azuracast"


@dataclass(frozen=True)
class AzuraCastConfig:
    enabled: bool = False
    base_url: str | None = None
    station_shortcode: str | None = None
    station_id: str | None = None
    streamer_id: str = "1"
    station_name: str | None = None
    nowplaying_url: str | None = None
    podcast_feed_url: str | None = None
    last_successful_check_at: str | None = None
    last_check_message: str | None = None
    api_key_configured: bool = False

    def as_dict(self):
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "station_shortcode": self.station_shortcode,
            "station_id": self.station_id,
            "streamer_id": self.streamer_id,
            "station_name": self.station_name,
            "nowplaying_url": self.nowplaying_url,
            "podcast_feed_url": self.podcast_feed_url,
            "last_successful_check_at": self.last_successful_check_at,
            "last_check_message": self.last_check_message,
            "api_key_configured": self.api_key_configured,
        }


class AzuraCastConfigStore:
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS integration_settings (
                    integration_key TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    base_url TEXT,
                    station_shortcode TEXT,
                    station_id TEXT,
                    streamer_id TEXT NOT NULL DEFAULT '1',
                    station_name TEXT,
                    nowplaying_url TEXT,
                    podcast_feed_url TEXT,
                    last_successful_check_at TEXT,
                    last_check_message TEXT,
                    api_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(integration_settings)").fetchall()
            }
            if "streamer_id" not in columns:
                connection.execute(
                    "ALTER TABLE integration_settings ADD COLUMN streamer_id TEXT NOT NULL DEFAULT '1'"
                )
            if "api_key" not in columns:
                connection.execute("ALTER TABLE integration_settings ADD COLUMN api_key TEXT")

    def get_config(self):
        self.initialize()
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM integration_settings
                WHERE integration_key = ?
                """,
                (AZURACAST_CONFIG_KEY,),
            ).fetchone()

        if not row:
            return config_from_environment()
        return config_from_row(row)

    def save_config(self, values):
        errors, normalized = validate_azuracast_config(values)
        if errors:
            return None, errors

        self.initialize()
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO integration_settings (
                    integration_key, enabled, base_url, station_shortcode, station_id, streamer_id,
                    station_name, nowplaying_url, podcast_feed_url,
                    last_successful_check_at, last_check_message, api_key, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                ON CONFLICT(integration_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    base_url = excluded.base_url,
                    station_shortcode = excluded.station_shortcode,
                    station_id = excluded.station_id,
                    streamer_id = excluded.streamer_id,
                    station_name = excluded.station_name,
                    nowplaying_url = excluded.nowplaying_url,
                    podcast_feed_url = excluded.podcast_feed_url,
                    api_key = COALESCE(excluded.api_key, integration_settings.api_key),
                    updated_at = excluded.updated_at
                """,
                (
                    AZURACAST_CONFIG_KEY,
                    1 if normalized["enabled"] else 0,
                    normalized["base_url"],
                    normalized["station_shortcode"],
                    normalized["station_id"],
                    normalized["streamer_id"],
                    normalized["station_name"],
                    normalized["nowplaying_url"],
                    normalized["podcast_feed_url"],
                    clean_text(values.get("api_key")),
                    now,
                    now,
                ),
            )
        return self.get_config(), []

    def get_api_key(self):
        self.initialize()
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT api_key FROM integration_settings WHERE integration_key = ?",
                (AZURACAST_CONFIG_KEY,),
            ).fetchone()
        saved_key = clean_text(row["api_key"]) if row else None
        return saved_key or clean_text(os.getenv("AZURACAST_API_KEY"))

    def clear_api_key(self):
        self.initialize()
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE integration_settings SET api_key = NULL, updated_at = ? WHERE integration_key = ?",
                (utc_now(), AZURACAST_CONFIG_KEY),
            )
        return self.get_config()

    def record_check_result(self, message, *, success=False):
        self.initialize()
        now = utc_now()
        with closing(self.connect()) as connection:
            result = connection.execute(
                """
                UPDATE integration_settings
                SET last_successful_check_at = CASE WHEN ? THEN ? ELSE last_successful_check_at END,
                    last_check_message = ?,
                    updated_at = ?
                WHERE integration_key = ?
                """,
                (1 if success else 0, now, message, now, AZURACAST_CONFIG_KEY),
            )
        if result.rowcount == 0:
            config = config_from_environment()
            self.save_config(config.as_dict())
            return self.record_check_result(message, success=success)
        return self.get_config()


def get_azuracast_config(store=None):
    store = store or AzuraCastConfigStore()
    return store.get_config()


def get_azuracast_api_key(store=None):
    store = store or AzuraCastConfigStore()
    return store.get_api_key()


def validate_azuracast_config(values):
    normalized = {
        "enabled": bool(values.get("enabled")),
        "base_url": clean_text(values.get("base_url")),
        "station_shortcode": clean_text(values.get("station_shortcode")),
        "station_id": clean_text(values.get("station_id")),
        "streamer_id": clean_text(values.get("streamer_id")) or "1",
        "station_name": clean_text(values.get("station_name")),
        "nowplaying_url": clean_text(values.get("nowplaying_url")),
        "podcast_feed_url": clean_text(values.get("podcast_feed_url")),
    }
    errors = []

    for field_name in ("base_url", "nowplaying_url", "podcast_feed_url"):
        value = normalized[field_name]
        if value and not is_http_url(value):
            errors.append(f"{field_label(field_name)} must start with http:// or https://.")

    if normalized["station_id"] and not normalized["station_id"].isdigit():
        errors.append("Station ID must be numeric.")

    if not normalized["streamer_id"].isdigit():
        errors.append("Streamer ID must be numeric.")

    if normalized["station_shortcode"] and not re.fullmatch(r"[A-Za-z0-9_-]+", normalized["station_shortcode"]):
        errors.append("Station shortcode may only contain letters, numbers, underscores, and hyphens.")

    if normalized["enabled"]:
        if not normalized["base_url"]:
            errors.append("Base URL is required when the AzuraCast integration is enabled.")
        if not (normalized["station_shortcode"] or normalized["station_id"]):
            errors.append("Station shortcode or station ID is required when the AzuraCast integration is enabled.")

    normalized["base_url"] = strip_trailing_slash(normalized["base_url"])
    return errors, normalized


def config_from_environment():
    return AzuraCastConfig(
        enabled=env_truthy("AZURACAST_ENABLED"),
        base_url=strip_trailing_slash(clean_text(os.getenv("AZURACAST_BASE_URL"))),
        station_shortcode=clean_text(os.getenv("AZURACAST_STATION_SHORTCODE")),
        station_id=clean_text(os.getenv("AZURACAST_STATION_ID")),
        streamer_id=clean_text(os.getenv("AZURACAST_STREAMER_ID")) or "1",
        station_name=clean_text(os.getenv("AZURACAST_STATION_NAME")),
        nowplaying_url=clean_text(os.getenv("AZURACAST_NOWPLAYING_URL")),
        podcast_feed_url=clean_text(os.getenv("AZURACAST_PODCAST_FEED_URL")),
        api_key_configured=bool(clean_text(os.getenv("AZURACAST_API_KEY"))),
    )


def config_from_row(row):
    return AzuraCastConfig(
        enabled=bool(row["enabled"]),
        base_url=row["base_url"],
        station_shortcode=row["station_shortcode"],
        station_id=row["station_id"],
        streamer_id=row["streamer_id"] or "1",
        station_name=row["station_name"],
        nowplaying_url=row["nowplaying_url"],
        podcast_feed_url=row["podcast_feed_url"],
        last_successful_check_at=row["last_successful_check_at"],
        last_check_message=row["last_check_message"],
        api_key_configured=bool(clean_text(row["api_key"]) or clean_text(os.getenv("AZURACAST_API_KEY"))),
    )


def clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def strip_trailing_slash(value):
    return value.rstrip("/") if value else value


def is_http_url(value):
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def env_truthy(name):
    return str(os.getenv(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def field_label(field_name):
    return field_name.replace("_", " ").title()
