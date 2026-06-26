import os
import re

import requests


def make_slug(title):
    slug = re.sub(r"[^a-z0-9]+", "-", str(title or "").lower()).strip("-")
    return slug or "episode"


def castopod_config_from_environment():
    return {
        "castopod_url": os.getenv("CASTOPOD_URL"),
        "api_user": os.getenv("API_USER"),
        "api_pass": os.getenv("API_PASS"),
        "public_host": os.getenv("PUBLIC_HOST", "pacific-shift.com"),
        "podcast_id": int(os.getenv("PODCAST_ID", "1")),
        "created_by": int(os.getenv("CREATED_BY", "1")),
        "updated_by": int(os.getenv("UPDATED_BY", os.getenv("CREATED_BY", "1"))),
        "request_timeout": int(os.getenv("REQUEST_TIMEOUT", "3600")),
    }


def missing_castopod_config(config=None):
    config = config or castopod_config_from_environment()
    required = {
        "CASTOPOD_URL": config.get("castopod_url"),
        "API_USER": config.get("api_user"),
        "API_PASS": config.get("api_pass"),
    }
    return [name for name, value in required.items() if not value]


def create_castopod_draft_episode(
    *,
    audio_path,
    filename,
    title,
    description,
    podcast_id=None,
    http_post=None,
    config=None,
):
    config = config or castopod_config_from_environment()
    missing = missing_castopod_config(config)
    if missing:
        return {
            "ok": False,
            "error": f"Publisher is missing required configuration: {', '.join(missing)}.",
        }

    podcast_id = int(podcast_id or config["podcast_id"])
    http_post = http_post or requests.post
    headers = {
        "Host": config["public_host"],
        "X-Forwarded-Proto": "https",
    }
    data = {
        "created_by": config["created_by"],
        "updated_by": config["updated_by"],
        "podcast_id": podcast_id,
        "title": title,
        "slug": make_slug(title),
        "description": description,
        "type": "full",
    }

    try:
        with open(audio_path, "rb") as audio_file:
            files = {
                "audio_file": (filename, audio_file, "audio/mpeg")
            }
            response = http_post(
                f"{config['castopod_url'].rstrip('/')}/api/rest/v1/episodes",
                auth=(config["api_user"], config["api_pass"]),
                headers=headers,
                files=files,
                data=data,
                timeout=config["request_timeout"],
            )
    except requests.RequestException as exc:
        return {
            "ok": False,
            "error": "Castopod upload request failed.",
            "detail": str(exc),
        }
    except OSError as exc:
        return {
            "ok": False,
            "error": "Audio file could not be read.",
            "detail": str(exc),
        }

    if response.status_code not in (200, 201):
        return {
            "ok": False,
            "error": "Castopod rejected the episode upload.",
            "status_code": response.status_code,
            "detail": response.text,
        }

    try:
        episode = response.json()
    except ValueError:
        episode = {}
    return {
        "ok": True,
        "episode_id": episode.get("id"),
        "episode_url": episode.get("url") or episode.get("link"),
        "status_code": response.status_code,
    }
