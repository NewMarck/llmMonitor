"""Detached host workers controlled by per-launch tokens, without killing stored PIDs."""
import json
import hashlib
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .common import ROOT, atomic_json, console_options
from .hardware import Sampler


def code_version():
    digest = hashlib.sha256(sys.executable.encode())
    for name in ("common.py", "hardware.py", "runtime.py"):
        digest.update((Path(__file__).parent / name).read_bytes())
    return digest.hexdigest()


def state(name):
    try:
        data = json.loads((ROOT / ".runtime" / f"{name}.json").read_text())
        if not isinstance(data, dict):
            return None
        return data if time.time() - data.get("heartbeat", 0) < 10 else None
    except (OSError, ValueError, TypeError):
        return None


def launch(name, command=None, pid=None):
    if state(name):
        raise ValueError(f"{name} è già attivo; usa stop/down prima di riavviarlo")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    spec = runtime / f"launch-{token}.json"
    atomic_json(spec, dict(name=name, token=token, command=command, pid=pid, code_version=code_version()))
    options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    with (runtime / f"{name}.log").open("ab") as log:
        child = subprocess.Popen([sys.executable, "-m", "llmmonitor.runtime", str(spec)],
                                 cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
    for _ in range(100):
        data = state(name)
        if data and data.get("token") == token:
            return data
        if child.poll() is not None:
            raise ValueError(f"Avvio {name} fallito: vedi .runtime/{name}.log")
        time.sleep(0.1)
    raise ValueError(f"Avvio {name} non confermato: vedi .runtime/{name}.log")


def stop(name):
    data = state(name)
    if not data:
        return
    runtime = ROOT / ".runtime"
    marker = runtime / f"stop-{data['token']}"
    marker.touch()
    for _ in range(150):
        current = state(name)
        if not current or current.get("token") != data["token"]:
            return
        time.sleep(0.1)
    raise ValueError(f"Arresto {name} ancora in corso: vedi .runtime/{name}.log")


def worker(spec):
    data = json.loads(spec.read_text())
    spec.unlink()
    name, token = data["name"], data["token"]
    runtime = ROOT / ".runtime"
    file = runtime / f"{name}.json"
    marker = runtime / f"stop-{token}"
    child = None
    stopping = False

    def on_signal(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    sampler = Sampler()
    try:
        if data.get("command"):
            child = subprocess.Popen(data["command"], cwd=ROOT, **console_options())
        while not stopping and not marker.exists():
            if child and child.poll() is not None:
                raise ValueError(f"{name} terminato con codice {child.returncode}")
            atomic_json(file, dict(pid=child.pid if child else os.getpid(), worker_pid=os.getpid(), token=token,
                                   heartbeat=time.time(), command=data.get("command", []),
                                   code_version=data.get("code_version"), watched_pid=data.get("pid")))
            if name == "collector":
                server = state("server")
                benchmark = runtime / "benchmark.json"
                watched_pid = data.get("pid") or (server or {}).get("pid")
                if benchmark.exists():
                    try:
                        watched_pid = json.loads(benchmark.read_text())["pid"]
                    except (OSError, ValueError, KeyError):
                        pass
                atomic_json(runtime / "host.json", sampler.sample(watched_pid))
            time.sleep(0.5 if child else 2)
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        file.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


if __name__ == "__main__":
    worker(Path(sys.argv[1]))
