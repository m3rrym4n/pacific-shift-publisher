from flask import Flask, request, render_template
import requests
import os
import uuid

app = Flask(__name__)

CASTOPOD_URL = os.getenv("CASTOPOD_URL")

API_USER = os.getenv("API_USER")
API_PASS = os.getenv("API_PASS")

HEADERS = {
    "Host": "pacific-shift.com",
    "X-Forwarded-Proto": "https"
}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():

    title = request.form["title"]
    description = request.form["description"]
    audio_file = request.files["audio_file"]

    temp_path = f"/tmp/{uuid.uuid4()}-{audio_file.filename}"
    audio_file.save(temp_path)

    with open(temp_path, "rb") as f:

        files = {
            "audio_file": f
        }

        data = {
            "created_by": 1,
            "updated_by": 1,
            "podcast_id": 1,
            "title": title,
            "slug": title.lower().replace(" ", "-"),
            "description": description,
            "type": "full"
        }

        r = requests.post(
            f"{CASTOPOD_URL}/api/rest/v1/episodes",
            auth=(API_USER, API_PASS),
            headers=HEADERS,
            files=files,
            data=data,
            timeout=3600
        )

    os.remove(temp_path)

    if r.status_code not in (200, 201):
        return f"Create failed:<br><pre>{r.text}</pre>"

    episode = r.json()

    publish = requests.post(
        f"{CASTOPOD_URL}/api/rest/v1/episodes/{episode['id']}/publish",
        auth=(API_USER, API_PASS),
        headers=HEADERS,
        data={
            "publication_method": "now",
            "created_by": 1
        }
    )

    return f"""
    Success!

    Episode ID: {episode['id']}

    Publish Response:

    <pre>{publish.text}</pre>
    """


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
