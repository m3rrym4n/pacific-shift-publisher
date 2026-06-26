# Pacific Shift Publisher

Pacific Shift Publisher is a lightweight web application designed to upload and publish podcast episodes directly to Castopod using its REST API.

The project was created to solve a specific problem encountered while publishing Storm Surge, a drum & bass podcast hosted on Castopod. Because Cloudflare imposes upload size limits on proxied web traffic, large podcast episodes and DJ mixes exceeding 100 MB could not be uploaded through the standard Castopod web interface.

Pacific Shift Publisher bypasses this limitation by communicating directly with Castopod over the internal Docker network. Audio files are uploaded directly to the Castopod REST API, allowing reliable publication of large episodes without exposing internal services to the public internet.

---

# Screenshot

<img width="513" height="527" alt="image" src="https://github.com/user-attachments/assets/942dab7b-cd75-4e61-837d-13b64a5adc7e" />


---

# Features

* Upload large audio files directly to Castopod
* Bypass Cloudflare upload size limitations
* Automatically create podcast episodes
* Automatically publish episodes after upload
* Lightweight Flask-based web interface
* Docker container deployment
* Environment-variable based configuration
* Git version controlled
* Works entirely within the internal Docker network

---

# Architecture

```text
Browser
    ↓
Pacific Shift Publisher
    ↓
Castopod REST API
    ↓
Castopod Media Storage
    ↓
RSS Feed
```

Unlike the standard Castopod web interface, uploads never pass through Cloudflare. The Publisher communicates directly with Castopod on the internal Docker network while still preserving the public-facing podcast URLs and RSS feed.

---

# Why This Exists

Storm Surge episodes are frequently one to two hours long and can easily exceed Cloudflare's upload limits.

This application provides a dedicated publishing workflow that:

* Preserves the existing Castopod installation
* Preserves public RSS feeds
* Preserves podcast URLs
* Supports large file uploads
* Automates episode creation and publication

---

# Quick Start

## Clone Repository

```bash
git clone https://github.com/m3rrym4n/pacific-shift-publisher.git
cd pacific-shift-publisher
```

## Configure Environment

Create a `.env` file:

```text
API_USER=automation
API_PASS=CHANGE_ME
CASTOPOD_URL=http://castopod:8080
```

## Build and Start

```bash
docker compose up -d --build
```

## Open the Publisher

```text
http://SERVER_IP:5000
```

Upload an MP3 file, enter episode metadata, and publish directly to Castopod.

---

# Technology Stack

* Python
* Flask
* Requests
* Docker
* Docker Compose
* Castopod REST API

---

# Security

Secrets are stored using environment variables and are intentionally excluded from version control.

Sensitive files such as local configuration and recovery scripts should never be committed to the repository.

Examples:

```text
.env
scripts/enable-restapi.sh
```

Template versions of sensitive scripts should be used for documentation and distribution.

---

# UI Shell Notes

Publisher uses a server-rendered Tabler shell. Tabler assets are vendored locally under `static/vendor/tabler/1.4.0/`; production templates must load those files through Flask static routes rather than a CDN.

Shared layout files live under `templates/layouts/` and `templates/partials/`. Navigation is centralized in `navigation.py`; add new shell links there instead of duplicating sidebar markup in page templates.

Dashboard is the primary/default operating view and is available at `/dashboard`; `/` redirects there. Runs and Logs are represented as future pipeline operations surfaces, and Settings is represented for future configuration work.

Manual Upload remains the operational fallback workflow. It is reachable at `/manual-upload` and keeps the existing Castopod upload behavior.

Publisher remains a separate bounded application from CrateSpy. The projects may share visual conventions, but Publisher navigation should stay focused on publishing automation.

---

# AzuraCast Integration Settings

AzuraCast integration settings are available under:

```text
GET /settings
POST /settings/azuracast
```

Publisher stores non-secret AzuraCast settings in the same SQLite state database used for pipeline state. Future automation code should load settings through `get_azuracast_config()` in `azuracast_config.py` instead of hardcoding deployment-specific instance details.

Supported non-secret fields are:

```text
enabled
base_url
station_shortcode
station_id
station_name
nowplaying_url
podcast_feed_url
last_successful_check_at
last_check_message
```

The AzuraCast API key is read from `AZURACAST_API_KEY`. The Settings UI only shows whether the key is configured; it never displays the full token.

Optional environment defaults:

```text
AZURACAST_ENABLED
AZURACAST_BASE_URL
AZURACAST_STATION_SHORTCODE
AZURACAST_STATION_ID
AZURACAST_STATION_NAME
AZURACAST_NOWPLAYING_URL
AZURACAST_PODCAST_FEED_URL
AZURACAST_API_KEY
```

This settings page does not call AzuraCast, test credentials, fetch track history, read RSS feeds, acquire audio, or create Castopod drafts.

---

# AzuraCast Webhook Session Tracking

Publisher accepts AzuraCast streamer lifecycle webhooks at:

```text
POST /api/webhooks/azuracast
```

Supported normalized events are `streamer_start` and `streamer_stop`. A start event creates or updates the current pipeline run and records `started_at`; a stop event updates the same run when it can be correlated by `session_id` or active station/streamer and records `ended_at`.

Publisher also accepts AzuraCast Now Playing webhook payloads wrapped under `np -> App\Entity\Api\NowPlaying\NowPlaying`. Non-live Now Playing payloads are accepted as a known no-op when no active run exists. Live Now Playing payloads create or update an active run using station, streamer, and `live.broadcast_start` when available; a later non-live Now Playing payload closes the matching active run.

Every AzuraCast webhook request emits a sanitized diagnostic event named `azuracast_webhook_diagnostics`. Inspect recent diagnostics with:

```text
GET /api/pipeline-events?event_name=azuracast_webhook_diagnostics
```

Diagnostics include request shape fields such as content type, content length, JSON parse method, form keys, top-level keys and value types, `np` presence/type/keys, candidate Now Playing paths, station fields, streamer-presence flags, parser decision, and parser reason. They intentionally do not store raw request bodies, authorization headers, cookies, token values, or full song history payloads.

Example start payload:

```json
{
  "event": "streamer_start",
  "station": "Storm Surge",
  "streamer": "SeaCapn",
  "timestamp": "2026-06-24T22:00:00Z",
  "session_id": "storm-surge-20260624"
}
```

Example stop payload:

```json
{
  "event": "streamer_stop",
  "station": "Storm Surge",
  "streamer": "SeaCapn",
  "timestamp": "2026-06-24T23:00:00Z",
  "session_id": "storm-surge-20260624"
}
```

The completed session window is available through the pipeline run state as `run_id`, `session_id`, `station`, `streamer`, `started_at`, and `ended_at`. This endpoint records state only; it does not call AzuraCast, retrieve recordings or tracklists, or create Castopod episodes.

---

# AzuraCast Session Tracklists

Publisher can prepare a simple Castopod description tracklist from AzuraCast Now Playing/static history for a completed pipeline run.

Tracklist generation is config-driven. Code should call `generate_tracklist_for_run()` in `azuracast_history.py`; it loads AzuraCast settings through `get_azuracast_config()` and never hardcodes local station URLs, station IDs, or LAN addresses.

Endpoint resolution uses:

```text
1. configured nowplaying_url override, when present
2. base_url + /api/nowplaying_static/{station_shortcode}.json
3. base_url + /api/nowplaying/{station_id}
```

The generated tracklist reads `song_history`, filters tracks inclusively between the run `started_at` and `ended_at` timestamps, sorts tracks by `played_at` ascending, and formats:

```text
Tracklist

01. Artist - Title
02. Artist - Title
```

Use `prepare_description_with_tracklist()` to append the generated tracklist to an existing description. If a description already contains a `Tracklist` section, the helper replaces that section instead of appending a duplicate.

This helper prepares description content only. It does not fetch recordings, assemble podcast episodes, create Castopod drafts, publish episodes, use WNP, or read the AzuraCast database directly.

---

# AzuraCast Podcast RSS Source

Publisher stores a configured AzuraCast podcast RSS source at `/settings/source`. This is the source issue #30 should use to locate the published AzuraCast podcast enclosure before acquiring audio for a Castopod draft.

The source configuration is persisted in the Publisher state database and supports:

```text
source name
RSS feed URL
station/show identifier
optional AzuraCast podcast ID or slug
enabled/disabled state
last refresh timestamp/status/error
latest parsed item and enclosure metadata
```

Use the `/settings/source` page to save the feed URL and trigger a manual refresh. Refreshing fetches the configured RSS feed, parses item title, publication date, GUID/stable ID, enclosure URL, enclosure type, and enclosure length, then stores recent parsed items for later pipeline use. Refresh success/failure is logged as structured pipeline events under the `acquire_mp3` step with event names such as `rss_source.refresh_succeeded` and `rss_source.refresh_failed`.

This source setup does not download MP3 files, call Castopod, perform AzuraCast API readiness checks, or create podcast drafts. Those behaviors remain future pipeline work.

---

# Live Pipeline Verification Logs

The `/logs` page renders recent structured pipeline events in a single tail-style viewer for operator verification. It reads the same event store exposed by `GET /api/pipeline-events` and keeps that JSON API available.

Use `/logs` during a live test to confirm:

```text
1. AzuraCast webhook diagnostics were received.
2. Stream start and stream end events were recorded.
3. Tracklist acquisition events appear when that step is wired.
4. Events belong to the expected run_id or session_id.
5. Failure, skipped, or success messages explain the outcome.
```

Supported query-string filters:

```text
/logs?run_id=...
/logs?session_id=...
/logs?step_key=stream_start
/logs?event_name=azuracast_webhook_diagnostics
```

Detail modes:

```text
/logs
/logs?detail_mode=safe
/logs?detail_mode=verbose
/logs?detail_mode=raw
```

Safe mode is the default and shows a compact whitelist of useful detail fields such as station, streamer, parser decision/reason, history URL used, and track counts. Verbose mode shows the stored structured `details` object as formatted JSON with normal redaction. Raw Debug mode is selectable from the UI and query string for local troubleshooting, and still hard-redacts obvious secret fields such as authorization, cookies, API keys, tokens, passwords, bearer values, and secrets.

Raw Debug can only show details that were stored with the event. If an earlier structured event was already sanitized before storage, Raw Debug cannot recover removed values.

Events are shown oldest-to-newest so the newest line appears at the bottom like a live log tail. The page includes a local auto-refresh toggle, a manual refresh button, and a Copy visible logs button for pasting current evidence into an issue or support thread.

---

# License

Personal project developed for the Pacific Shift podcast ecosystem.
