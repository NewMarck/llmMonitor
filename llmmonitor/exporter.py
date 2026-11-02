"""Docker exporter: transparent llama metrics proxy and saved experiment metrics."""
import argparse
import json
import math
import os
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import request
from .benchmark import validate_records


def metric(name, value, labels=None):
    value = float(value)
    if not math.isfinite(value):
        return ""
    def escape(text):
        return str(text).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    suffix = "{" + ",".join(f'{key}="{escape(val)}"' for key, val in sorted((labels or {}).items())) + "}" if labels else ""
    return f"{name}{suffix} {value:.12g}\n"


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def local_metrics(results, runtime):
    output = [metric("llmmonitor_exporter_timestamp_seconds", time.time())]
    snapshot = read_json(runtime / "host.json")
    fresh = bool(snapshot and time.time() - snapshot.get("values", {}).get("sample_timestamp_seconds", 0) < 15)
    output.append(metric("llmmonitor_host_collector_up", int(fresh)))
    if fresh:
        for name, value in snapshot.get("values", {}).items():
            output.append(metric("llmmonitor_" + name, value))
        for gpu in snapshot.get("gpus", []):
            labels = {"gpu": gpu["index"], "name": gpu["name"]}
            for name, value in gpu.items():
                if name not in ("index", "name"):
                    output.append(metric("llmmonitor_gpu_" + name, value, labels))
    invalid = 0
    for file in sorted(results.glob("*/result.json")):
        result = read_json(file)
        try:
            if not isinstance(result, dict) or not isinstance(result.get("run_id"), str):
                raise ValueError("Invalid result envelope")
            if result.get("kind") not in ("offline", "http"):
                raise ValueError("Invalid result kind")
            if not isinstance(result.get("started_at"), (float, int)):
                raise ValueError("Invalid timestamp")
            if result.get("finished_at") is not None and not isinstance(result["finished_at"], (float, int)):
                raise ValueError("Invalid finish timestamp")
            if result.get("status") == "success" and result["kind"] == "offline":
                validate_records(result.get("records"))
            if result["kind"] == "http" and result.get("status") in ("success", "failed"):
                if not isinstance(result.get("summary"), dict) or not isinstance(result.get("parameters"), dict):
                    raise ValueError("Invalid HTTP result")
                if not all(name in result["parameters"] for name in ("concurrency", "tokens")):
                    raise ValueError("Missing HTTP parameters")
        except (ValueError, TypeError, KeyError):
            invalid += 1
            continue
        labels = {"run_id": result["run_id"], "kind": result["kind"], "model": result.get("model", "")}
        output.append(metric("llmmonitor_run_status", {"running": 0, "success": 1, "failed": -1, "interrupted": -2}.get(result.get("status"), -1), labels))
        output.append(metric("llmmonitor_run_started_timestamp_seconds", result["started_at"], labels))
        if result.get("finished_at"):
            output.append(metric("llmmonitor_run_finished_timestamp_seconds", result["finished_at"], labels))
        if result.get("status") not in ("success", "failed"):
            continue
        if result["kind"] == "offline" and result.get("status") == "success":
            for index, record in enumerate(result.get("records", [])):
                prompt, generation = record["n_prompt"], record["n_gen"]
                row_labels = dict(labels, case=str(index), test=f"pp{prompt}+tg{generation}",
                                  threads=str(record.get("n_threads", "")), gpu_layers=str(record.get("n_gpu_layers", "")),
                                  batch=str(record.get("n_batch", "")), build=str(record.get("build_commit", "")))
                for key in ("avg_ts", "stddev_ts", "avg_ns", "stddev_ns", "model_size", "model_n_params"):
                    if key in record:
                        output.append(metric("llmmonitor_bench_" + key, record[key], row_labels))
        elif result["kind"] == "http":
            labels.update(concurrency=str(result["parameters"]["concurrency"]), tokens=str(result["parameters"]["tokens"]))
            for key, value in result.get("summary", {}).items():
                if isinstance(value, (int, float)):
                    output.append(metric("llmmonitor_http_" + key, value, labels))
    output.append(metric("llmmonitor_invalid_result_files", invalid))
    return "".join(output).encode()


def make_handler(results, runtime, url, key="", model=""):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == "/health":
                    body, content_type = b'{"status":"ok"}', "application/json"
                elif self.path == "/metrics":
                    body, content_type = local_metrics(results, runtime), "text/plain; version=0.0.4"
                elif self.path == "/llama":
                    target = url.rstrip("/") + "/metrics"
                    if model:
                        target += "?" + urllib.parse.urlencode({"model": model})
                    with request(target, key=key, timeout=3) as response:
                        body = response.read()
                    # Preserve every native series, including new metrics and labels.
                    content_type = "text/plain; version=0.0.4"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
                # A failed native scrape must be marked DOWN by Prometheus.
                self.send_error(502, "Upstream o dati non disponibili")

        def log_message(self, fmt, *args):
            if args and str(args[1]) != "200":
                super().log_message(fmt, *args)

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=Path("results"))
    parser.add_argument("--runtime", type=Path, default=Path(".runtime"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9108)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(
        args.results, args.runtime, os.environ.get("LLAMA_URL", "http://127.0.0.1:8080"),
        os.environ.get("LLAMA_API_KEY", ""), os.environ.get("LLAMA_METRICS_MODEL", "")))
    server.serve_forever()


if __name__ == "__main__":
    main()
