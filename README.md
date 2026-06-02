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

# Current Status

Version 1.0.0

Validated capabilities:

* Successful Castopod REST API integration
* Automated episode creation
* Automated episode publication
* Large-file uploads greater than 100 MB
* RSS feed generation and validation
* Internal Docker network communication
* Environment variable configuration

---

# Technology Stack

* Python
* Flask
* Requests
* Docker
* Docker Compose
* Castopod REST API

---

# Future Enhancements

Planned improvements include:

* Health check page
* Startup configuration validation
* Upload progress bar
* Draft vs Publish workflow
* Upload history
* Improved success pages
* Embedded operational documentation
* Castopod API recovery instructions
* Castopod administration and SEO review
* Exploration of Castopod 2.0 plugin capabilities

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

# License

Personal project developed for the Pacific Shift podcast ecosystem.
