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

# AzuraCast Webhook Session Tracking

Publisher accepts AzuraCast streamer lifecycle webhooks at:

```text
POST /api/webhooks/azuracast
```

Supported normalized events are `streamer_start` and `streamer_stop`. A start event creates or updates the current pipeline run and records `started_at`; a stop event updates the same run when it can be correlated by `session_id` or active station/streamer and records `ended_at`.

Publisher also accepts AzuraCast Now Playing webhook payloads wrapped under `np -> App\Entity\Api\NowPlaying\NowPlaying`. Non-live Now Playing payloads are accepted as a known no-op when no active run exists. Live Now Playing payloads create or update an active run using station, streamer, and `live.broadcast_start` when available; a later non-live Now Playing payload closes the matching active run.

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

# License

Personal project developed for the Pacific Shift podcast ecosystem.
