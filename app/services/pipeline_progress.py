"""
In-memory pipeline progress tracking.
Provides a single shared state updated by scheduler jobs and read by the API.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class _StageResult:
    name: str
    status: str          # "ok" | "warn" | "error" | "skipped"
    detail: str
    warnings: list = field(default_factory=list)


@dataclass
class _State:
    running: bool = False
    pct: int = 0
    stage: str = ""
    message: str = ""
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    stages: list = field(default_factory=list)   # list[_StageResult]


_state = _State()


def start() -> None:
    """Mark pipeline as started, reset all fields."""
    _state.running = True
    _state.pct = 0
    _state.stage = "starting"
    _state.message = "Starting pipeline…"
    _state.error = None
    _state.started_at = datetime.now(timezone.utc)
    _state.finished_at = None
    _state.stages = []


def update(pct: int, stage: str, message: str = "") -> None:
    """Update progress mid-run."""
    _state.pct = max(0, min(99, pct))
    _state.stage = stage
    _state.message = message


def log_stage(name: str, status: str, detail: str, warnings: list | None = None) -> None:
    """Record a completed stage result."""
    _state.stages.append(_StageResult(
        name=name,
        status=status,
        detail=detail,
        warnings=warnings or [],
    ))


def finish(error: Optional[str] = None) -> None:
    """Mark pipeline as finished (success or error)."""
    _state.running = False
    _state.finished_at = datetime.now(timezone.utc)
    if error:
        _state.error = error
        _state.stage = "error"
        _state.message = error
    else:
        _state.pct = 100
        _state.stage = "done"
        _state.message = "Pipeline complete"
        _state.error = None


def get() -> dict:
    """Return current state as a dict (safe for JSON serialisation)."""
    elapsed = None
    if _state.started_at and _state.finished_at:
        elapsed = round((_state.finished_at - _state.started_at).total_seconds())
    return {
        "running": _state.running,
        "pct": _state.pct,
        "stage": _state.stage,
        "message": _state.message,
        "error": _state.error,
        "elapsed_seconds": elapsed,
        "stages": [
            {
                "name": s.name,
                "status": s.status,
                "detail": s.detail,
                "warnings": s.warnings,
            }
            for s in _state.stages
        ],
    }
