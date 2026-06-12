"""
Windows: force the Claude Agent SDK's spawned claude.exe to run windowless.

Every script here that calls ``claude_agent_sdk.query()`` spawns the bundled
``claude.exe`` via ``anyio.open_process()`` WITHOUT a creation flag. When the
parent script was launched ``DETACHED_PROCESS`` (truly console-less), Windows
hands that console child a brand-new VISIBLE window. All spawn sites here now
use ``CREATE_NO_WINDOW`` (hidden console, inherited by children) instead --
2026-06-11 fix in flush.py's compile trigger -- but keep this patch as the
belt-and-suspenders layer for manual or console-less launches.

Single source: do NOT copy this monkeypatch inline into each script. Import and
call ``apply()`` at module top, before any SDK call. This file is the one place
the patch lives, so a future SDK-calling script can't half-reintroduce the stray
"claude" window -- the exact bug that resurfaced 2026-06-07 in compile.py after
only flush.py had been patched.

Usage (module top, before the first query()):
    from sdk_nowindow import apply as silence_sdk_windows
    silence_sdk_windows()
"""

from __future__ import annotations

import sys

_PATCHED = False


def apply() -> None:
    """Idempotently wrap anyio.open_process to OR-in CREATE_NO_WINDOW on Windows.

    Safe to call multiple times and on non-Windows platforms (no-op there).
    The SDK does ``import anyio`` then ``anyio.open_process(...)`` (late
    attribute lookup), so reassigning the module attribute here is picked up by
    both the main transport spawn and the SDK's ``claude -v`` version check.
    """
    global _PATCHED
    if _PATCHED or sys.platform != "win32":
        return

    import subprocess as _subprocess

    import anyio as _anyio

    _orig_open_process = _anyio.open_process

    async def _no_window_open_process(*args, **kwargs):
        kwargs["creationflags"] = (
            kwargs.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
        )
        return await _orig_open_process(*args, **kwargs)

    _anyio.open_process = _no_window_open_process
    _PATCHED = True
