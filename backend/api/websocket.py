"""
VIP Pipeline — WebSocket progress broadcaster.

All pipeline stages push events here. The frontend connects once and
receives live updates without polling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.profiles import get_active_profile, get_current_profile_id, get_profile

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Connection registry — multiple browser tabs can connect simultaneously
# ---------------------------------------------------------------------------
_connections: list[tuple[WebSocket, str]] = []


async def broadcast(event: str, profile_id: str | None = None, **kwargs: Any) -> None:
    """Send a JSON event to all connected WebSocket clients."""
    target_profile_id = profile_id or get_current_profile_id()
    payload = json.dumps({"event": event, **kwargs})
    dead: list[tuple[WebSocket, str]] = []
    for ws, ws_profile_id in _connections:
        if ws_profile_id != target_profile_id:
            continue
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append((ws, ws_profile_id))
    for item in dead:
        if item in _connections:
            _connections.remove(item)


@router.websocket("/ws/progress")
async def progress_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint — frontend connects here for live pipeline progress."""
    requested_profile_id = websocket.query_params.get("profile_id")
    profile = get_profile(requested_profile_id) if requested_profile_id else get_active_profile()
    if requested_profile_id and profile is None:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    _connections.append((websocket, profile.id))
    logger.info("WebSocket client connected (%d total)", len(_connections))

    async def _ping_loop() -> None:
        """Send a keepalive ping every 30 s while this connection is open."""
        try:
            while True:
                await asyncio.sleep(30)
                await websocket.send_text(json.dumps({"event": "ping"}))
        except Exception:
            pass  # client gone — receive loop will clean up

    ping_task = asyncio.create_task(_ping_loop())
    try:
        # Block here; receive() raises WebSocketDisconnect when the client leaves.
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        ping_task.cancel()
        item = (websocket, profile.id)
        if item in _connections:
            _connections.remove(item)
        logger.info("WebSocket client disconnected (%d remaining)", len(_connections))
