PIPELINE_STEP_KEYS = (
    "stream_start",
    "stream_end",
    "acquire_mp3",
    "acquire_tracklist",
    "assemble_episode",
    "post_castopod_draft",
)

PIPELINE_STATUSES = (
    "pending",
    "waiting",
    "waiting_transcode",
    "in_progress",
    "success",
    "failed",
    "skipped",
)
