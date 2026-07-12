from __future__ import annotations

import datetime as dt
import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def workspace_lock(path: Path, *, label: str) -> Iterator[None]:
    """Hold a non-blocking OS file lock for one orchestrated workspace run.

    The operating system releases the lock after a crash. The small metadata
    file is informational and is overwritten by the next successful owner.
    Direct per-domain commands remain available for intentional parallel work.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("a+b")
    except OSError as error:
        raise RuntimeError(
            f"Another orchestrated pipeline may already be using {label}."
        ) from error
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        try:
            owner = path.read_text(encoding="utf-8", errors="replace").strip("\0\r\n ")
        except OSError:
            owner = ""
        detail = f" Current owner: {owner}" if owner else ""
        raise RuntimeError(
            f"Another orchestrated pipeline is already using {label}.{detail}"
        ) from error

    metadata = {
        "label": label,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "acquired_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(metadata, sort_keys=True).encode("utf-8"))
    handle.flush()
    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        try:
            path.unlink()
        except OSError:
            pass
