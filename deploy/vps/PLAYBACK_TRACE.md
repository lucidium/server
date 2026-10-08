# Playback timing capture

`MA_PLAYBACK_TRACE=1` enables structured `PLAYBACK_TRACE` events at INFO level.
The normal setting is disabled. The diagnostic Dockerfile enables it in the
image, so the application log level can stay at INFO. No database migration or
client update is required.

Events cover queue locks and preparation, provider selection, HTTP pool/DNS/TLS
stages and response headers, source-slot acquisition, ffmpeg startup and HTTP
requests, first PCM, buffer readiness, pull handoff, first response bytes, and
client playback-state transitions. Durations use a monotonic clock. Nested
actions and producer tasks share a trace ID; HTTP requests have their own IDs.
Queue/item IDs correlate the separate client stream request and progress events.

Logged URLs include scheme, hostname and port. Userinfo, paths, query strings,
fragments, request/response bodies and authorization headers are omitted.
FFmpeg HTTP tracing extracts only destination, method, Range, numeric status,
connection and reconnect information. Its raw debug stderr and command line
are suppressed while HTTP tracing is active. Keep the overall log level at
INFO for this capture; blanket VERBOSE output from other components is unnecessary.

`http.headers` measures completion of response headers, not the full body.
`source.first_pcm` measures receipt of the first configured PCM chunk (normally
one second of audio), not a single decoded sample. `stream.first_bytes` can be
a FLAC header; the iPhone log provides the native AudioQueue milestone.
Readiness thresholds are seconds of buffered media, not mandatory wall-clock waits.

For reproduction, select an Apple Music track, switch tracks while it plays,
and select a Deezer track. Capture both a slow and a fast start if possible.
Keep the complete server log around those attempts along with the client,
sing-box and nginx logs. Note track names and approximate tap times. The trace
records the selected provider because a library track can have multiple mappings.

## Diagnostic image

`Dockerfile.playback-trace` builds on the exact currently deployed MA image.
Its build context must contain **only** the modified `music_assistant/` files,
including `helpers/playback_trace.py`. Compare each existing file's pre-change
SHA-256 with the deployed file before using this method. This preserves the
deployed dependencies, binaries and deployment-specific fixes.

Build with `--pull=false --network=none`, an explicit `BASE_IMAGE`, and a unique
tag/digest for the patch. Verify source hashes and module imports in the built
image before selecting it in `/opt/ma/.env`. Use the existing two Compose files
and recreate only `music-assistant` with `--no-deps --no-build --pull never`.
Retain the previous image and back up `.env` before the update. The live overlay
contains deployment-specific secrets declarations and must not be replaced by
the local overlay.

To disable tracing without reverting the code, set `MA_PLAYBACK_TRACE=0` in the
service environment and recreate MA. To roll back the deployment, restore the
previous `MA_IMAGE_TAG` and recreate the MA service with the same Compose files.
Production backups containing `.env` must remain on the VPS.
