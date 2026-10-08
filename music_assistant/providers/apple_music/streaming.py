"""Streaming and Widevine decryption for Apple Music."""

from __future__ import annotations

import base64
import json
import os
import re
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urljoin

import aiofiles
from aiohttp import ClientTimeout
from aiohttp.client_exceptions import ClientError
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails
from pywidevine import PSSH, Cdm, Device, DeviceTypes
from pywidevine.license_protocol_pb2 import WidevinePsshData
from shortuuid import uuid

from music_assistant.helpers.aiohttp_client import encoded_request_url
from music_assistant.helpers.ffmpeg import get_ffmpeg_hls_cmaf_input_args
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.playlists import PLAYLIST_READ_TIMEOUT, parse_m3u, read_playlist_body

from .constants import (
    CACHE_CATEGORY_DECRYPT_KEY,
    DECRYPT_CLIENT_ID_FILENAME,
    DECRYPT_PRIVATE_KEY_FILENAME,
    WIDEVINE_BASE_PATH,
)
from .helpers.utils import is_library_id

if TYPE_CHECKING:
    from .provider import AppleMusicProvider


class AppleMusicStreamingManager:
    """Handles stream URL resolution and Widevine decryption."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize streaming manager."""
        self.provider = provider
        self.api = provider.api_client
        self.logger = provider.logger
        self._decrypt_client_id: bytes | None = None
        self._decrypt_private_key: bytes | None = None
        self._session_id: str | None = None

    async def initialize(self) -> None:
        """Load Widevine CDM files and create a session ID."""
        self._session_id = str(uuid())
        try:
            async with aiofiles.open(
                os.path.join(WIDEVINE_BASE_PATH, DECRYPT_CLIENT_ID_FILENAME), "rb"
            ) as _file:
                self._decrypt_client_id = await _file.read()
            async with aiofiles.open(
                os.path.join(WIDEVINE_BASE_PATH, DECRYPT_PRIVATE_KEY_FILENAME), "rb"
            ) as _file:
                self._decrypt_private_key = await _file.read()
        except FileNotFoundError:
            self.logger.warning(
                "Widevine CDM files not found at %s. "
                "Streaming of encrypted catalog tracks will not work. "
                "Radio stations and unencrypted library tracks are still available.",
                WIDEVINE_BASE_PATH,
            )

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Return StreamDetails for a single catalog or library track."""
        stream_metadata = await self._fetch_song_stream_metadata(item_id)
        if is_library_id(item_id):
            try:
                stream_url = stream_metadata["assets"][0]["URL"]
            except (KeyError, IndexError, TypeError) as exc:
                raise MediaNotFoundError(
                    f"Failed to extract stream URL for library track {item_id}: {exc}"
                ) from exc
            return StreamDetails(
                item_id=item_id,
                provider=self.provider.instance_id,
                path=stream_url,
                stream_type=StreamType.HTTP,
                audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
                can_seek=True,
                allow_seek=True,
            )
        if not self._decrypt_client_id or not self._decrypt_private_key:
            raise MediaNotFoundError(
                "Widevine CDM files are not available. "
                "Cannot stream encrypted catalog tracks without them."
            )
        license_url = stream_metadata["hls-key-server-url"]
        stream_url, uri = await self._parse_stream_url_and_uri(stream_metadata["assets"])
        if not stream_url or not uri:
            raise MediaNotFoundError("No stream URL found for song.")
        key_id = base64.b64decode(uri.split(",")[1])
        decryption_key = await self._get_decryption_key(license_url, key_id, uri, item_id)
        return StreamDetails(
            item_id=item_id,
            provider=self.provider.instance_id,
            audio_format=AudioFormat(content_type=ContentType.MP4, codec_type=ContentType.AAC),
            # Keep the HLS byte-range index for both startup and seeking. StreamType.HLS
            # would pass through the generic substream resolver and discard that index.
            stream_type=StreamType.HTTP,
            extra_input_args=[
                "-f",
                "hls",
                "-seg_format_options",
                f"decryption_key={decryption_key}",
                # Fetch each declared range separately: FFmpeg 9 range reuse can
                # consume later fragments without decoding them. HLS still seeks
                # through the playlist index with explicit HTTP byte ranges.
                "-http_multiple",
                "0",
                "-http_seekable",
                "0",
                "-http_persistent",
                "0",
                *get_ffmpeg_hls_cmaf_input_args(),
            ],
            path=stream_url,
            can_seek=True,
            allow_seek=True,
        )

    async def _fetch_song_stream_metadata(self, song_id: str) -> dict[str, Any]:
        """Get the stream metadata for a song from Apple Music."""
        playback_url = "https://play.music.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
        data: dict[str, Any] = {}
        self.logger.debug("_fetch_song_stream_metadata: Check if Library ID: %s", song_id)
        if is_library_id(song_id):
            data["universalLibraryId"] = song_id
            data["isLibrary"] = True
        else:
            data["salableAdamId"] = song_id
        for retry in (True, False):
            try:
                async with self.provider.mass.http_session.post(
                    playback_url,
                    headers=self._decryption_headers,
                    json=data,
                    ssl=True,
                ) as response:
                    response.raise_for_status()
                    content = await response.json(loads=json_loads)
                    if content.get("failureType"):
                        message = content.get("failureMessage")
                        raise MediaNotFoundError(f"Failed to get song stream metadata: {message}")
                    return cast("dict[str, Any]", content["songList"][0])
            except (MediaNotFoundError, ClientError) as exc:
                if retry:
                    self.logger.warning("Failed to get song stream metadata: %s", exc)
                    continue
                raise
        raise MediaNotFoundError(f"Failed to get song stream metadata for {song_id}")

    async def _parse_stream_url_and_uri(
        self, stream_assets: list[dict[str, Any]]
    ) -> tuple[str | None, str | None]:
        """Return an in-memory HLS playlist and the account license key URI."""
        ctrp256_urls = [asset["URL"] for asset in stream_assets if asset["flavor"] == "28:ctrp256"]
        if not ctrp256_urls:
            raise MediaNotFoundError("No ctrp256 URL found for song.")
        async with self.provider.mass.http_session.get(
            encoded_request_url(ctrp256_urls[0]),
            timeout=ClientTimeout(total=PLAYLIST_READ_TIMEOUT),
        ) as response:
            response.raise_for_status()
            playlist = (await read_playlist_body(response.content)).decode("utf-8")
            playlist_url = str(response.url)
        playlist_items = parse_m3u(playlist)
        if not playlist_items:
            raise MediaNotFoundError("Empty Apple Music stream playlist.")
        # Resolve the license through the account as before, then give its key directly
        # to the MP4 demuxer. FFmpeg must not fetch the playlist's DRM key URIs itself.
        lines = []
        for raw_line in playlist.splitlines():
            line = raw_line.strip()
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                continue
            if line.startswith("#EXT-X-MAP:"):
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda match: f'URI="{urljoin(playlist_url, match[1])}"',
                    line,
                )
            elif line and not line.startswith("#"):
                line = urljoin(playlist_url, line)
            lines.append(line)
        encoded_playlist = base64.b64encode(("\n".join(lines) + "\n").encode()).decode()
        return (
            f"data:application/vnd.apple.mpegurl;base64,{encoded_playlist}",
            playlist_items[0].key,
        )

    @property
    def _decryption_headers(self) -> dict[str, str]:
        """Return headers required for decryption requests."""
        return {
            "authorization": f"Bearer {self.provider._music_app_token}",
            "media-user-token": cast("str", self.provider._music_user_token),
            "connection": "keep-alive",
            "accept": "application/json",
            "origin": "https://music.apple.com",
            "referer": "https://music.apple.com/",
            "accept-encoding": "gzip, deflate, br",
            "content-type": "application/json;charset=utf-8",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
            ),
        }

    async def _get_decryption_key(
        self, license_url: str, key_id: bytes, uri: str, item_id: str
    ) -> str:
        """Get (or retrieve from cache) the decryption key for a song."""
        if decryption_key := await self.provider.mass.cache.get(
            key=item_id,
            provider=self.provider.instance_id,
            category=CACHE_CATEGORY_DECRYPT_KEY,
            checksum=self._session_id,
        ):
            self.logger.debug("Decryption key for %s found in cache.", item_id)
            return cast("str", decryption_key)
        pssh = self._build_pssh(key_id)
        device = Device(
            client_id=self._decrypt_client_id,
            private_key=self._decrypt_private_key,
            type_=DeviceTypes.ANDROID,
            security_level=3,
            flags={},
        )
        cdm = Cdm.from_device(device)
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, pssh)
            track_license = await self._get_license(challenge, license_url, uri, item_id)
            cdm.parse_license(session_id, track_license)
            key = next((key for key in cdm.get_keys(session_id) if key.type == "CONTENT"), None)
            if not key:
                raise MediaNotFoundError(f"Unable to get decryption key for song {item_id}.")
            decryption_key = key.key.hex()
        finally:
            cdm.close(session_id)
        self.provider.mass.create_task(
            self.provider.mass.cache.set(
                key=item_id,
                data=decryption_key,
                expiration=3600,
                provider=self.provider.instance_id,
                category=CACHE_CATEGORY_DECRYPT_KEY,
                checksum=self._session_id,
            )
        )
        return decryption_key

    def _build_pssh(self, key_id: bytes) -> PSSH:
        """Build a Widevine PSSH object for the given key ID."""
        pssh_data = WidevinePsshData()
        pssh_data.algorithm = WidevinePsshData.AESCTR
        pssh_data.key_ids.append(key_id)
        init_data = base64.b64encode(pssh_data.SerializeToString()).decode("utf-8")
        return PSSH.new(system_id=PSSH.SystemId.Widevine, init_data=init_data)

    async def _get_license(self, challenge: bytes, license_url: str, uri: str, item_id: str) -> str:
        """Request a Widevine license from Apple Music."""
        challenge_b64 = base64.b64encode(challenge).decode("utf-8")
        data = {
            "challenge": challenge_b64,
            "key-system": "com.widevine.alpha",
            "uri": uri,
            "adamId": item_id,
            "isLibrary": False,
            "user-initiated": True,
        }
        async with self.provider.mass.http_session.post(
            license_url,
            data=json.dumps(data),
            headers=self._decryption_headers,
            ssl=False,
        ) as response:
            response.raise_for_status()
            content = await response.json(loads=json_loads)
            track_license = content.get("license")
            if not track_license:
                raise MediaNotFoundError(f"No license found for song {item_id}.")
            return cast("str", track_license)
