# Pipeline Run State

Publisher stores automation run state in a small SQLite database managed by `pipeline_state.py`.

Default database path:

```text
/app/data/publisher_state.sqlite
```

Override with:

```text
PUBLISHER_STATE_DB=/path/to/publisher_state.sqlite
```

## Run Fields

Each run represents one DJ set processing cycle and stores:

```text
run_id
station
show_name
streamer
started_at
ended_at
overall_status
current_step
session_id
recording_reference
tracklist_status
castopod_episode_id
castopod_episode_url
error_summary
created_at
updated_at
```

## Step Fields

Each run initializes these step keys in order:

```text
stream_start
stream_end
acquire_mp3
acquire_tracklist
assemble_episode
post_castopod_draft
```

Each step stores:

```text
step_key
status
started_at
ended_at
duration_ms
message
error_details
retry_count
```

Allowed statuses:

```text
pending
waiting
in_progress
success
failed
skipped
```

Future webhook, dashboard, run-history, and structured-log issues should call the service functions in `pipeline_state.py` rather than writing run state directly.
