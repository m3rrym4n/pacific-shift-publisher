import unittest

from tracklist import (
    EMPTY_TRACKLIST_MESSAGE,
    append_tracklist_to_description,
    filter_tracks_for_session,
    format_tracklist,
    parse_song_history,
)


class TracklistTest(unittest.TestCase):
    def test_parse_song_history_preserves_fields_and_prefers_artist_title(self):
        tracks = parse_song_history(
            {
                "song_history": [
                    {
                        "sh_id": 10,
                        "played_at": 1781935200,
                        "duration": "300",
                        "streamer": "SeaCapn",
                        "song": {
                            "text": "Technimatic - Unity (Original Mix)",
                            "artist": "Technimatic",
                            "title": "Unity (Original Mix)",
                        },
                    }
                ]
            }
        )

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].sh_id, "10")
        self.assertEqual(tracks[0].played_at_epoch, 1781935200)
        self.assertEqual(tracks[0].duration, 300)
        self.assertEqual(tracks[0].streamer, "SeaCapn")
        self.assertEqual(tracks[0].artist, "Technimatic")
        self.assertEqual(tracks[0].title, "Unity (Original Mix)")
        self.assertEqual(tracks[0].text, "Technimatic - Unity (Original Mix)")

    def test_parse_song_history_falls_back_to_text_and_skips_malformed_entries(self):
        tracks = parse_song_history(
            {
                "song_history": [
                    {"played_at": 1781935200, "song": {"text": "Break - Something New"}},
                    {"played_at": 1781935260, "song": {"text": "Standalone ID"}},
                    "bad-row",
                ]
            }
        )

        self.assertEqual(len(tracks), 2)
        self.assertEqual(tracks[0].artist, "Break")
        self.assertEqual(tracks[0].title, "Something New")
        self.assertIsNone(tracks[1].artist)
        self.assertEqual(tracks[1].title, "Standalone ID")

    def test_filter_tracks_for_completed_session_window_and_sort_played_order(self):
        tracks = parse_song_history(
            {
                "song_history": [
                    {"played_at": 1781935400, "song": {"artist": "In", "title": "Window Late"}},
                    {"played_at": 1781935100, "song": {"artist": "Old", "title": "Outside"}},
                    {"played_at": 1781935200, "song": {"artist": "In", "title": "Window Early"}},
                    {"played_at": 1781935600, "song": {"artist": "New", "title": "Outside"}},
                ]
            }
        )

        filtered = filter_tracks_for_session(
            tracks,
            started_at="2026-06-20T06:00:00+00:00",
            ended_at="2026-06-20T06:05:00+00:00",
        )

        self.assertEqual([track.title for track in filtered], ["Window Early", "Window Late"])

    def test_filter_includes_start_and_end_boundaries(self):
        tracks = parse_song_history(
            {
                "song_history": [
                    {"played_at": 1781935200, "song": {"artist": "Start", "title": "Boundary"}},
                    {"played_at": 1781935500, "song": {"artist": "End", "title": "Boundary"}},
                ]
            }
        )

        filtered = filter_tracks_for_session(tracks, started_at=1781935200, ended_at=1781935500)

        self.assertEqual(len(filtered), 2)

    def test_filter_missing_end_time_is_not_ready(self):
        tracks = parse_song_history({"song_history": [{"played_at": 1781935200, "song": {"text": "A - B"}}]})

        self.assertEqual(filter_tracks_for_session(tracks, started_at=1781935200, ended_at=None), [])

    def test_format_tracklist_and_empty_message(self):
        tracks = parse_song_history(
            {
                "song_history": [
                    {"played_at": 1, "song": {"artist": "Artist A", "title": "Title A"}},
                    {"played_at": 2, "song": {"text": "Title Only"}},
                ]
            }
        )

        self.assertEqual(
            format_tracklist(tracks),
            "Tracklist\n\n01. Artist A - Title A\n02. Title Only",
        )
        self.assertEqual(format_tracklist([]), f"Tracklist\n\n{EMPTY_TRACKLIST_MESSAGE}")

    def test_description_append_and_update_without_duplicate(self):
        tracklist = "Tracklist\n\n01. Artist - Title"
        description = "Existing description text"

        appended = append_tracklist_to_description(description, tracklist)
        repeated = append_tracklist_to_description(appended, tracklist)
        replaced = append_tracklist_to_description("Intro\n\nTracklist\n\n01. Old - Track", tracklist)

        self.assertEqual(appended, "Existing description text\n\nTracklist\n\n01. Artist - Title")
        self.assertEqual(repeated, appended)
        self.assertEqual(replaced, "Intro\n\nTracklist\n\n01. Artist - Title")


if __name__ == "__main__":
    unittest.main()
