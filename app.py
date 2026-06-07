import os
import re
import tempfile

import requests
from flask import Flask, render_template, request
from werkzeug.utils import secure_filename

app = Flask(__name__)

CASTOPOD_URL = os.getenv("CASTOPOD_URL")
API_USER = os.getenv("API_USER")
API_PASS = os.getenv("API_PASS")
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "pacific-shift.com")
PODCAST_ID = int(os.getenv("PODCAST_ID", "1"))
CREATED_BY = int(os.getenv("CREATED_BY", "1"))
UPDATED_BY = int(os.getenv("UPDATED_BY", str(CREATED_BY)))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "3600"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1024"))

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

HEADERS = {
    "Host": PUBLIC_HOST,
    "X-Forwarded-Proto": "https"
}


def make_slug(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "episode"


def validate_upload(form, files):
    errors = []
    title = form.get("title", "").strip()
    description = form.get("description", "").strip()
    audio_file = files.get("audio_file")

    if not title:
        errors.append("Title is required.")

    if not description:
        errors.append("Description is required.")

    if not audio_file or not audio_file.filename:
        errors.append("MP3 file is required.")
    elif not audio_file.filename.lower().endswith(".mp3"):
        errors.append("Audio file must be an MP3.")

    return errors, title, description, audio_file


def check_config():
    config = {
        "CASTOPOD_URL": CASTOPOD_URL,
        "API_USER": API_USER,
        "API_PASS": API_PASS,
    }
    return [name for name, value in config.items() if not value]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def healthz():
    missing = check_config()
    if missing:
        return {"status": "error", "missing": missing}, 500
    return {"status": "ok"}, 200


@app.route("/upload", methods=["POST"])
def upload():
    missing_config = check_config()
    if missing_config:
        return render_template(
            "index.html",
            error=f"Publisher is missing required configuration: {', '.join(missing_config)}.",
            form=request.form,
        ), 500

    errors, title, description, audio_file = validate_upload(request.form, request.files)
    if errors:
        return render_template("index.html", errors=errors, form=request.form), 400

    filename = secure_filename(audio_file.filename)
    suffix = os.path.splitext(filename)[1] or ".mp3"
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_path = temp_file.name
            audio_file.save(temp_file)

        with open(temp_path, "rb") as f:
            files = {
                "audio_file": (filename, f, "audio/mpeg")
            }
            data = {
                "created_by": CREATED_BY,
                "updated_by": UPDATED_BY,
                "podcast_id": PODCAST_ID,
                "title": title,
                "slug": make_slug(title),
                "description": description,
                "type": "full"
            }
            try:
                response = requests.post(
                    f"{CASTOPOD_URL}/api/rest/v1/episodes",
                    auth=(API_USER, API_PASS),
                    headers=HEADERS,
                    files=files,
                    data=data,
                    timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                return render_template(
                    "index.html",
                    error="Castopod upload request failed.",
                    detail=str(exc),
                    form=request.form,
                ), 502
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    if response.status_code not in (200, 201):
        return render_template(
            "index.html",
            error="Castopod rejected the episode upload.",
            detail=response.text,
            form=request.form,
        ), response.status_code

    episode = response.json()
    episode_id = episode["id"]

    try:
        publish_response = requests.post(
            f"{CASTOPOD_URL}/api/rest/v1/episodes/{episode_id}/publish",
            auth=(API_USER, API_PASS),
            headers=HEADERS,
            data={
                "publication_method": "now",
                "created_by": CREATED_BY
            },
            timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as exc:
        return render_template(
            "index.html",
            error="Episode was created, but the publish request failed.",
            detail=str(exc),
            form=request.form,
        ), 502

    if publish_response.status_code not in (200, 201):
        return render_template(
            "index.html",
            error="Episode was created, but Castopod did not publish it.",
            detail=publish_response.text,
            form=request.form,
        ), publish_response.status_code

    return render_template(
        "index.html",
        success=f"Episode {episode_id} uploaded and published.",
        publish_detail=publish_response.text,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
