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

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Connection registry — multiple browser tabs can connect simultaneously
# ---------------------------------------------------------------------------
_connections: list[WebSocket] = []


async def broadcast(event: str, **kwargs: Any) -> None:
    """Send a JSON event to all connected WebSocket clients."""
    payload = json.dumps({"event": event, **kwargs})
    dead = []
    for ws in _connections:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.remove(ws)


@router.websocket("/ws/progress")
async def progress_ws(websocket: WebSocket) -> None:
    """WebSocket endpoint — frontend connects here for live pipeline progress."""
    await websocket.accept()
    _connections.append(websocket)
    logger.info("WebSocket client connected (%d total)", len(_connections))
    try:
        while True:
            # Keep the connection alive — we broadcast to clients, not the other way
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"event": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in _connections:
            _connections.remove(websocket)
        logger.info("WebSocket client disconnected (%d remaining)", len(_connections))
