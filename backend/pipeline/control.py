"""Pipeline runtime control primitives (pause/resume/stop) scoped per profile."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class _PipelineControl:
    pause_event: asyncio.Event
    stop_requested: bool = False


class PipelineStopRequested(RuntimeError):
    """Raised at cooperative checkpoints when a stop has been requested."""


_controls: dict[str, _PipelineControl] = {}


def _get(profile_id: str) -> _PipelineControl:
    control = _controls.get(profile_id)
    if control is None:
        control = _PipelineControl(pause_event=asyncio.Event())
        control.pause_event.set()
        _controls[profile_id] = control
    return control


def mark_pipeline_started(profile_id: str) -> None:
    """Reset control state for a newly started pipeline run."""
    control = _get(profile_id)
    control.pause_event.set()
    control.stop_requested = False


def mark_pipeline_finished(profile_id: str) -> None:
    """Ensure paused pipelines are unblocked after run completion/failure."""
    control = _get(profile_id)
    control.pause_event.set()
    control.stop_requested = False


def request_pause(profile_id: str) -> bool:
    control = _get(profile_id)
    if control.stop_requested:
        return False
    if not control.pause_event.is_set():
        return False
    control.pause_event.clear()
    return True


def request_resume(profile_id: str) -> bool:
    control = _get(profile_id)
    if control.stop_requested:
        return False
    if control.pause_event.is_set():
        return False
    control.pause_event.set()
    return True


def request_stop(profile_id: str) -> bool:
    control = _get(profile_id)
    if control.stop_requested:
        return False
    control.stop_requested = True
    # If currently paused, unblock immediately so the next checkpoint can exit.
    control.pause_event.set()
    return True


def is_paused(profile_id: str) -> bool:
    return not _get(profile_id).pause_event.is_set()


async def wait_if_paused(profile_id: str) -> None:
    """Checkpoint helper called by ingest phases to honor pause requests."""
    control = _get(profile_id)
    await control.pause_event.wait()
    if control.stop_requested:
        raise PipelineStopRequested("Pipeline stop requested")
