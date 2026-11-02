import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def console_options():
    """Console tools spawned by a Windows background worker must stay hidden."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def config(root=ROOT):
    values = {}
    file = root / ".env"
    if file.exists():
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep:
                raise ValueError(f"Riga .env non valida: {line}")
            values[key.strip()] = value.strip().strip("\"").strip("'")
    values.update(os.environ)
    return values


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
        # Windows readers (including Docker bind mounts) can briefly hold a file
        # without delete sharing. Retry the atomic rename, never truncate it.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.025)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def request(url, data=None, key="", timeout=3, headers=None):
    headers = dict(headers or {})
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=None if data is None else json.dumps(data).encode(), headers=headers)
    # Local inference should not accidentally traverse a system HTTP proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)
