# AGENTS.md

## Pacific Shift Publisher

### Purpose

Pacific Shift Publisher provides a lightweight web interface and automation platform for publishing podcast and DJ mix content.

The project originated as a solution for bypassing Cloudflare upload limitations when publishing large podcast files into Castopod. The long-term vision is to automate podcast publishing workflows by orchestrating existing platforms rather than replacing them.

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

Application:

* Pacific Shift Publisher

Runtime:

* Flask
* Gunicorn
* Docker

Primary Integrations:

* Castopod REST API
* AzuraCast
* What's Now Playing (WNP)

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

Current Release:

* v1.2.0

---

## Product Philosophy

Pacific Shift Publisher is an orchestration layer.

Publisher should prefer integrating with existing platform capabilities rather than reimplementing them.

Preferred systems:

* Castopod for podcast publishing
* AzuraCast for recording and streaming
* What's Now Playing for metadata acquisition

Publisher coordinates workflows between systems.

Publisher should remain loosely coupled to downstream platforms whenever practical.

Avoid creating custom implementations when a supported platform already provides the capability.

---

## Roadmap

The roadmap is maintained in GitHub Milestones and Issues.

Before beginning work:

1. Identify the active milestone.
2. Review milestone issues.
3. Implement issues in milestone order.
4. Avoid skipping ahead to future milestones unless explicitly directed.

GitHub is the source of truth for roadmap planning.

---

## Feature Evaluation

New feature ideas should be captured as GitHub Issues.

Before implementing a feature, evaluate:

1. Does an existing platform already provide this capability?
2. Can Publisher orchestrate the capability instead of implementing it?
3. Does the feature reduce operator effort?
4. Does the feature fit an existing milestone?

Ideas are cheap. Roadmap changes require justification.

---

## Constraints

Do not:

* Introduce Cloudflare into the upload path.
* Hardcode secrets into source files.
* Store API credentials in Git.
* Replace Castopod as the source of truth.
* Couple Publisher tightly to any downstream platform.

Prefer:

* Environment variables
* Docker-native deployment
* Existing platform capabilities
* Castopod API integration
* Backward-compatible changes
* Reusable abstractions over platform-specific implementations

---

## Before Making Changes

When implementing a feature:

1. Review AGENTS.md.
2. Review the active GitHub milestone.
3. Review related GitHub issues.
4. Preserve existing workflows unless the issue explicitly changes them.
5. Keep Docker deployment functional.
6. Update documentation when behavior changes.
7. Commit related changes together as a feature slice.

Example feature slices:

* Podcast Workflow
* Upload UX
* Castopod Integration
* AzuraCast Automation
* Metadata Automation
* Distribution Automation
* Media Pipeline

Avoid unrelated refactoring during feature work.
