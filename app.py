import os
import re
import tempfile

import requests
from flask import Flask, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from dashboard import build_dashboard_view_model
from navigation import get_navigation
from pipeline_logging import get_pipeline_logger
from pipeline_state import get_pipeline_store

app = Flask(__name__)

CASTOPOD_URL = os.getenv("CASTOPOD_URL")
API_USER = os.getenv("API_USER")
API_PASS = os.getenv("API_PASS")
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "pacific-shift.com")
PODCAST_ID = int(os.getenv("PODCAST_ID", "1"))
PODCAST_NAME = os.getenv("PODCAST_NAME", f"Podcast {PODCAST_ID}")
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


def get_podcast_options():
    return [{"id": PODCAST_ID, "name": PODCAST_NAME}]


def render_upload_template(**context):
    context.setdefault("page_title", "Manual Upload")
    context.setdefault(
        "page_description",
        "Operational fallback for publishing an episode directly to Castopod.",
    )
    context.setdefault("podcast_options", get_podcast_options())
    context.setdefault("default_podcast_id", PODCAST_ID)
    return render_template("index.html", **context)


def validate_upload(form, files):
    errors = []
    title = form.get("title", "").strip()
    description = form.get("description", "").strip()
    podcast_id_raw = form.get("podcast_id", str(PODCAST_ID)).strip()
    audio_file = files.get("audio_file")

    if not title:
        errors.append("Title is required.")

    if not description:
        errors.append("Description is required.")

    if not audio_file or not audio_file.filename:
        errors.append("MP3 file is required.")
    elif not audio_file.filename.lower().endswith(".mp3"):
        errors.append("Audio file must be an MP3.")

    try:
        podcast_id = int(podcast_id_raw)
    except ValueError:
        errors.append("Podcast selection is invalid.")
        podcast_id = PODCAST_ID

    allowed_podcast_ids = {option["id"] for option in get_podcast_options()}
    if podcast_id not in allowed_podcast_ids:
        errors.append("Podcast selection is invalid.")
        podcast_id = PODCAST_ID

    save_as_draft = form.get("save_as_draft") == "1"

    return errors, title, description, podcast_id, save_as_draft, audio_file


def check_config():
    config = {
        "CASTOPOD_URL": CASTOPOD_URL,
        "API_USER": API_USER,
        "API_PASS": API_PASS,
    }
    return [name for name, value in config.items() if not value]


@app.context_processor
def inject_navigation():
    return {"navigation_items": get_navigation()}


@app.route("/dashboard")
def dashboard():
    return render_template(
        "dashboard.html",
        page_title="Dashboard",
        page_description="Pipeline overview foundation for Pacific Shift publishing automation.",
        dashboard=build_dashboard_view_model(),
    )


@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/manual-upload")
def manual_upload():
    return render_upload_template()


@app.route("/runs")
def runs():
    return render_template(
        "placeholder.html",
        page_title="Runs",
        page_description="Pipeline run history will appear here in a future milestone.",
    )


@app.route("/logs")
def logs():
    return render_template(
        "placeholder.html",
        page_title="Logs",
        page_description="Pipeline logs will appear here in a future milestone.",
    )


@app.route("/settings")
def settings():
    return render_template(
        "placeholder.html",
        page_title="Settings",
        page_description="Publisher configuration controls will appear here in a future milestone.",
    )


@app.route("/healthz")
def healthz():
    missing = check_config()
    if missing:
        return {"status": "error", "missing": missing}, 500
    return {"status": "ok"}, 200


@app.route("/api/pipeline-runs/latest")
def latest_pipeline_run():
    run = get_pipeline_store().get_latest_run()
    if not run:
        return jsonify({"run": None}), 200
    return jsonify({"run": run}), 200


@app.route("/api/pipeline-events")
def pipeline_events():
    events = get_pipeline_logger().find_events(
        run_id=request.args.get("run_id"),
        session_id=request.args.get("session_id"),
        step_key=request.args.get("step_key"),
    )
    return jsonify({"events": events}), 200


@app.route("/upload", methods=["POST"])
def upload():
    missing_config = check_config()
    if missing_config:
        return render_upload_template(
            error=f"Publisher is missing required configuration: {', '.join(missing_config)}.",
            form=request.form,
        ), 500

    errors, title, description, podcast_id, save_as_draft, audio_file = validate_upload(
        request.form,
        request.files,
    )
    if errors:
        return render_upload_template(
            errors=errors,
            form=request.form,
        ), 400

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
                "podcast_id": podcast_id,
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
                return render_upload_template(
                    error="Castopod upload request failed.",
                    detail=str(exc),
                    form=request.form,
                ), 502
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    if response.status_code not in (200, 201):
        return render_upload_template(
            error="Castopod rejected the episode upload.",
            detail=response.text,
            form=request.form,
        ), response.status_code

    episode = response.json()
    episode_id = episode["id"]

    if save_as_draft:
        return render_upload_template(
            success=f"Episode {episode_id} uploaded and saved as draft.",
        )

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
        return render_upload_template(
            error="Episode was created, but the publish request failed.",
            detail=str(exc),
            form=request.form,
        ), 502

    if publish_response.status_code not in (200, 201):
        return render_upload_template(
            error="Episode was created, but Castopod did not publish it.",
            detail=publish_response.text,
            form=request.form,
        ), publish_response.status_code

    return render_upload_template(
        success=f"Episode {episode_id} uploaded and published.",
        publish_detail=publish_response.text,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
