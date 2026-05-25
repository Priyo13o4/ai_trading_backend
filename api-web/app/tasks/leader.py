import os
import fcntl
import logging

logger = logging.getLogger(__name__)

_janitor_leader_lock_handle = None
_janitor_is_leader = False

def try_acquire_janitor_leader_lock() -> bool:
    global _janitor_leader_lock_handle, _janitor_is_leader

    if _janitor_is_leader:
        return True

    lock_path = (os.getenv("JANITOR_LEADER_LOCK_PATH") or "/tmp/fastapi_janitor_leader.lock").strip() or "/tmp/fastapi_janitor_leader.lock"
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_handle.seek(0)
        lock_handle.truncate()
        lock_handle.write(str(os.getpid()))
        lock_handle.flush()
        _janitor_leader_lock_handle = lock_handle
        _janitor_is_leader = True
        logger.info("[JANITOR] Acquired leader lock path=%s pid=%s", lock_path, os.getpid())
        return True
    except BlockingIOError:
        lock_handle.close()
        _janitor_is_leader = False
        logger.info("[JANITOR] Leader lock held by another worker path=%s", lock_path)
        return False
    except Exception:
        lock_handle.close()
        _janitor_is_leader = False
        logger.exception("[JANITOR] Failed to acquire leader lock path=%s", lock_path)
        return False

def release_janitor_leader_lock() -> None:
    global _janitor_leader_lock_handle, _janitor_is_leader

    handle = _janitor_leader_lock_handle
    _janitor_leader_lock_handle = None
    _janitor_is_leader = False
    if handle is None:
        return

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        logger.exception("[JANITOR] Failed to unlock leader lock")
    finally:
        try:
            handle.close()
        except Exception:
            logger.exception("[JANITOR] Failed to close leader lock handle")
