# VPS hybrid playback deployment

This overlay builds the server fork while retaining `/opt/ma/docker-compose.yml`
as the source of truth for data, `/opt/media`, TLS, sing-box, and other services.
It advertises progressive FLAC URLs through the existing nginx
`/streamserver` location at `https://music.liandry.org`, and binds the raw
streamserver port 8097 to loopback. The signed pull token travels in the
`X-MA-Pull-Token` request header, not in the URL.
The fork owner explicitly authorizes this signed, item-scoped full-speed pull
exception to upstream stream pacing for the listed sources. Other stream
requests retain their pacing, and the pull endpoint does not expose provider URLs
or persist audio.

Place this checkout at `/opt/ma/server`, or set `MA_SERVER_SOURCE_DIR` to its
absolute path. Build the wheel expected by the root Dockerfile and validate
the merged configuration:

```sh
cd /opt/ma/server
install -d -m 755 /opt/ma/widevine_cdm
cp deploy/vps/PLACE_FILES_HERE.txt /opt/ma/widevine_cdm/
export MA_BUILD_COMMIT="$(git rev-parse HEAD)"
export MA_IMAGE_TAG="$(git rev-parse --short=12 HEAD)"
uv build --wheel --out-dir dist
docker compose -f /opt/ma/docker-compose.yml -f deploy/vps/compose.override.yml config --quiet
docker compose -f /opt/ma/docker-compose.yml -f deploy/vps/compose.override.yml build music-assistant
```

The checkout currently builds `dist/music_assistant-0.0.0-py3-none-any.whl`.
For a source archive without `.git`, set `MA_BUILD_COMMIT` to the source commit
and `MA_IMAGE_TAG` to its short hash before running the Compose commands.
Set `MA_PULL_PUBLIC_BASE_URL` if the public origin or proxy prefix differs from
`https://music.liandry.org/streamserver/pull`. The existing nginx location
forwards that path unchanged to port 8097, where this fork registers the pull
route. No provider CDN URL is sent to the app.

After reviewing the image and merged Compose configuration, redeploy with:

```sh
docker compose -f /opt/ma/docker-compose.yml -f deploy/vps/compose.override.yml up -d
```

Check that the running container uses the image built from this checkout:

```sh
container_id="$(docker compose -f /opt/ma/docker-compose.yml -f deploy/vps/compose.override.yml ps -q music-assistant)"
docker inspect "$container_id" --format '{{.Image}}'
docker image inspect "ma-server:${MA_IMAGE_TAG}" --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
```

The two image IDs must match, and the revision label must equal
`$MA_BUILD_COMMIT`. The image name uses the local `ma-server` repository, and
the overlay sets `pull_policy: never` for that service.

Local file playback uses the existing `/opt/media:/opt/media` bind mount. The
test album is under `/opt/media/2008 - The Slip`. Apple Music catalog playback
also requires `client_id.bin` and `private_key.pem` in `/opt/ma/widevine_cdm`
on the VPS. The overlay mounts that directory read-only at the provider's
`/usr/local/bin/widevine_cdm` path inside the container. The placeholder file
can remain there; add the CDM files later and restart the music-assistant
container. Until then, encrypted Apple Music catalog tracks cannot play. The
current VPS image predates that provider implementation. Streaming service
accounts must be configured through Music Assistant before their tracks can be
tested.

The server can be deployed before the updated mobile app: clients without the
negotiated `pull@v1` role continue on Sendspin push. The original handoff is in
`HANDOFF_SENDEPIN_HYBRID.md`; verified deviations are recorded in
`HANDOFF_SENDEPIN_HYBRID_REVIEW.md`.
