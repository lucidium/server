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
absolute path. Configure the shared DNS path described below before using the
overlay. Build the wheel expected by the root Dockerfile and validate the merged
configuration:

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

Deezer encrypted tracks require an authorized 16-character decryption secret.
The optional `/opt/ma/secrets/deezer.env` file supplies it to the container as
`MASS_APP_VAR_DEEZER_DECRYPT_KEY`. Keep the directory private and the file readable
only by root (mode `0600`); do not put the key in this checkout or a Compose
command line. Without the file, the server still starts, but Deezer tracks cannot
play. After provisioning or changing the file, recreate the music-assistant
container so it receives the updated environment.

The Audio DB uses the public free v1 API key `123` in the Compose environment.
Its requests are paced at one every 2.5 seconds (at most 24 per minute), below
the free tier's 30-per-minute limit.

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

## Shared container DNS

Music Assistant and nginx join sing-box's network namespace and inherit its
Docker resolver configuration. Without an explicit `dns` setting on sing-box,
Docker's `127.0.0.11` resolver forwards queries through the host's nameservers,
outside the container's TUN. The overlay sets the sole upstream to `127.0.0.1`,
which Docker contacts inside the shared namespace. Keep Docker's embedded
resolver so container names still resolve. Do not add `dns` to the services
using `network_mode: service:sing-box`; Docker does not support that combination.

Before applying the overlay, back up `/opt/ma/sing-box/config.json` and add this
entry to its `inbounds` array (TCP and UDP are both enabled when `network` is
omitted):

```json
{
  "type": "direct",
  "tag": "dns-in",
  "listen": "127.0.0.1",
  "listen_port": 53
}
```

Add these rules at the start of `route.rules`, ahead of music outbound routing:

```json
[
  {"inbound": "dns-in", "action": "hijack-dns"},
  {"port": 53, "action": "hijack-dns"}
]
```

Keep the existing sniff rule and place the existing `protocol: dns` hijack rule
immediately after it, before the music outbound rule. This also captures clients
that send ordinary DNS directly to another resolver through the TUN.
The loopback listener is internal; do not publish port 53 on the host.

Retain the existing DNS upstream policy: music domains use the proxy DNS server,
and other domains and the proxy hostname use the direct encrypted DNS server.
The bootstrap resolver must have a literal IP address and must not depend on
the loopback listener or proxy, otherwise proxy startup can deadlock. A direct
encrypted lookup made by sing-box under this policy is intentional.

Validate with `docker exec music-sing-box sing-box check -c
/etc/sing-box/config.json` after writing the configuration. The overlay's live
DNS healthcheck requires the image's BusyBox `nslookup` command and gates startup
of Music Assistant and nginx on a working listener and upstream. Recreate all
three containers together so they join the new network namespace:

```sh
cd /opt/ma
docker compose -f docker-compose.yml -f server/deploy/vps/compose.override.yml config --quiet
docker compose -f docker-compose.yml -f server/deploy/vps/compose.override.yml up -d --force-recreate --no-build --pull never --wait --wait-timeout 180
docker exec music-assistant cat /etc/resolv.conf
docker exec music-sing-box nslookup -type=A pipe.deezer.com
docker exec music-sing-box nslookup -type=A api.music.apple.com
```

The resolver file should retain `nameserver 127.0.0.11` and show only
`ExtServers: [127.0.0.1]`, without `host(...)` upstreams. Confirm provider lookups
with a DNS capture on the VPS uplink: no plaintext provider queries should leave
on port 53. To roll back, restore both the sing-box configuration and the Compose
overlay from their backups, then run the same recreation command. Preserve any
deployment-specific secrets files and the current `MA_IMAGE_TAG` in `/opt/ma/.env`.
