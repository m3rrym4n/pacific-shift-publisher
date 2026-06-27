import os
import tempfile

import requests
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from azuracast_config import AzuraCastConfigStore
from azuracast_connection import test_azuracast_connection
from azuracast_webhook import (
    emit_webhook_diagnostics,
    handle_azuracast_webhook,
    parse_azuracast_request,
)
from castopod_client import create_castopod_draft_episode, missing_castopod_config
from dashboard import build_dashboard_view_model
from logs_view import build_logs_download, build_logs_view_model, logs_download_filename
from navigation import get_navigation
from pipeline_logging import get_pipeline_logger
from pipeline_run_snapshot import (
    SnapshotImportError,
    export_run_snapshot,
    import_run_snapshot,
    load_snapshot_file,
    snapshot_filename,
)
from pipeline_retry import retry_pipeline_run
from pipeline_state import get_pipeline_store
from rss_source import RssSourceStore, refresh_rss_source
from runs_view import build_recent_runs_view_model
from tracklist_detail import build_tracklist_detail_view_model

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "publisher-dev-secret")

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
    return missing_castopod_config(
        {
            "castopod_url": CASTOPOD_URL,
            "api_user": API_USER,
            "api_pass": API_PASS,
        }
    )


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
        "runs.html",
        page_title="Runs",
        page_description="Recent pipeline automation attempts and step outcomes.",
        runs=build_recent_runs_view_model(),
    )


@app.route("/runs/<run_id>/export")
def export_run(run_id):
    snapshot = export_run_snapshot(run_id, get_pipeline_store(), get_pipeline_logger())
    if not snapshot:
        return jsonify({"ok": False, "message": "Pipeline run was not found."}), 404

    body = jsonify(snapshot)
    body.headers["Content-Disposition"] = f'attachment; filename="{snapshot_filename(run_id)}"'
    return body


@app.route("/runs/import", methods=["POST"])
def import_run():
    upload_file = request.files.get("snapshot_file")
    if not upload_file or not upload_file.filename:
        flash("Choose a pipeline run snapshot JSON file to import.", "warning")
        return redirect(url_for("runs"), code=303)

    try:
        snapshot = load_snapshot_file(upload_file)
        run = import_run_snapshot(snapshot, get_pipeline_store())
    except SnapshotImportError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("runs"), code=303)

    flash(f"Imported pipeline run {run['run_id'][:8]}.", "success")
    return redirect(url_for("runs"), code=303)


@app.route("/runs/current/cancel", methods=["POST"])
def cancel_current_run():
    get_pipeline_store().cancel_current_run()
    return redirect(url_for("dashboard"), code=303)


@app.route("/runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id):
    get_pipeline_store().cancel_run(run_id)
    return redirect(url_for("runs"), code=303)


@app.route("/runs/<run_id>/retry", methods=["POST"])
def retry_run(run_id):
    result = retry_pipeline_run(run_id, get_pipeline_store())
    flash(result["message"], "success" if result.get("ok") else "warning")
    return redirect(url_for("runs"), code=303)


@app.route("/runs/<run_id>/delete", methods=["POST"])
def delete_run(run_id):
    deleted = get_pipeline_store().delete_run(run_id)
    if deleted:
        flash(f"Deleted pipeline run {run_id[:8]}.", "success")
    else:
        flash("Pipeline run was not found.", "warning")
    return redirect(url_for("runs"), code=303)


@app.route("/runs/<run_id>/tracklist")
def run_tracklist(run_id):
    view_model = build_tracklist_detail_view_model(run_id)
    status_code = 200 if view_model["found"] else 404
    return render_template(
        "tracklist_detail.html",
        page_title="Tracklist Detail",
        page_description="Acquired AzuraCast tracklist for a pipeline run.",
        tracklist_detail=view_model,
    ), status_code


@app.route("/logs")
def logs():
    return render_template(
        "logs.html",
        page_title="Logs",
        page_description="Recent structured Publisher pipeline events.",
        logs=build_logs_view_model(request.args),
    )


@app.route("/logs/download")
def download_logs():
    body = build_logs_download(request.args)
    response = app.response_class(body, mimetype="text/plain; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="{logs_download_filename()}"'
    return response


@app.route("/settings")
def settings():
    return render_template(
        "settings.html",
        page_title="Settings",
        page_description="Publisher configuration for integration endpoints and future automation.",
        azuracast_config=AzuraCastConfigStore().get_config(),
    )


@app.route("/settings/azuracast", methods=["POST"])
def save_azuracast_settings():
    config, errors = AzuraCastConfigStore().save_config(
        {
            "enabled": request.form.get("enabled") == "1",
            "base_url": request.form.get("base_url"),
            "station_shortcode": request.form.get("station_shortcode"),
            "station_id": request.form.get("station_id"),
            "streamer_id": request.form.get("streamer_id"),
            "transcode_poll_interval_minutes": request.form.get(
                "transcode_poll_interval_minutes"
            ),
            "station_name": request.form.get("station_name"),
            "nowplaying_url": request.form.get("nowplaying_url"),
            "podcast_feed_url": request.form.get("podcast_feed_url"),
            "api_key": request.form.get("api_key"),
        }
    )
    status_code = 400 if errors else 200
    return render_template(
        "settings.html",
        page_title="Settings",
        page_description="Publisher configuration for integration endpoints and future automation.",
        azuracast_config=config or AzuraCastConfigStore().get_config(),
        azuracast_form=request.form,
        azuracast_errors=errors,
        azuracast_saved=not errors,
    ), status_code


@app.route("/settings/azuracast/api-key/clear", methods=["POST"])
def clear_azuracast_api_key():
    store = AzuraCastConfigStore()
    config = store.clear_api_key()
    return render_template(
        "settings.html",
        page_title="Settings",
        page_description="Publisher configuration for integration endpoints and future automation.",
        azuracast_config=config,
        azuracast_key_cleared=True,
    )


@app.route("/settings/azuracast/test", methods=["POST"])
def test_azuracast_settings():
    store = AzuraCastConfigStore()
    result = test_azuracast_connection(store=store)
    return render_template(
        "settings.html",
        page_title="Settings",
        page_description="Publisher configuration for integration endpoints and future automation.",
        azuracast_config=store.get_config(),
        azuracast_test_result=result,
    )


@app.route("/settings/source")
def source_settings():
    store = RssSourceStore()
    return render_template(
        "source_settings.html",
        page_title="Source",
        page_description="Configured AzuraCast podcast RSS source for future audio acquisition.",
        rss_source_config=store.get_config(),
        rss_source_items=store.list_items(),
    )


@app.route("/settings/source", methods=["POST"])
def save_source_settings():
    store = RssSourceStore()
    config, errors = store.save_config(
        {
            "source_name": request.form.get("source_name"),
            "feed_url": request.form.get("feed_url"),
            "station_identifier": request.form.get("station_identifier"),
            "podcast_identifier": request.form.get("podcast_identifier"),
            "enabled": request.form.get("enabled") == "1",
        }
    )
    status_code = 400 if errors else 200
    return render_template(
        "source_settings.html",
        page_title="Source",
        page_description="Configured AzuraCast podcast RSS source for future audio acquisition.",
        rss_source_config=config or store.get_config(),
        rss_source_form=request.form,
        rss_source_errors=errors,
        rss_source_saved=not errors,
        rss_source_items=store.list_items(),
    ), status_code


@app.route("/settings/source/refresh", methods=["POST"])
def refresh_source_settings():
    store = RssSourceStore()
    result = refresh_rss_source(store=store)
    return render_template(
        "source_settings.html",
        page_title="Source",
        page_description="Configured AzuraCast podcast RSS source for future audio acquisition.",
        rss_source_config=result["config"],
        rss_source_items=store.list_items(),
        rss_source_refresh_result=result,
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
        event_name=request.args.get("event_name"),
    )
    return jsonify({"events": events}), 200


@app.route("/api/webhooks/azuracast", methods=["POST"])
def azuracast_webhook():
    payload, request_diagnostics = parse_azuracast_request(request)
    if not isinstance(payload, dict):
        emit_webhook_diagnostics(
            request_diagnostics=request_diagnostics,
            parser_decision="invalid_json",
            parser_reason="Request body could not be parsed as a JSON object.",
        )
        return jsonify({"ok": False, "message": "Invalid JSON payload."}), 400

    result = handle_azuracast_webhook(payload)
    emit_webhook_diagnostics(
        payload=payload,
        request_diagnostics=request_diagnostics,
        result=result,
    )
    return jsonify(
        {
            "ok": result["ok"],
            "message": result["message"],
            "event_type": result["event_type"],
            "run_id": result["run"]["run_id"] if result.get("run") else None,
            "session_id": result["run"]["session_id"] if result.get("run") else None,
        }
    ), result["status_code"]


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

        draft_result = create_castopod_draft_episode(
            audio_path=temp_path,
            filename=filename,
            title=title,
            description=description,
            podcast_id=podcast_id,
            http_post=requests.post,
            config={
                "castopod_url": CASTOPOD_URL,
                "api_user": API_USER,
                "api_pass": API_PASS,
                "public_host": PUBLIC_HOST,
                "podcast_id": PODCAST_ID,
                "created_by": CREATED_BY,
                "updated_by": UPDATED_BY,
                "request_timeout": REQUEST_TIMEOUT,
            },
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)

    if not draft_result["ok"]:
        return render_upload_template(
            error=draft_result["error"],
            detail=draft_result.get("detail"),
            form=request.form,
        ), draft_result.get("status_code") or 502

    episode_id = draft_result["episode_id"]

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
