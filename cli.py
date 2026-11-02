import argparse
import copy
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from . import benchmark, runtime
from .common import ROOT, config, request
from .diagnostics import inspect_stack
from .environment import cmake_configure, default_backend, find_binary, resolve_path, source_directory


def path(value):
    return resolve_path(value)


def binary(cfg, name):
    return find_binary(cfg, name)


def save_setting(key, value):
    file = ROOT / ".env"
    lines = file.read_text(encoding="utf-8").splitlines()
    replacement = f"{key}={value}"
    if any(line.startswith(key + "=") for line in lines):
        lines = [replacement if line.startswith(key + "=") else line for line in lines]
    else:
        lines.append(replacement)
    file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def setup():
    for directory in (".runtime", "results", "models"):
        (ROOT / directory).mkdir(exist_ok=True)
    file = ROOT / ".env"
    if not file.exists():
        shutil.copyfile(ROOT / ".env.example", file)
        if os.name != "nt":
            file.chmod(0o600)
    cfg = config()
    if not cfg.get("GRAFANA_ADMIN_PASSWORD"):
        save_setting("GRAFANA_ADMIN_PASSWORD", secrets.token_urlsafe(24))
    for key, value in (("HOST_UID", os.getuid() if hasattr(os, "getuid") else 1000),
                       ("HOST_GID", os.getgid() if hasattr(os, "getgid") else 1000)):
        if not cfg.get(key):
            save_setting(key, str(value))
    print("Configurazione pronta. Credenziali Grafana in .env.")


def docker_compose(arguments, check=True):
    executable = shutil.which("docker") or shutil.which("docker.exe")
    if not executable:
        raise ValueError("Docker non trovato. Installa/avvia Docker Desktop e abilita l'integrazione WSL, oppure Docker Engine + Compose v2 su Linux.")
    return subprocess.run([executable, "compose", "--project-directory", str(ROOT), "--env-file", str(ROOT / ".env"), *arguments],
                          cwd=ROOT, check=check)


def install(args, cfg):
    setup()
    source = path(args.source) if args.source else source_directory(cfg)
    lookup = dict(cfg)
    if args.source:
        lookup.update(LLAMA_CPP_DIR=str(source), LLAMA_BIN_DIR="")
    if args.bin_dir:
        lookup["LLAMA_BIN_DIR"] = str(path(args.bin_dir))
    existing = {}
    if args.backend in ("auto", "existing"):
        try:
            existing = {name: binary(lookup, name) for name in ("llama-server", "llama-bench")}
        except ValueError:
            if args.backend == "existing":
                raise
    if existing:
        for name in ("llama-server", "llama-bench"):
            exe = existing[name]
            result = subprocess.run([str(exe), "--version" if name == "llama-server" else "--help"],
                                    capture_output=True, text=True, check=True)
            print(f"OK {name}: {exe}", flush=True)
            if name == "llama-server":
                print(result.stderr.strip() or result.stdout.strip(), flush=True)
        if args.source:
            save_setting("LLAMA_CPP_DIR", str(source))
        save_setting("LLAMA_BIN_DIR", str(existing["llama-server"].parent))
        print("Installazione esistente verificata e collegata al progetto.")
        return 0
    backend = default_backend() if args.backend == "auto" else args.backend
    if not source.is_dir():
        if not args.clone:
            raise ValueError(f"Sorgenti assenti: {source}. Passa --source PERCORSO o --clone per scaricarli in .runtime.")
        source = ROOT / ".runtime" / "llama.cpp"
        if source.exists():
            raise ValueError(f"La destinazione clone esiste già: {source}; verifica i sorgenti senza sovrascriverli")
        command = ["git", "clone", "--depth", "1"]
        if args.ref:
            command += ["--branch", args.ref]
        subprocess.run([*command, "https://github.com/ggml-org/llama.cpp.git", str(source)], check=True)
    if not (source / "CMakeLists.txt").is_file():
        raise ValueError("La directory indicata non contiene i sorgenti llama.cpp")
    build = ROOT / ".runtime" / ("llama-build-" + backend)
    command = cmake_configure(source, build, backend)
    subprocess.run(command, check=True)
    subprocess.run(["cmake", "--build", str(build), "--config", "Release", "--parallel", str(args.jobs),
                    "--target", "llama-server", "llama-bench"], check=True)
    bindir = build / "bin"
    if (bindir / "Release").is_dir():
        bindir /= "Release"
    save_setting("LLAMA_CPP_DIR", str(source))
    save_setting("LLAMA_BIN_DIR", str(bindir))
    print(f"llama.cpp compilato: {bindir}")
    return 0


def doctor(cfg):
    problems = 0
    for name, args in (("llama-server", ["--version"]), ("llama-bench", ["--help"])):
        try:
            exe = binary(cfg, name)
            result = subprocess.run([str(exe), *args], capture_output=True, text=True, timeout=15)
            print(f"{'OK' if result.returncode == 0 else 'ERRORE'} {name}: {exe}")
            if result.returncode:
                problems += 1
            if result.stderr.strip():
                print("  " + result.stderr.strip().splitlines()[0])
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            print(f"ERRORE {name}: {exc}")
            problems += 1
    try:
        result = docker_compose(["config", "--quiet"], check=False)
        problems += int(result.returncode != 0)
        if result.returncode == 0:
            result = subprocess.run([shutil.which("docker") or shutil.which("docker.exe"), "info"], capture_output=True, timeout=15)
            problems += int(result.returncode != 0)
            print("OK Docker daemon" if result.returncode == 0 else "ERRORE Docker daemon non raggiungibile")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERRORE Docker: {exc}")
        problems += 1
    base = cfg.get("LLAMA_LOCAL_URL", "http://127.0.0.1:8080").rstrip("/")
    for endpoint in ("health", "metrics"):
        target = base + "/" + endpoint
        if cfg.get("LLAMA_METRICS_MODEL"):
            target += "?" + urllib.parse.urlencode({"model": cfg["LLAMA_METRICS_MODEL"]})
        try:
            with request(target, key=cfg.get("LLAMA_API_KEY", "")) as response:
                response.read()
            print(f"OK {target}")
        except OSError as exc:
            print(f"NON DISPONIBILE {target}: {exc}")
            problems += 1
    print("Collector host:", "attivo" if runtime.state("collector") else "fermo (up/collect per avviarlo)")
    return int(bool(problems))


def serve(args, cfg):
    if runtime.state("server"):
        raise ValueError("Server già attivo. Usa stop prima di cambiare modello.")
    model = path(args.model)
    if not model.is_file():
        raise ValueError(f"Modello non trovato: {model}")
    command = [str(binary(cfg, "llama-server")), "-m", str(model), "--metrics", "--host", args.host,
               "--port", str(args.port), "-c", str(args.context), "-ngl", str(args.gpu_layers), "-np", str(args.parallel)]
    if args.threads:
        command += ["-t", str(args.threads)]
    if args.alias:
        command += ["--alias", args.alias]
    if cfg.get("LLAMA_API_KEY"):
        # Read by llama-server from its environment, not exposed in process arguments/logs.
        os.environ["LLAMA_API_KEY"] = cfg["LLAMA_API_KEY"]
    runtime.launch("server", command)
    print("Server avviato. Log: .runtime/server.log", flush=True)
    if args.no_wait:
        return 0
    host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if not runtime.state("server"):
            raise ValueError("llama-server è terminato: vedi .runtime/server.log")
        try:
            with request(f"http://{host}:{args.port}/health", key=cfg.get("LLAMA_API_KEY", ""), timeout=2) as response:
                response.read()
            print(f"Server pronto su http://{host}:{args.port}")
            return 0
        except OSError:
            time.sleep(0.5)
    raise ValueError("Caricamento ancora in corso; controlla .runtime/server.log o usa stop.")


def positive(value):
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("Deve essere maggiore di zero")
    return result


def main():
    parser = argparse.ArgumentParser(description="llmMonitor — Grafana, llama.cpp e benchmark riproducibili")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Crea cartelle e .env con password Grafana casuale")
    installer = commands.add_parser("install", help="Collega llama.cpp esistente o compila in .runtime")
    installer.add_argument("--backend", choices=["auto", "existing", "cpu", "cuda", "metal"], default="auto")
    installer.add_argument("--source")
    installer.add_argument("--bin-dir", help="Cartella di una installazione esistente, anche fuori dai sorgenti")
    installer.add_argument("--clone", action="store_true")
    installer.add_argument("--ref", help="Tag/branch per --clone")
    installer.add_argument("--jobs", type=positive, default=min(8, os.cpu_count() or 2))
    commands.add_parser("doctor", help="Verifica binari, Docker e endpoint")
    commands.add_parser("diagnose", help="Controlla dashboard, scrape e dati reali in Prometheus")
    commands.add_parser("build", help="Build delle immagini Docker")
    up = commands.add_parser("up", help="Avvia monitoring e collector host")
    up.add_argument("--no-collector", action="store_true")
    up.add_argument("--pid", type=positive, help="PID di un llama-server esterno per metriche processo Linux")
    down = commands.add_parser("down", help="Ferma Docker e collector; conserva dati e server")
    down.add_argument("--server", action="store_true", help="Ferma anche il server gestito dal progetto")
    collect = commands.add_parser("collect", help="Avvia solo il collector hardware host")
    collect.add_argument("--pid", type=positive)
    commands.add_parser("collect-stop")
    commands.add_parser("status")
    logs = commands.add_parser("logs")
    logs.add_argument("service", nargs="?", choices=["grafana", "prometheus", "exporter", "server", "collector"])
    server = commands.add_parser("serve", help="Avvia llama-server in background con --metrics")
    server.add_argument("--model", required=True)
    server.add_argument("--alias", help="ID del modello esposto dalla API")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=positive, default=8080)
    server.add_argument("--context", type=positive, default=4096)
    server.add_argument("--parallel", type=positive, default=1)
    server.add_argument("--gpu-layers", type=int, default=-1)
    server.add_argument("--threads", type=positive)
    server.add_argument("--wait", type=positive, default=120)
    server.add_argument("--no-wait", action="store_true")
    commands.add_parser("stop", help="Ferma il server gestito da questo progetto")
    bench = commands.add_parser("bench", help="Benchmark nativo: multipli modelli e matrici di parametri")
    bench.add_argument("--model", action="append", required=True)
    bench.add_argument("--prompt-tokens", default="512", help="Lista/range nativi llama-bench")
    bench.add_argument("--tokens", default="128")
    bench.add_argument("--repetitions", type=positive, default=5)
    bench.add_argument("--threads", default="8")
    bench.add_argument("--gpu-layers", default="-1")
    bench.add_argument("--batch", default="2048")
    demo = commands.add_parser("demo", help="Esegue run separate per confrontare configurazioni nella dashboard Benchmark")
    demo.add_argument("--model", required=True)
    demo.add_argument("--threads", default="2,4", help="Una run per ciascun numero di thread")
    demo.add_argument("--gpu-layers", default="0")
    demo.add_argument("--prompt-tokens", default="64")
    demo.add_argument("--tokens", default="16")
    demo.add_argument("--batch", default="128")
    demo.add_argument("--repetitions", type=positive, default=2)
    load = commands.add_parser("load", help="Benchmark HTTP streaming /completion sul server attivo")
    load.add_argument("--url")
    load.add_argument("--model", default="", help="Alias server/router da registrare e inviare alla API")
    load.add_argument("--prompt", default="Spiega in italiano come funziona un modello linguistico.")
    load.add_argument("--prompt-file")
    load.add_argument("--tokens", type=positive, default=128)
    load.add_argument("--requests", type=positive, default=20)
    load.add_argument("--concurrency", type=positive, default=2)
    load.add_argument("--timeout", type=positive, default=120)
    load.add_argument("--seed", type=int, default=42)
    load.add_argument("--warmup", action=argparse.BooleanOptionalAction, default=True)
    compare = commands.add_parser("compare", help="Confronta run completate, esporta CSV")
    compare.add_argument("--csv")
    args = parser.parse_args()
    cfg = config()
    try:
        if args.command == "setup":
            setup()
        elif args.command == "install":
            return install(args, cfg)
        elif args.command == "doctor":
            return doctor(cfg)
        elif args.command == "diagnose":
            report = inspect_stack(cfg)
            for check in report["checks"]:
                print(f"{'OK' if check['ok'] else 'ERRORE'} {check['component']}: {check['detail']}")
            print("Report: .runtime/diagnostics.json")
            print("Home:", report["dashboards"]["home"])
            return int(any(not check["ok"] for check in report["checks"]))
        elif args.command in ("build", "up"):
            setup()
            docker_compose(["build"] if args.command == "build" else ["up", "-d", "--build", "--wait", "--wait-timeout", "120"])
            if args.command == "up":
                if not args.no_collector:
                    current = runtime.state("collector")
                    if current and (current.get("code_version") != runtime.code_version() or
                                    (args.pid is not None and current.get("watched_pid") != args.pid)):
                        runtime.stop("collector")
                        current = None
                    if not current:
                        runtime.launch("collector", pid=args.pid)
                print(f"Grafana: http://localhost:{cfg.get('GRAFANA_PORT', '3000')} (credenziali in .env)")
                print(f"Home: http://localhost:{cfg.get('GRAFANA_PORT', '3000')}/d/llmmonitor-home")
                if not runtime.state("server"):
                    print("Nessun server gestito attivo: usa serve --model FILE o collega un server esterno con --metrics. Hardware e benchmark salvati sono indipendenti dal server.")
        elif args.command == "down":
            docker_compose(["down"])
            runtime.stop("collector")
            if args.server:
                runtime.stop("server")
        elif args.command == "collect":
            setup()
            runtime.launch("collector", pid=args.pid)
            print("Collector avviato.")
        elif args.command == "collect-stop":
            runtime.stop("collector")
        elif args.command == "serve":
            setup()
            return serve(args, cfg)
        elif args.command == "stop":
            runtime.stop("server")
        elif args.command == "status":
            for name in ("server", "collector"):
                print(name + ": " + json.dumps(runtime.state(name) or {"status": "fermo"}))
            docker_compose(["ps"])
        elif args.command == "logs":
            if args.service in ("server", "collector"):
                file = ROOT / ".runtime" / f"{args.service}.log"
                print(file.read_text(encoding="utf-8", errors="replace")[-20000:] if file.exists() else "Nessun log")
            else:
                docker_compose(["logs", "--tail", "100", *([args.service] if args.service else [])])
        elif args.command == "bench":
            return benchmark.offline(ROOT, binary(cfg, "llama-bench"), args.model, args)
        elif args.command == "demo":
            try:
                threads = [str(positive(value.strip())) for value in args.threads.split(",")]
            except argparse.ArgumentTypeError as exc:
                raise ValueError("I thread della demo devono essere interi positivi") from exc
            print(f"Dashboard: http://localhost:{cfg.get('GRAFANA_PORT', '3000')}/d/llmmonitor-benchmarks", flush=True)
            code = 0
            for count in threads:
                case = copy.copy(args)
                case.threads = count
                code |= benchmark.offline(ROOT, binary(cfg, "llama-bench"), [args.model], case)
            benchmark.compare(ROOT, ROOT / "results" / "comparison.csv")
            return code
        elif args.command == "load":
            args.url = args.url or cfg.get("LLAMA_LOCAL_URL", "http://127.0.0.1:8080")
            return benchmark.http_benchmark(ROOT, args, cfg.get("LLAMA_API_KEY", ""))
        elif args.command == "compare":
            return benchmark.compare(ROOT, args.csv)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrotto.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
