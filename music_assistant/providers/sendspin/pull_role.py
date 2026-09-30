"""Negotiated Sendspin extension for private progressive playback handoff."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from aiosendspin.models.types import ServerMessage
from aiosendspin.server.roles.base import Role

if TYPE_CHECKING:
    from aiosendspin.server.client import SendspinClient

    from .provider import SendspinProvider


PULL_ROLE_ID = "pull@v1"
PULL_PROGRESS_BINARY_TYPE = 24


@dataclass
class PullPlayPayload:
    """The single-item stream selected for a pull-capable client."""

    url: str
    token: str
    item_id: str
    queue_id: str
    start_position_ms: int
    duration_ms: int | None


@dataclass
class PullPlayMessage(ServerMessage):
    """Start a progressive FLAC item on the client."""

    payload: PullPlayPayload
    type: Literal["pull/play"] = "pull/play"


@dataclass
class PullControlPayload:
    """Control the active progressive item."""

    command: Literal["stop", "pause", "resume"]


@dataclass
class PullControlMessage(ServerMessage):
    """Stop, pause, or resume a progressive item on the client."""

    payload: PullControlPayload
    type: Literal["pull/control"] = "pull/control"


class PullRole(Role):
    """Receive playback position from a pull-capable Sendspin client."""

    def __init__(self, client: SendspinClient, provider: SendspinProvider) -> None:
        """Bind the negotiated role to its client and MA provider."""
        self._client = client
        self._provider = provider

    @property
    def role_id(self) -> str:
        """Return the negotiated extension role."""
        return PULL_ROLE_ID

    @property
    def role_family(self) -> str:
        """Return the extension role family."""
        return "pull"

    def handles_inbound_binary(self, message_type: int) -> bool:
        """Accept only pull position reports."""
        return message_type == PULL_PROGRESS_BINARY_TYPE

    def on_binary_chunk(self, message_type: int, timestamp_us: int, data: bytes) -> None:
        """Forward an item-scoped position report to its MA player."""
        if message_type != PULL_PROGRESS_BINARY_TYPE or len(data) < 10:
            return
        player = self._provider.mass.players.get_player(self._client.client_id)
        if player is None or (on_progress := getattr(player, "on_pull_progress", None)) is None:
            return
        try:
            item_id = data[9:].decode("utf-8")
        except UnicodeDecodeError:
            return
        on_progress(item_id, int.from_bytes(data[1:9], "big"), data[0])
