# Hybrid playback implementation review

`HANDOFF_SENDEPIN_HYBRID.md` remains the original approved design record. This
note records findings from checking it against the current server, mobile app,
and VPS. It does not replace the handoff.

## Fork authorization

The fork owner explicitly authorizes full-speed, signed, item-scoped pull playback
for local files and the listed streaming providers. This is a deliberate exception
to upstream Music Assistant's stream endpoint `-readrate` pacing rule for this
fork. The exception applies only to the negotiated pull client and does not
expose provider audio URLs or add downloading, exporting, or disk storage.
Push playback and its pacing remain in place for other clients and sources.

## Confirmed behavior and corrected assumptions

- The Sendspin push path has a 30-second producer limit and a separate
  aiosendspin player buffer limit. Both remain in place for push playback.
- The streamserver also paces its `/single` and `/flow` output with ffmpeg
  `-readrate`. Full-speed pull needs an explicit exception for an authenticated,
  approved request. This fork grants that exception only to a signed,
  item-scoped request for local files, Apple Music, Deezer, Tidal, Qobuz, or
  SoundCloud. Spotify (including librespot), YouTube Music, live sources, and
  synced groups remain push, per the owner's decision.
- `SendspinPlayer.requires_flow_mode` is always true, so the normal URL resolver
  does not provide a single-track URL for this player. Pull uses a separate
  single-item URL issuer. This first implementation does not provide gapless
  `/flow` playback; each track has its own request.
- iOS `SinkHandle.write` accepts PCM into a native queue immediately. It does
  not pace writes at the hardware rate. The pull loop limits its native output
  lead explicitly and keeps the rest in a bounded, indexed FLAC buffer. Pull
  pause retains accepted audio in AudioQueue, so resuming does not discard it.
- Sendspin's existing decoders accept complete codec packets. HTTP FLAC is a
  byte stream, so the mobile app parses STREAMINFO and complete CRC-checked
  frames before passing frames to the existing decoders.
- The VPS already publishes port 8097 and proxies `/streamserver` to it. The
  Compose overlay binds 8097 to loopback and advertises the HTTPS proxy route.

## Current implementation and limits

The mobile client advertises `pull@v1`, fetches first-party FLAC with a signed
token header, retains complete frames up to the configured in-memory cap, and
can seek within those frames with no network request. When the cap is reached,
consumed frames can be evicted so playback can continue. The server receives
encrypted, item-scoped position, end, and failure reports. Failure status `4`
stops the queue at the current item, allowing a fresh Play request. Local skip
and power-off stop retained audio even while disconnected. Old clients continue
on push.

The existing buffer-size slider still controls the Sendspin push buffer. Pull
has a separate 32-512 MB setting, defaulting to 128 MB. Existing installs
without the new key receive the default through an idempotent settings migration.
Missing, damaged, or out-of-range pull values are repaired; the existing push
value is not changed.
The configured cap applies when the next pull item starts.

The simulator tests cover FLAC framing, HTTP fetch failure and retry, and
offline replay from the retained buffer. The server tests cover signed-token
binding, provider routing, and terminal failure handling. The native iOS
simulator app build succeeds. Real-device audio output, gapless transitions,
and streaming provider accounts have not yet been exercised. Apple Music
catalog audio requires its server-side Widevine CDM files. The VPS overlay has
a read-only mount for `/opt/ma/widevine_cdm`. It remains a placeholder until
the owner adds the files.
