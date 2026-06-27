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
assembled_episode_payload
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
waiting_transcode
in_progress
success
failed
skipped
```

`waiting_transcode` is used by `acquire_mp3` after a matching AzuraCast streamer
broadcast is found but its `recording` field is still null. The matched broadcast
ID is persisted in `recording_reference` so a later sync cycle can resume without
repeating session matching.

Future webhook, dashboard, run-history, and structured-log issues should call the service functions in `pipeline_state.py` rather than writing run state directly.

## Structured Events

Pipeline state changes emit structured events through `pipeline_logging.py`.

Event fields:

```text
timestamp
level
run_id
session_id
step_key
event_name
status
message
details
```

Events are written to normal application logs as JSON and persisted in the same SQLite database in `pipeline_events`.

Future pipeline code should emit events through the structured logging helpers so dashboard cards, run history, and log views can filter by `run_id`, `session_id`, and `step_key`.

Messages and details are sanitized for common secret shapes such as bearer tokens, authorization headers, API keys, passwords, and secrets before they are logged or stored.
