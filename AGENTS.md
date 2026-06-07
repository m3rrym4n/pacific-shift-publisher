# AGENTS.md

## Pacific Shift Publisher

### Purpose

Pacific Shift Publisher provides a lightweight web interface for uploading podcast episodes directly into Castopod.

The primary reason this application exists is to bypass Cloudflare upload limitations for large podcast files by communicating directly with the Castopod REST API over the internal Docker network.

---

## Architecture

Current upload path:

Publisher Container
→ Docker Network
→ Castopod REST API
→ Castopod Media Storage
→ RSS Feed

Uploads MUST NOT be routed through Cloudflare.

Castopod remains the system of record for podcast episodes and media.

---

## Environment

Application: Pacific Shift Publisher

Runtime:

* Flask
* Gunicorn
* Docker

Target:

* Castopod REST API

Network:

* castopod_castopod-app

---

## Current State

Implemented:

* Episode upload
* Episode creation
* Publish immediately
* Save as draft
* Podcast selector
* Health endpoint (/healthz)
* GitHub repository
* GitHub SSH workflow
* Docker deployment

Current release tag:

v1.2.0

---

## Development Priorities

Near-term:

1. Upload Progress Bar
2. Dynamic podcast enumeration from Castopod API
3. Improved success page
4. Upload history / audit log

Medium-term:

5. AzuraCast webhook integration
6. Automated draft episode creation
7. Cover art workflow improvements

Long-term:

8. Media pipeline automation
9. Traktor history import
10. AI-assisted show notes and artwork generation

---

## Constraints

Do not:

* Introduce Cloudflare into the upload path.
* Hardcode secrets into source files.
* Store API credentials in Git.
* Replace Castopod as the source of truth.

Prefer:

* Environment variables
* Docker-native deployment
* Castopod API integration
* Backward-compatible changes

---

## Before Making Changes

When implementing a feature:

1. Review README roadmap.
2. Preserve existing upload workflow.
3. Keep Docker deployment functional.
4. Update documentation if behavior changes.
5. Commit related changes together as a feature slice.

Example feature slices:

* Health Check
* Podcast Workflow
* Upload UX
* AzuraCast Integration
* Media Pipeline

Avoid unrelated refactoring during feature work.
