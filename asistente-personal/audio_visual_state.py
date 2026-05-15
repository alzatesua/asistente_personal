import json
import os
import tempfile
import time
import uuid


STATE_FILE = os.path.join(tempfile.gettempdir(), "asistente_audio_visual_state.json")


def _write_state(data):
    tmp_path = f"{STATE_FILE}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp_path, STATE_FILE)


def notify_audio_start(path, source="local"):
    if not path:
        return None

    token = uuid.uuid4().hex
    now = time.time()
    _write_state({
        "state": "playing",
        "token": token,
        "path": os.path.abspath(path),
        "source": source,
        "started_at": now,
        "updated_at": now,
    })
    return token


def notify_audio_stop(token=None):
    data = read_audio_state()
    if token and data.get("token") != token:
        return

    _write_state({
        "state": "idle",
        "token": token or data.get("token"),
        "path": data.get("path"),
        "source": data.get("source"),
        "started_at": data.get("started_at"),
        "updated_at": time.time(),
    })


def read_audio_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}
