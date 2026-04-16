"""
Realtime voice — WebRTC transport.

Wraps Pipecat's SmallWebRTCTransport (built on aiortc) so the browser can
negotiate peer-to-peer audio with the backend over a single SDP offer/answer
exchange. No TURN/STUN infra required for local/self-hosted deployments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.voice.providers import VoiceConfig, build_vad_analyzer, build_turn_analyzer

logger = logging.getLogger(__name__)


@dataclass
class TransportHandle:
    """Opaque handle returned by `create_transport`."""
    transport: Any                # SmallWebRTCTransport
    peer_connection: Any          # SmallWebRTCConnection
    answer_sdp: str
    answer_type: str              # "answer"


async def create_transport(
    offer_sdp: str,
    offer_type: str,
    cfg: VoiceConfig,
    ice_servers: Optional[list[dict]] = None,
) -> TransportHandle:
    """
    Create a SmallWebRTCTransport from an SDP offer, returning the local
    SDP answer for the browser to apply.

    Args:
        offer_sdp: RTCSessionDescription.sdp from the browser
        offer_type: usually "offer"
        cfg: resolved VoiceConfig
        ice_servers: optional ICE server list (fallbacks to no-servers)
    """
    # Deferred imports — Pipecat is an optional runtime dep
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.network.small_webrtc import (
        SmallWebRTCConnection,
        SmallWebRTCTransport,
    )

    connection = SmallWebRTCConnection(ice_servers=ice_servers or [])
    await connection.initialize(sdp=offer_sdp, type=offer_type)

    params = TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=build_vad_analyzer(cfg),
        turn_analyzer=build_turn_analyzer(cfg),
    )

    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=params,
    )

    answer = connection.get_answer()
    if answer is None:
        raise RuntimeError("Failed to generate SDP answer")

    return TransportHandle(
        transport=transport,
        peer_connection=connection,
        answer_sdp=answer["sdp"],
        answer_type=answer["type"],
    )
