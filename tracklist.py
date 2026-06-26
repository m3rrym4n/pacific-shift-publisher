import re
from dataclasses import dataclass
from datetime import datetime, timezone


EMPTY_TRACKLIST_MESSAGE = "No AzuraCast track history was found for this session window."
UNAVAILABLE_OFFSET = "--:--:--"
STARTUP_GRACE_SECONDS = 30


@dataclass(frozen=True)
class TrackHistoryEntry:
    sh_id: str | None
    played_at: str | None
    played_at_epoch: int | None
    duration: int | None
    streamer: str | None
    artist: str | None
    title: str | None
    text: str | None

    @property
    def display(self):
        if self.artist and self.title:
            return f"{self.artist} - {self.title}"
        if self.title:
            return self.title
        if self.artist:
            return self.artist
        return self.text or "Unknown track"

    def as_dict(self):
        return {
            "sh_id": self.sh_id,
            "played_at": self.played_at,
            "played_at_epoch": self.played_at_epoch,
            "duration": self.duration,
            "streamer": self.streamer,
            "artist": self.artist,
            "title": self.title,
            "text": self.text,
            "display": self.display,
        }


def parse_song_history(payload):
    history = payload.get("song_history") if isinstance(payload, dict) else None
    if not isinstance(history, list):
        return []

    entries = []
    for item in history:
        entry = parse_song_history_entry(item)
        if entry:
            entries.append(entry)
    return entries


def parse_song_history_entry(item):
    if not isinstance(item, dict):
        return None
    song = item.get("song") if isinstance(item.get("song"), dict) else {}
    text = clean_text(song.get("text"))
    artist = clean_text(song.get("artist"))
    title = clean_text(song.get("title"))
    if not (artist or title) and text:
        parsed_artist, parsed_title = parse_artist_title_text(text)
        artist = parsed_artist
        title = parsed_title
    elif not title and text:
        title = text

    played_at_epoch = parse_epoch_seconds(item.get("played_at"))
    played_at = epoch_to_iso(played_at_epoch) if played_at_epoch is not None else None
    return TrackHistoryEntry(
        sh_id=clean_text(item.get("sh_id")),
        played_at=played_at,
        played_at_epoch=played_at_epoch,
        duration=parse_int(item.get("duration")),
        streamer=clean_text(item.get("streamer")),
        artist=artist,
        title=title,
        text=text,
    )


def parse_artist_title_text(text):
    if not text:
        return None, None
    for separator in (" - ", " – ", " — "):
        if separator in text:
            artist, title = text.split(separator, 1)
            return clean_text(artist), clean_text(title)
    return None, clean_text(text)


def filter_tracks_for_session(entries, started_at, ended_at):
    start_epoch = to_epoch_seconds(started_at)
    end_epoch = to_epoch_seconds(ended_at)
    if start_epoch is None or end_epoch is None:
        return []
    selected = [
        entry for entry in entries
        if entry.played_at_epoch is not None
        and (
            start_epoch <= entry.played_at_epoch <= end_epoch
            or track_overlaps_session_start(entry, start_epoch)
        )
    ]
    return dedupe_adjacent_tracks(sorted(selected, key=lambda entry: entry.played_at_epoch))


def format_tracklist(entries, started_at=None, startup_grace_seconds=STARTUP_GRACE_SECONDS):
    lines = ["Tracklist", ""]
    if not entries:
        lines.append(EMPTY_TRACKLIST_MESSAGE)
        return "\n".join(lines)
    for index, entry in enumerate(entries, start=1):
        if started_at is not None:
            lines.append(
                f"{episode_relative_timestamp(entry, started_at, startup_grace_seconds, index == 1)} {track_display(entry)}"
            )
        else:
            lines.append(f"{index:02d}. {track_display(entry)}")
    return "\n".join(lines)


def episode_relative_timestamp(track, started_at, startup_grace_seconds=None, is_first_track=False):
    start_epoch = to_epoch_seconds(started_at)
    played_epoch = to_epoch_seconds(track_value(track, "played_at_epoch"))
    if played_epoch is None:
        played_epoch = to_epoch_seconds(track_value(track, "played_at"))
    if start_epoch is None or played_epoch is None:
        return UNAVAILABLE_OFFSET
    observed_offset_seconds = played_epoch - start_epoch
    offset_seconds = max(0, observed_offset_seconds)
    if (
        is_first_track
        and startup_grace_seconds is not None
        and 0 <= observed_offset_seconds <= startup_grace_seconds
    ):
        offset_seconds = 0
    return format_offset_seconds(offset_seconds)


def track_overlaps_session_start(track, start_epoch):
    played_epoch = to_epoch_seconds(track_value(track, "played_at_epoch"))
    duration = parse_int(track_value(track, "duration"))
    if played_epoch is None or duration is None or duration <= 0:
        return False
    return played_epoch < start_epoch <= played_epoch + duration


def dedupe_adjacent_tracks(entries):
    deduped = []
    previous_key = None
    for entry in entries:
        key = normalize_track_identity(track_display(entry))
        if key and key == previous_key:
            continue
        deduped.append(entry)
        previous_key = key
    return deduped


def normalize_track_identity(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def format_offset_seconds(offset_seconds):
    offset_seconds = max(0, int(offset_seconds))
    hours = offset_seconds // 3600
    minutes = (offset_seconds % 3600) // 60
    seconds = offset_seconds % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def track_display(track):
    display = getattr(track, "display", None)
    if display:
        return display
    artist = track_value(track, "artist")
    title = track_value(track, "title")
    text = track_value(track, "text")
    if artist and title:
        return f"{artist} - {title}"
    if title:
        return title
    if artist:
        return artist
    return text or track_value(track, "display") or "Unknown track"


def track_value(track, key):
    if isinstance(track, dict):
        return track.get(key)
    return getattr(track, key, None)


def append_tracklist_to_description(description, tracklist_text):
    description = (description or "").strip()
    tracklist_text = (tracklist_text or format_tracklist([])).strip()
    if not description:
        return tracklist_text
    if tracklist_text in description:
        return description
    if has_tracklist_section(description):
        return replace_tracklist_section(description, tracklist_text)
    return f"{description}\n\n{tracklist_text}"


def has_tracklist_section(description):
    return re.search(r"(?im)^tracklist\s*$", description or "") is not None


def replace_tracklist_section(description, tracklist_text):
    return re.sub(
        r"(?ims)^tracklist\s*\n.*$",
        tracklist_text,
        description.strip(),
        count=1,
    )


def to_epoch_seconds(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return int(float(text))
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp())


def epoch_to_iso(value):
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat() if value is not None else None


def parse_epoch_seconds(value):
    return to_epoch_seconds(value)


def parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None
