# Handoff: Hybrid Pull/Push Playback — Resolving the 30 s Sendspin Buffer Cap

| | |
|---|---|
| **Date** | 2026-09-29 |
| **Status** | Design approved, ready for implementation |
| **Server repo** | `/Users/aulyanov/git/ma-server` (fork `github.com:lucidium/server`, HEAD `68443338d`) |
| **Client repo** | `/Users/aulyanov/git/ma-mobile-app` (fork `github.com:lucidium/mobile-app`, HEAD `2d149ff7`) |
| **Supersedes** | `ma-mobile-app/.claude/sendspin-status.md` (old client repo) — its root-cause changelog remains valid |
| **Deployment target** | VPS `188.127.227.85` — MA `2.10.4` + aiosendspin `9.1.1`, containers currently **stopped** |

> Single source of truth for the hybrid-transport feature. A copy lives in both repos
> (`ma-server/HANDOFF_SENDEPIN_HYBRID.md`, `ma-mobile-app/HANDOFF_SENDEPIN_HYBRID.md`).
> The server copy is canonical; keep the client copy in sync.

> **First-rollout addendum (2026-09-29):** This handoff is retained as the original
> design record. Implementation follows the owner's later decisions: progressive
> pull is enabled for local files, Apple Music, Deezer, Tidal, Qobuz, and
> SoundCloud; all Spotify, YouTube Music, live sources, and synced groups stay
> on push. Pull is an unpaced, signed, per-track FLAC request with local seek
> within a bounded in-memory buffer (128 MB by default). A brief gap between
> pull tracks is accepted for this deployment. The streamserver's normal
> `/single` and `/flow` responses are paced, contrary to the assumption below.
> See the server repo's `HANDOFF_SENDEPIN_HYBRID_REVIEW.md` for verified behavior and rollout
> limits. The original sections below remain for design history.

---

## 1. Executive summary

- **Problem:** playback never buffers more than ~30 s ahead. Root cause is **entirely server-side**:
  two independent 30-second caps in the sendspin push path. Not a mobile-app bug.
- **Both caps verified:** MA provider constant `_PRODUCER_BUFFER_LIMIT_US = 30_000_000`
  (`providers/sendspin/playback.py:77`) applied via `sleep_to_limit_buffer()` (line 899), and
  aiosendspin `9.1.1` `PlayerPersistentState.max_duration_us = 30_000_000` (library default, not overridden).
- **Solution:** a **hybrid transport**. Full-speed sources (local files, Tidal, Qobuz, Deezer,
  SoundCloud, Apple Music, Spotify-via-librespot) are served over MA's existing **streamserver**
  (progressive HTTP, port 8097, unpaced, no artificial cap — the cap becomes client RAM).
  Paced sources (Spotify-via-Soloist, YouTube Music, radio/live) and **synced groups** stay on the
  existing **sendspin push** path, where the 30 s cap costs nothing because those sources cannot
  outpace realtime anyway.
- **Routing is data-driven**, keyed off `StreamDetails.is_realtime` — a predicate the server already
  computes in `_is_live_source()` (`providers/sendspin/playback.py:1538`). No provider allowlist.
- **Scope:** one contained server patch (transport handoff + one new wire command) + one new client
  pipeline (`PullPlaybackPipeline`) reusing the existing decoder/sink stack. No new client
  dependencies (Ktor already in the catalog). Backwards-compatible in both directions.

---

## 2. Problem statement and root cause

### 2.1 Symptom

During playback the client buffer fills to ~30 s within 1–2 s (≈25× front-load), then stops. Any
network interruption longer than the remaining cushion stalls playback. Requirement: deep buffering
(minutes to whole-track) for unreliable-network listening.

### 2.2 Root cause (verified, server-side only)

| # | Cap | Location | Mechanism |
|---|-----|----------|-----------|
| 1 | MA provider producer limit | `music_assistant/providers/sendspin/playback.py:77` `_PRODUCER_BUFFER_LIMIT_US = 30_000_000` | Producer throttled via `await push_stream.sleep_to_limit_buffer(_PRODUCER_BUFFER_LIMIT_US)` (line 899); queue sized from it (line 570); readiness deadline (line 1446) |
| 2 | aiosendspin persistent-state limit | aiosendspin `9.1.1`, `PlayerPersistentState.max_duration_us = 30_000_000` | Library default; MA does not override. Caps the persistent player-side queue duration |

Client byte capacity never binds with Opus output (only PCM F32LE at low slider values would).
**Neither cap is a client bug; neither can be fixed from the client.**

### 2.3 Why the caps are correct for *some* sources

The producer cap exists because some sources genuinely deliver at playback pace: Spotify's official
client (Soloist) is captured from a PulseAudio FIFO at ~1.1× realtime (`backends/soloist.py:246`,
`is_realtime = True`); YouTube Music sets `is_realtime=True` (`providers/ytmusic/__init__.py:753`);
radio/live media types are inherently paced. For those, deeper buffering is **physically impossible** —
the audio does not exist server-side faster than playback pace. For everything else the cap is pure loss.

---

## 3. Investigation findings (all verified in source)

### 3.1 The two server transports already coexist

| | Sendspin push (current app path) | Streamserver pull (DLNA path, port 8097) |
|---|---|---|
| Direction | Server → client, wall-clock paced | Client pulls via HTTP GET |
| URLs | N/A (Noise/WebSocket wire protocol) | `/single/{session}/{queue}/{item}/{player}.{fmt}`, `/flow/...` (gapless), `/source/...` |
| Pacing | Scheduler/wall clock + producer cap | **Unpaced** for buffered sources; ffmpeg transcodes as fast as CPU allows (realtime pacing only for live AudioSources) |
| Cap | 30 s (both caps above) | None — bounded only by client RAM |
| Output codec | Per-player config (PCM/Opus/FLAC) | Per-player config (default **FLAC**) |
| DSP/normalization | Included (server-side PCM chain) | Included |
| HTTP Range | N/A | **Not supported** — seek = fresh session URL |
| Entry point | `providers/sendspin/playback.py` `start()` (line 435) / `_run_playback()` (line 739) | `controllers/streams/controller.py` `resolve_stream_url()` (line 609) |

### 3.2 Provider compatibility matrix

`StreamDetails.is_realtime` defaults to `False` ("delivers as fast as the connection allows").
A codebase-wide sweep found **only** `ytmusic` and Spotify's Soloist backend set it to `True`.

| Source | Stream mechanism | `is_realtime` | Deep-bufferable via pull |
|---|---|---|---|
| Local files | Disk | False | ✅ |
| Tidal | `streaming.get_stream_details()` → CDN file URL | False | ✅ |
| Qobuz | `get_stream_details()` (`__init__.py:511`) → CDN FLAC URL | False | ✅ |
| Deezer | `streaming_manager` (`provider.py:364`) → CDN URL | False | ✅ |
| SoundCloud | `get_stream_details()` (`__init__.py:537`) → CDN URL | False | ✅ |
| Apple Music — library tracks | Direct HTTP asset URL (`StreamType.HTTP`) | False | ✅ |
| Apple Music — catalog tracks | `StreamType.ENCRYPTED_HTTP`: HLS + Widevine CDM decryption server-side (`apple_music/streaming.py:67`) | False | ✅ — needs Widevine CDM files on server; **needs server ≥ current `main`** (deployed 2.10.4 is metadata-only) |
| Spotify — librespot backend (**default**, `provider.py:339`) | `librespot --single-track` per item, decrypted Ogg Vorbis at full CDN speed (`backends/base.py:72` → `is_realtime=False`) | False | ✅ — **but accounts created since Dec 2024 cannot use librespot** (provider README) |
| Spotify — Soloist backend | Official headless client → PulseAudio capture FIFO @ ~1.1× (`backends/soloist.py:246`) | **True** | ❌ physically impossible (any transport) |
| YouTube Music | Adaptive paced stream (`ytmusic/__init__.py:753`) | **True** | ❌ physically impossible (any transport) |
| Radio / live / AudioSource | Inherently paced | True (media type) | ❌ by nature |

**Key insight:** the hybrid is not a compromise — it assigns each source the only transport matching
its physical delivery characteristic. Spotify routing is per-*backend*, not per-provider:
librespot-Spotify lands on pull automatically, Soloist-Spotify on push automatically.

### 3.3 Access gaps that motivate the server patch

- `resolve_stream_url()` is **not** exposed via JSON-RPC (only `streams/info` at `controller.py:307`
  is an `@api_command`); session URLs are minted server-side.
- The app's player registers via the push-only sendspin provider; no code path today hands a pull
  URL to a sendspin player.

---

## 4. Chosen architecture: hybrid transport

### 4.1 Routing rules

The decision is made **per queue item, at play time**, inside the sendspin provider:

| Condition | Transport | Why |
|---|---|---|
| Player is member of a **synced group** | **push** (override) | Sync/wall-clock only exists on the sendspin path; pull abandons it |
| `is_realtime == True` (Soloist Spotify, YT Music, radio/live) | **push** | Source cannot outpace realtime; pull gains nothing |
| Everything else (buffered sources) | **pull** via streamserver | Full-speed transcode + deep client buffer; both 30 s caps bypassed |
| Per-player explicit override (`push`/`pull`/`auto`) | as configured | Escape hatch for debugging and edge cases; default `auto` = the table above |

The predicate for "paced source" already exists: `_is_live_source(media)` in
`providers/sendspin/playback.py:1538` (media type in `_LIVE_MEDIA_TYPES` **or**
`queue_item.streamdetails.is_realtime`). Today it only tunes send-ahead sizing; the patch promotes
it to the transport router. Grouped-state check: the provider already knows the player's sync role
(`synchronizer_role.py`, `bridge_role.py`).

### 4.2 End-to-end flow (pull case)

```
 App                          MA server                                    Source
 │                                │                                           │
 │  play (JSON-RPC queue cmd) ──► │                                           │
 │                                │ queue item → get_stream_details() ───────► │ (token/URL
 │                                │                    ◄────────────────────── │  resolution)
 │                                │ is_realtime? no  ──► pull selected         │
 │                                │ resolve_stream_url(player, media)          │
 │                                │   → /single/… or /flow/… session URL       │
 │        ◄── wire cmd "play_url" │ (no push producer started)                 │
 │                                │                                           │
 │  GET session URL ────────────► │ ffmpeg transcode (unpaced, full speed) ──► │ CDN/disk
 │        ◄────── bytes ───────── │                                           │
 │ PullPlaybackPipeline:                                                        │
 │  byte buffer → DecoderStage → AudioSink                                     │
 │  (paced by blocking sink writes; buffer grows to whole-track/cap)           │
```

Push case is unchanged from today. Mixed queues alternate transports per item; gapless across a
transport boundary is a pipeline swap (inherent to any hybrid; see §9 Risks).

---

## 5. Server implementation — `/Users/aulyanov/git/ma-server`

All changes are contained in the sendspin provider + (optionally) the streams controller.
Nothing else in the server needs to know the hybrid exists.

### Patch S1 — transport routing in the sendspin provider

**File:** `music_assistant/providers/sendspin/playback.py` (and `player.py` for config plumbing).

1. **Per-player config**: add `transport` setting to the player's config options:
   `"auto" | "push" | "pull"`, default `"auto"`. Exposed via the provider's existing player
   config mechanism (see `player.py` / provider `config_entries`).
2. **Router function** (new, small):
   ```python
   def _use_pull_transport(self, media: PlayerMedia) -> bool:
       """True when this item should be handed to the client as a stream URL."""
       mode = self.player_settings.transport  # auto/push/pull
       if mode == "push":
           return False
       if mode == "pull":
           return True
       # auto: pull only when ungrouped and the source can be read ahead
       if self._is_grouped():          # player participates in a synced group
           return False
       return not self._is_live_source(media)   # existing helper, playback.py:1538
   ```
   `_is_grouped()` should consult the existing sync-role state (`bridge_role.py` /
   `synchronizer_role.py`); the exact accessor is an implementation detail to confirm on site.
3. **Handoff in `start()` (line 435) / `_run_playback()` (line 739):** when
   `_use_pull_transport(media)` is true, do **not** start the push producer. Instead:
   ```python
   url = await self.mass.streams.resolve_stream_url(self.player_id, media)
   await self._send_play_url(media, url)     # new wire command, Patch S2
   ```
   Keep the existing bookkeeping (queue state, `on_played` reporting, announcements) intact —
   only the audio delivery changes. Position for pull items now comes from the client's state
   reports (the provider must keep updating queue elapsed time from player-reported position,
   the same way external players do).
4. **Seek/restart:** `start(media, restart=True)` (with seek offset in `media`) re-resolves a
   fresh URL — never reuse, the streamserver has no Range support. The provider must treat every
   (re)start of a pull item as a new `resolve_stream_url` call.
5. **Gapless:** when routing to pull and subsequent queue items are also pull-eligible, prefer the
   `/flow/...` URL form (server-side gapless) — `resolve_stream_url` already handles queue-flow
   logic for pull players; verify it returns flow URLs when the queue has upcoming items and
   include a `gapless: bool` hint in the wire message.

### Patch S2 — wire command `play_url` (client handoff)

**Preferred mechanism:** a new sendspin wire command delivered over the existing authenticated
Noise/WebSocket channel (keeps transport intent explicit, no extra HTTP round trip, no new auth
surface). Add alongside the existing stream-start family:

```json
{
  "type": "play_url",
  "item_id": "<queue item id>",
  "queue_id": "<queue id>",
  "url": "https://<host>/single/<session>/<queue>/<item>/<player>.flac",
  "codec": "flac",                 // matches client AudioCodec serialnames: opus|flac|pcm
  "sample_rate": 44100, "channels": 2, "bit_depth": 16,
  "start_position_ms": 0,          // for seek/restart
  "gapless": false                 // url is a /flow stream continuing into next items
}
```

- Register in the client-facing wire schema and bump the negotiated role/feature version
  (client: `wire/VersionedRole.kt`; server: aiosendspin protocol version or the provider's
  role advertisement — confirm which layer owns the version on site).
- **Capability negotiation / fallback:** the client announces `supports_play_url` during
  registration/hello. If absent (older client), the server silently falls back to push —
  this makes the server patch deployable before the client update and keeps compat with
  any other sendspin clients.

**Rejected alternative (documented for context):** exposing `resolve_stream_url` as a JSON-RPC
`@api_command` and letting the app fetch the URL itself. Works, but adds an auth-surface question
(player-scoped scopes for an otherwise internal command), an extra round trip, and splits the
playback decision across two channels. Implement it only as a debug aid if useful.

### Patch S3 — streamserver exposure & hardening (small, verify-only)

The streamserver already serves any queue item by session; **no streaming machinery changes**.
Verify only:

- Session URL lifetime/expiry semantics (session IDs in `/single/{session}/...`) — the client must
  tolerate URL expiry on long-paused playback by asking the server to re-issue (see client C4).
- Output format per player config (default FLAC) — align with client decoder support; if the app's
  platform decoders prefer Opus (smaller buffers, pass-through on iOS), set the app player's
  streamserver format accordingly in player config rather than changing server defaults.
- Optional debug aid: `@api_command("streams/resolve_url", ...)` wrapping `resolve_stream_url`
  (scope-protected) — useful for curl-level testing of the pull path without the app.

### What NOT to change

- `_PRODUCER_BUFFER_LIMIT_US` / aiosendspin `max_duration_us`: both stay. They are *correct*
  for the push path (paced sources, groups). The hybrid makes them irrelevant for pull items,
  which is the whole point. Do not "fix" the caps by raising them — that would only trade
  correctness for a slightly larger paced-source cushion.

### Build & deploy (VPS `188.127.227.85`)

1. Build a custom image from the fork (`Dockerfile` at repo root; `docker build -t ghcr.io/lucidium/server:hybrid .`).
2. Compose changes on the VPS:
   - Publish the streamserver port (default `8097`) — currently likely unexposed.
   - Note: the deployed image installs MA at `/app/venv/lib/python3.14/site-packages/music_assistant`
     (matters for any on-VPS debugging).
3. nginx reverse-proxy: add a location for the streamserver (proxy to `127.0.0.1:8097`):
   ```nginx
   location /ma-streams/ {
       proxy_pass http://127.0.0.1:8097/;
       proxy_http_version 1.1;
       proxy_buffering off;          # progressive delivery, no spooling
       proxy_request_buffering off;
       proxy_read_timeout 3600s;     # long-lived downloads; pauses are client-side backpressure
       proxy_set_header Connection "";
       # auth: session URLs carry unguessable session IDs; additionally restrict by source IP
       # or require the app's existing auth token header if the streamserver honors it — VERIFY.
   }
   ```
   **Security note:** confirm the auth posture of session URLs before exposing publicly. The
   URLs are minted server-side with session identifiers, but if they are pure-capability URLs
   with no expiry, consider the nginx-level restriction or a `map`-based allowlist. This is the
   main open security question (§11).
4. Apple Music full streaming requires the `main`-base image (the fork is already `main`-based;
   the currently deployed `2.10.4` is metadata-only for Apple Music) **plus** Widevine CDM files
   placed on the server (`_decrypt_client_id` / `_decrypt_private_key`).
5. Rollout order: deploy server patch first (old clients keep working via push fallback),
   then ship the client update, then optionally flip per-player `transport` overrides if `auto`
   misbehaves for any source.

---

## 6. Client implementation — `/Users/aulyanov/git/ma-mobile-app`

Kotlin Multiplatform app. Modules: `:composeApp` (`io.music_assistant.client`), `:sendspin`
(`io.music_assistant.sendspin` — protocol + audio), `:androidApp`, `:shared-icons`.
**Ktor `3.5.2` is already in `gradle/libs.versions.toml` (client-core/cio/android)** — the pull
HTTP client needs no new dependencies.

### Relevant existing code (inventory)

| Area | File(s) |
|---|---|
| Push audio pipeline | `sendspin/src/commonMain/kotlin/io/music_assistant/sendspin/audio/AudioPipeline.kt` (reader-side control surface, `StreamState`), `Scheduler.kt` (wall-clock pacing), `JitterBuffer.kt`, `DecoderStage.kt`, `DriftCorrector.kt`, `StreamLifecycle.kt` |
| Output API | `sendspin/.../api/AudioSink.kt` (`open(format): SinkHandle`; `write` **blocks at hardware rate** — natural pacing), `AudioDecoder.kt` (`DecoderFactory`), `AudioCodec.kt` (`OPUS`, `FLAC`, `PCM`), `SinkFormat`, `PlayerState.kt`, `LocalPlayerConfig.kt` |
| Wire/transport | `sendspin/.../wire/Messages.kt`, `WireCodec.kt`, `VersionedRole.kt`, `Binary.kt`; `transport/WebSocketTransport.kt`, `TransportConnector.kt`; `management/ManagementHandler.kt`; `connection/ConnectionSupervisor.kt` |
| App glue | `composeApp/.../data/LocalPlayerAdapter.kt`, `LocalPlayerDispatch.kt`, `LocalPlayerEndpoints.kt`, `PlayerPositionTracker.kt`; `player/PlatformAudioPlayer.kt`, `MediaSessionBridge.kt`; `player/local/AudioQueueSink.ios.kt` (iOS sink, supports compressed pass-through via `SinkFormat.codec`) |
| Test patterns | `sendspin/src/commonTest/.../fakes/FakeDecoders.kt`, `audio/AudioPipelineTest.kt`, `player/SendspinPlayerTest.kt` |

### C1 — `PullPlaybackPipeline` (new, in `:sendspin` module, package `...sendspin.audio`)

A parallel sibling of `AudioPipeline`, **without** `Scheduler`/`ClockSync`/`JitterBuffer`
wall-clock machinery. Pacing comes from the sink itself: `SinkHandle.write()` blocks at the
hardware rate, so an unpaced read→decode→write loop is automatically drain-rate-limited, and the
in-memory buffer ahead of the decoder grows to the target while network allows.

```
Ktor HttpClient (cio/android/darwin engine)
  └─ GET url → ByteReadChannel
       └─ PreDecodeBuffer (ring/chain of ByteArray, target: whole item, cap: config)
            └─ DecoderStage (REUSE — codec from wire message: flac|opus|pcm)
                 └─ AudioSink (REUSE — SinkHandle.write blocks; pause/resume/flush map 1:1)
```

Responsibilities:
- **Read loop**: consume the HTTP body as fast as it arrives into `PreDecodeBuffer`; suspend when
  buffer is full (cap reached) — this is also *pause*: stop reading = backpressure up through
  nginx to ffmpeg (verified: transcode is pull-driven, generator-based).
- **Decode/write loop**: pop encoded frames, decode via `DecoderStage`, `sink.write(pcm)`.
  Blocking writes pace playback. No timestamps needed — byte order *is* the timeline.
- **End-of-stream → "ended" event**: same event shape as push pipeline (`AudioEvent.Ended`
  analogue) so upper layers need only one state machine.
- **Error policy**: read errors with buffered audio remaining → keep playing from buffer, retry
  the connection with a `Range`-less restart **only if** `start_position_ms + consumed` can be
  re-requested via a fresh URL from the server (see C4); otherwise surface failure at buffer
  underrun like today.

### C2 — Transport arbitration (in `SendspinPlayer` / `ManagementHandler`)

- Handle the new `play_url` wire message: tear down any active push stream consumption
  (`StreamLifecycle`) **and** any active pull pipeline; start `PullPlaybackPipeline` with the
  message payload. Mirror on `stream_start` (push): tear down pull pipeline first.
- Announce `supports_play_url` during registration/hello (`wire/VersionedRole.kt` bump).
- Player state reporting must be transport-agnostic: `PlayerState`/position updates already flow
  to the server (`PlayerPositionTracker.kt`, `LocalPlayerEndpoints.kt`) — ensure position for pull
  items is derived from decoder-consumed duration (not wall clock) so pause doesn't drift the
  reported position.

### C3 — Seek / pause / resume semantics (pull items)

| Action | Implementation |
|---|---|
| Pause | `sink.pause()` **and** suspend the read loop (stop consuming HTTP body) — server-side transcode back-pressures and idles. Buffer contents are preserved |
| Resume | `sink.resume()`, resume read loop |
| Seek | Tear down pipeline; ask server to restart item at offset (existing queue/player API); server re-issues `play_url` with new session URL + `start_position_ms`. **Never** reuse a URL — no HTTP Range on the streamserver |
| Stop/skip | Tear down pipeline; buffer discarded |
| URL expiry on long pause | If read resume fails with 4xx/expired-session: request re-issue at current consumed position (same path as seek). Confirm server session TTL (open question §11) |

### C4 — Gapless (`/flow`)

When the server hands a `gapless: true` URL, the HTTP body is a continuous encoded stream across
queue items. The pipeline decodes straight through — gapless for free, no crossfade logic. The
client must **not** tear down between items; instead watch for the server's item-change
notifications to update now-playing UI. Transport-boundary transitions (pull item → push item or
vice versa) are non-gapless by construction; acceptable.

### C5 — Buffer policy & memory

- Target: **whole item** when known and below cap; otherwise cap. Suggested defaults:
  `maxBufferMinutes = 15`, `maxBufferBytes = 128 MB` (FLAC 44.1/16 stereo ≈ 10 MB/min — cap binds
  first; Opus 192 kbps ≈ 1.4 MB/min — rarely binds).
- Readiness threshold: start playback at first configurable watermark (e.g. 3 s buffered or
  buffer-fill % — pick one; 3 s matches current UX).
- Expose in `LocalPlayerConfig`/app settings alongside the existing sendspin buffer slider, so the
  same UI governs both transports.
- iOS pass-through: `AudioQueueSink` accepts compressed `SinkFormat.codec` (FLAC/Opus) with
  `codecHeaderBase64`; pass-through skips client decode entirely — preferred on iOS if the
  streamserver output codec matches. Verify FLAC pass-through behaves with `/flow` concatenation.

### C6 — Testing

- Unit (follow `AudioPipelineTest` / `FakeDecoders` patterns): `PullPlaybackPipeline` against a
  fake `ByteReadChannel` — fast-fill until cap, sink-write pacing, pause backpressure, EOS event,
  error-with-buffer-remaining policy.
- Arbitration tests: `play_url` ⇄ `stream_start` teardown matrix (no double audio, no leak).
- Integration (VPS or local docker): local file → confirm full-track arrival well under 10 s;
  Tidal/Qobuz/Deezer/SoundCloud tracks same; Spotify-librespot same; Spotify-Soloist stays push;
  YT Music stays push; grouped player stays push; seek returns fresh URL; 15-min airplane-mode
  playback with full buffer.

---

## 7. Compatibility matrix

| Server \ Client | Old (no `supports_play_url`) | New |
|---|---|---|
| **Old (unpatched)** | Push only (status quo) | Push only (client never receives `play_url`) — safe |
| **New (patched)** | Server falls back to push for that player | **Hybrid active** |

Deployment order is therefore free; recommended server-first (§5 rollout).

## 8. Acceptance criteria

1. Local file playback on the ungrouped app player buffers the **entire track** (or cap) within
   seconds; observed server-side transcode front-load ≈25× realtime persists to completion.
2. No 30 s ceiling anywhere on the pull path: `PlayerState`-visible buffered duration exceeds
   60 s for a 5+ min track; airplane-mode test passes for the buffered duration.
3. Tidal/Qobuz/Deezer/SoundCloud/Apple Music tracks behave identically (full-speed fill).
4. Spotify: with librespot backend → pull path + deep buffer; with Soloist → push path (routing
   verified, not deep-buffered — by nature).
5. YT Music / radio → push path, unchanged behavior.
6. Grouped player → push path with sync intact.
7. Seek works on both transports (pull: fresh URL + position offset honored).
8. Pause/resume on pull: no drift in reported position; server transcode idles while paused.
9. Gapless consecutive pull items via `/flow` (no audible gap); push runs unchanged.
10. Old-client + new-server combo: playback works exactly as today (push fallback).

---

## 9. Risks & mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| nginx-exposed streamserver auth posture unknown (capability URLs, unknown TTL) | Session URL leakage = playable stream | Verify TTL + add nginx restrictions; URLs travel only inside the authenticated Noise channel |
| VPS CPU on 25× front-load (FLAC encode) | Transcode stalls / contention | FLAC encodes ≫25× realtime per core; monitor; Opus output cheaper if needed |
| iOS FLAC pass-through with `/flow` concatenation | Decode glitch at item boundary | Verify early; fall back to client-side decode or Opus output |
| Gapless break at transport boundaries (pull→push) | Audible gap in mixed queues | Inherent to hybrid; documented; rare in practice (user controls queue composition) |
| Server fork drift from upstream | Rebase burden | Patch is small & contained (provider + optional controller cmd); aim upstream PR — capability-gated transport selection is generally useful (offline-friendly players) |
| Wire protocol versioning (new `play_url` command) | Old client crash on unknown message | Confirm client's unknown-message policy + announce capability explicitly; test old-client combo (§8.10) |
| `resolve_stream_url` behavior details (flow-vs-single selection, seek offsets) unverified at call level | Patch S1 assumptions off | First implementation step: read `resolve_stream_url` body + exercise via debug RPC/curl before wiring |

## 10. Suggested implementation order

1. **Server recon (½ day):** read `resolve_stream_url()` internals (flow selection, seek handling,
   session TTL); read `start()`/`_run_playback()` handoff points; identify grouped-state accessor.
2. **Server patch (1–2 days):** S1 router + handoff; S2 wire command + capability negotiation;
   S3 debug RPC. Unit tests for router truth table (§4.1).
3. **Deploy server** to VPS + nginx location; validate pull path with curl (session URL, full-speed
   download of a local-file item, no 30 s behavior).
4. **Client patch (2–4 days):** C1 `PullPlaybackPipeline` + tests; C2 arbitration + capability
   announce; C3 seek/pause; C5 buffer config UI.
5. **Integration:** acceptance list §8 on real devices; airplane-mode test.
6. **Polish:** `/flow` gapless validation, iOS pass-through check, upstream PR prep.

## 11. Open questions

1. **Streamserver session TTL / expiry semantics** — determines URL-expiry handling (C3) and the
   security posture (§9.1). Verify in `controllers/streams/controller.py` session lifecycle.
2. **Which layer owns the sendspin wire version** — aiosendspin protocol constant vs provider
   role advertisement; needed for clean `play_url` negotiation.
3. **`_is_grouped()` accessor** — exact API for "player is in a synced group" from the provider.
4. **Pull output codec default** — FLAC (server default, ~10 MB/min) vs Opus 192 kbps (~1.4 MB/min,
   iOS pass-through). Recommend deciding after the iOS FLAC pass-through check (C5).
5. **Position reporting cadence for pull items** — confirm the server's queue-time update path
   accepts client-reported position for sendspin players when no push stream is active.
6. **Upstream appetite** — whether to PR the hybrid into upstream MA (recommend yes, capability-gated).

## 12. Reference index (verified locations)

**Server (`/Users/aulyanov/git/ma-server`, HEAD `68443338d`):**
- `music_assistant/providers/sendspin/playback.py` — `_PRODUCER_BUFFER_LIMIT_US`:77, queue sizing:570,
  `sleep_to_limit_buffer` call:899, deadline:1446, `start()`:435, `_run_playback()`:739,
  `_is_live_source()`:1538, `_LIVE_MEDIA_TYPES`:~58
- `music_assistant/controllers/streams/controller.py` — `streams/info` @api_command:307,
  `resolve_stream_url()`:609
- `music_assistant/providers/spotify/backends/base.py:72` (`is_realtime=False`),
  `soloist.py:246` (`is_realtime=True`), `provider.py:339` (default backend `librespot`)
- `music_assistant/providers/ytmusic/__init__.py:753` (`is_realtime=True`)
- `music_assistant/providers/apple_music/streaming.py:67` (full streaming; ENCRYPTED_HTTP + CDM)
- `music_assistant/models/…/streamdetails.py` (models package): `is_realtime` default `False`
- Sync roles: `providers/sendspin/{synchronizer_role,bridge_role,bridge_manager}.py`

**Client (`/Users/aulyanov/git/ma-mobile-app`, HEAD `2d149ff7`):** see inventory table §6.

**Environment:** VPS `188.127.227.85` (MA `2.10.4`, aiosendspin `9.1.1`, containers stopped,
image path `/app/venv/lib/python3.14/site-packages/music_assistant`); aiosendspin
`PlayerPersistentState.max_duration_us = 30_000_000` (second cap; unchanged by design).

**Prior art / analysis artifacts:** `/tmp/mass-server` (analysis clone of upstream `main`),
`ma-mobile-app/.claude/sendspin-status.md` (root-cause changelog, old client repo).
