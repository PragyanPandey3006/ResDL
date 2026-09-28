# Batch download checkpointing.
#
# Persists the progress of a /bdl (batch download) run so that if the bot is
# restarted mid-batch, it can resume from the last unprocessed message id
# instead of starting the whole range over.
#
# One checkpoint file per (user, chat) is stored under batch_checkpoints/.
# The file is updated as the batch advances and removed when the batch
# finishes, is cancelled, or fails to start.

import os
import json
import threading

from logger import LOGGER

CHECKPOINT_DIR = "batch_checkpoints"
_lock = threading.Lock()

os.makedirs(CHECKPOINT_DIR, exist_ok=True)


def _checkpoint_path(user_id, chat_id) -> str:
    # chat_id can be negative (e.g. -100...) so sanitize for a filename.
    safe_chat = str(chat_id).replace("-", "m")
    return os.path.join(CHECKPOINT_DIR, f"{user_id}_{safe_chat}.json")


def save_checkpoint(
    user_id,
    origin_chat_id,
    command_message_id,
    chat_id,
    prefix,
    start_id,
    end_id,
    next_id,
    downloaded,
    skipped,
    failed,
):
    """Create/update the checkpoint for a running batch.

    ``next_id`` is the *next* message id that still needs processing. On
    resume, the batch continues from ``next_id``.
    """
    data = {
        "user_id": user_id,
        "origin_chat_id": origin_chat_id,
        "command_message_id": command_message_id,
        "chat_id": chat_id,
        "prefix": prefix,
        "start_id": start_id,
        "end_id": end_id,
        "next_id": next_id,
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
    }
    path = _checkpoint_path(user_id, chat_id)
    try:
        with _lock:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
    except Exception as e:
        LOGGER(__name__).warning(f"Failed to save batch checkpoint {path}: {e}")


def load_checkpoint(user_id, chat_id):
    """Return the checkpoint dict for a (user, chat), or None if absent."""
    path = _checkpoint_path(user_id, chat_id)
    if not os.path.exists(path):
        return None
    try:
        with _lock, open(path) as f:
            return json.load(f)
    except Exception as e:
        LOGGER(__name__).warning(f"Failed to load batch checkpoint {path}: {e}")
        return None


def clear_checkpoint(user_id, chat_id):
    """Remove the checkpoint once the batch is done/cancelled."""
    path = _checkpoint_path(user_id, chat_id)
    try:
        with _lock:
            if os.path.exists(path):
                os.remove(path)
    except Exception as e:
        LOGGER(__name__).warning(f"Failed to clear batch checkpoint {path}: {e}")


def list_pending_checkpoints():
    """Return a list of all pending checkpoint dicts (used on startup)."""
    pending = []
    try:
        for filename in os.listdir(CHECKPOINT_DIR):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(CHECKPOINT_DIR, filename)
            try:
                with _lock, open(path) as f:
                    pending.append(json.load(f))
            except Exception as e:
                LOGGER(__name__).warning(f"Skipping unreadable checkpoint {path}: {e}")
    except Exception as e:
        LOGGER(__name__).warning(f"Failed to list batch checkpoints: {e}")
    return pending
