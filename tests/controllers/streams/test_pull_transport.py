"""Progressive Sendspin handoff and stream-token tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.player import PlayerMedia

from music_assistant.controllers.streams.controller import StreamsController
from music_assistant.providers.sendspin.playback import SendspinPlaybackSession
from music_assistant.providers.sendspin.player import SendspinPlayer


def test_pull_url_is_item_scoped_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the exact issued item and player can use an unpaced stream token."""
    monkeypatch.setenv("MA_PULL_PUBLIC_BASE_URL", "https://music.example/streamserver/pull")
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    controller = StreamsController(mass)
    media = PlayerMedia(
        uri="library://track/item",
        queue_session_id="session",
        source_id="queue",
        queue_item_id="item",
    )
    url, token = controller.resolve_pull_stream_url("player", media)
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.path == "/streamserver/pull/single/session/queue/item/player.flac"

    assert not parsed.query
    match_info = {
        "session_id": "session",
        "queue_id": "queue",
        "queue_item_id": "item",
        "player_id": "player",
    }
    request = cast(
        "web.Request",
        SimpleNamespace(headers={"X-MA-Pull-Token": token}, match_info=match_info),
    )
    assert controller._valid_pull_request(request)
    with pytest.raises(web.HTTPForbidden):
        controller._valid_pull_request(
            cast(
                "web.Request",
                SimpleNamespace(
                    headers=request.headers, match_info={**match_info, "player_id": "other"}
                ),
            )
        )
    with (
        patch("music_assistant.controllers.streams.controller.time.time", return_value=10**12),
        pytest.raises(web.HTTPForbidden),
    ):
        controller._valid_pull_request(request)


@pytest.mark.parametrize(
    ("domain", "realtime", "group_size", "expected"),
    [
        ("filesystem_local", False, 1, True),
        ("apple_music", False, 1, True),
        ("deezer", False, 1, True),
        ("tidal", False, 1, True),
        ("qobuz", False, 1, True),
        ("soundcloud", False, 1, True),
        ("spotify", False, 1, False),
        ("ytmusic", True, 1, False),
        ("tidal", True, 1, False),
        ("tidal", False, 2, False),
    ],
)
def test_pull_routing_requires_capability_approved_source_and_no_group(
    domain: str, realtime: bool, group_size: int, expected: bool
) -> None:
    """Spotify and paced or grouped sources retain the Sendspin push pipeline."""
    player = MagicMock()
    player.synced_to = None
    player.api.group.clients = [object()] * group_size
    player.api.role.return_value = object()
    player.mass.player_queues.get.return_value.crossfade_enabled = False
    player.mass.player_queues.get_item.return_value.streamdetails.provider = "source_instance"
    player.mass.player_queues.get_item.return_value.streamdetails.is_realtime = realtime
    player.mass.get_provider.return_value.domain = domain
    session = SendspinPlaybackSession(player)
    media = PlayerMedia(
        uri="library://track/item",
        media_type=MediaType.TRACK,
        source_id="queue",
        queue_item_id="item",
    )
    with patch("music_assistant.providers.sendspin.playback.overlay_active", return_value=False):
        assert session._use_pull_transport(media) is expected
        player.api.role.return_value = None
        assert not session._use_pull_transport(media)


@pytest.mark.asyncio
async def test_failed_pull_report_stops_current_item_once() -> None:
    """A failed pull leaves the item available for a fresh Play request."""
    player = MagicMock()
    player.player_id = "player"
    player.playback_session.pull_item_id = "item"
    player._pull_terminal_item_id = None
    player.mass.player_queues.stop = AsyncMock()

    SendspinPlayer.on_pull_progress(player, "item", 1500, 4)
    assert player._attr_playback_state == PlaybackState.IDLE
    assert player._attr_elapsed_time == 1.5
    await player.mass.create_task.call_args.args[0]
    player.mass.player_queues.stop.assert_awaited_once_with("player")

    SendspinPlayer.on_pull_progress(player, "item", 1500, 4)
    SendspinPlayer.on_pull_progress(player, "previous_item", 1500, 4)
    player.mass.create_task.assert_called_once()
