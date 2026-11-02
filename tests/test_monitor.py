import contextlib
import io
import json
import http.client
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from llmmonitor import benchmark, exporter
from llmmonitor.common import ROOT, atomic_json
from llmmonitor.hardware import Sampler, psutil


class FakeLlama(BaseHTTPRequestHandler):
    mode = "ok"
    seen = []
    metrics = b'# HELP llamacpp:future_metric A new native metric\n# TYPE llamacpp:future_metric gauge\nllamacpp:future_metric{mode="test"} 42\n'

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.mode == "disabled":
            self.send_error(501)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.metrics)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, body))
        if self.mode == "error":
            self.send_error(500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b'data: {"content":"ciao","stop":false}\n\n')
        self.wfile.flush()
        time.sleep(0.01)
        if self.mode != "truncated":
            self.wfile.write(b'data: {"content":"","stop":true,"tokens_predicted":4,"timings":{"predicted_n":4}}\n\n')

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def serving(handler):
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    except PermissionError:
        # Restricted sandboxes may forbid sockets. Still execute HTTP parsing,
        # headers, status handling and handlers through an in-memory transport.
        with memory_serving(handler) as url:
            yield url
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


MEMORY_SERVERS = {}
MEMORY_SERIAL = 0


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = io.BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)


def memory_open(opener, fullurl, data=None, timeout=None):
    req = fullurl if isinstance(fullurl, urllib.request.Request) else urllib.request.Request(fullurl, data=data)
    parsed = urllib.parse.urlsplit(req.full_url)
    headers = dict(req.header_items())
    payload = req.data or b""
    headers.update({"Host": parsed.netloc, "Connection": "close", "Content-Length": str(len(payload))})
    target = parsed.path + ("?" + parsed.query if parsed.query else "")
    raw = (req.get_method() + " " + target + " HTTP/1.1\r\n" + "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n\r\n").encode() + payload
    connection = MemorySocket(raw)
    MEMORY_SERVERS[parsed.netloc](connection, ("127.0.0.1", 1), None)
    response = http.client.HTTPResponse(MemorySocket(connection.output.getvalue()))
    response.begin()
    if response.status >= 400:
        raise urllib.error.HTTPError(req.full_url, response.status, response.reason, response.headers, response)
    return response


@contextlib.contextmanager
def memory_serving(handler):
    global MEMORY_SERIAL
    MEMORY_SERIAL += 1
    address = f"127.0.0.1:{20000 + MEMORY_SERIAL}"
    MEMORY_SERVERS[address] = handler
    with patch.object(urllib.request.OpenerDirector, "open", memory_open):
        try:
            yield "http://" + address
        finally:
            del MEMORY_SERVERS[address]


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "results").mkdir()
        (self.root / ".runtime").mkdir()
        FakeLlama.mode = "ok"
        FakeLlama.seen = []

    def tearDown(self):
        self.temporary.cleanup()

    def test_native_proxy_keeps_unknown_series_labels_and_auth(self):
        with serving(FakeLlama) as upstream:
            handler = exporter.make_handler(self.root / "results", self.root / ".runtime", upstream, "secret", "a/b q4")
            with serving(handler) as proxy:
                self.assertEqual(urllib.request.urlopen(proxy + "/llama").read(), FakeLlama.metrics)
        self.assertEqual(FakeLlama.seen, [("/metrics?model=a%2Fb+q4", "Bearer secret")])

    def test_disabled_upstream_is_a_failed_scrape(self):
        FakeLlama.mode = "disabled"
        with serving(FakeLlama) as upstream, serving(exporter.make_handler(self.root / "results", self.root / ".runtime", upstream)) as proxy:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(proxy + "/llama")
            self.assertEqual(caught.exception.code, 502)
            self.assertEqual(urllib.request.urlopen(proxy + "/health").status, 200)

    def test_stale_hardware_is_removed_instead_of_reported_as_live(self):
        atomic_json(self.root / ".runtime" / "host.json", {"values": {
            "sample_timestamp_seconds": time.time() - 60, "host_cpu_usage_ratio": 0.8}, "gpus": []})
        body = exporter.local_metrics(self.root / "results", self.root / ".runtime").decode()
        self.assertIn("llmmonitor_host_collector_up 0", body)
        self.assertNotIn("host_cpu_usage_ratio", body)

    def test_result_persistence_and_benchmark_series(self):
        directory, run = benchmark.new_run(self.root, "offline", 'model"x.gguf', {})
        run.update(status="success", finished_at=time.time(), records=[{
            "n_prompt": 512, "n_gen": 0, "avg_ts": 123.5, "stddev_ts": 0.5,
            "n_threads": 4, "n_gpu_layers": 0, "build_commit": "abcd"}])
        atomic_json(directory / "result.json", run)
        malformed = self.root / "results" / "broken"
        malformed.mkdir()
        (malformed / "result.json").write_text("{broken")
        body = exporter.local_metrics(self.root / "results", self.root / ".runtime").decode()
        self.assertIn("llmmonitor_bench_avg_ts{", body)
        self.assertIn('model="model\\"x.gguf"', body)
        self.assertIn('threads="4"', body)
        self.assertIn(" 123.5\n", body)
        self.assertIn("llmmonitor_invalid_result_files 1", body)
        with contextlib.redirect_stdout(io.StringIO()):
            benchmark.compare(self.root, self.root / "comparison.csv")
        self.assertIn("pp512+tg0", (self.root / "comparison.csv").read_text())

    def test_invalid_result_schema_does_not_break_other_metrics(self):
        for index, content in enumerate(({"kind": "offline"}, [], None)):
            file = self.root / "results" / str(index) / "result.json"
            atomic_json(file, content)
        body = exporter.local_metrics(self.root / "results", self.root / ".runtime").decode()
        self.assertIn("llmmonitor_invalid_result_files 3", body)
        self.assertIn("llmmonitor_host_collector_up 0", body)

    def test_incomplete_http_response_is_a_recorded_error(self):
        with patch("llmmonitor.benchmark.request", side_effect=http.client.IncompleteRead(b"partial", 10)):
            result = benchmark.completion("http://example.invalid", "", "hello", 8, 3, 42)
        self.assertFalse(result["ok"])
        self.assertIn("IncompleteRead", result["error"])

    def test_stream_ttft_and_native_count(self):
        with serving(FakeLlama) as upstream:
            result = benchmark.completion(upstream, "", "hello", 8, 3, 42)
        self.assertTrue(result["ok"])
        self.assertEqual(result["tokens"], 4)
        self.assertLess(result["ttft_seconds"], result["latency_seconds"])
        self.assertFalse(FakeLlama.seen[0][1]["cache_prompt"])

    def test_truncated_stream_and_http_errors_fail(self):
        for mode in ("truncated", "error"):
            FakeLlama.mode = mode
            with serving(FakeLlama) as upstream:
                result = benchmark.completion(upstream, "", "hello", 8, 3, 42)
            self.assertFalse(result["ok"])
            self.assertNotIn("tokens", result)

    def test_concurrent_load_creates_summary_and_failure_exit(self):
        with serving(FakeLlama) as upstream:
            args = SimpleNamespace(prompt_file=None, prompt="hello", tokens=8, timeout=3, warmup=False,
                                   url=upstream, seed=42, model="test-model", concurrency=2, requests=4)
            with contextlib.redirect_stdout(io.StringIO()):
                code = benchmark.http_benchmark(self.root, args, "")
            self.assertEqual(code, 0)
            run = json.loads(next((self.root / "results").glob("*/result.json")).read_text())
            self.assertEqual(run["summary"]["generated_tokens"], 16)
            self.assertEqual(run["summary"]["errors"], 0)
            self.assertEqual(sorted(row["seed"] for row in run["records"]), [42, 43, 44, 45])
            FakeLlama.mode = "error"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(benchmark.http_benchmark(self.root, args, ""), 1)
        failed = [json.loads(p.read_text()) for p in (self.root / "results").glob("*/result.json") if '"failed"' in p.read_text()][0]
        self.assertEqual(failed["summary"]["errors"], 4)
        self.assertNotIn("ttft_seconds_p95", failed["summary"])

    def test_offline_process_failure_preserves_logs_and_does_not_publish_score(self):
        model = self.root / "fake.gguf"
        model.touch()
        args = SimpleNamespace(prompt_tokens="8", tokens="4", repetitions=1, threads="1", gpu_layers="0", batch="32")
        # Python receives llama-bench flags and exits with an error; exercises the real process path.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(benchmark.offline(self.root, sys.executable, [str(model)], args), 1)
        file = next((self.root / "results").glob("*/result.json"))
        run = json.loads(file.read_text())
        self.assertEqual(run["status"], "failed")
        self.assertTrue((file.parent / "stderr.log").read_text())
        self.assertFalse((self.root / ".runtime" / "benchmark.json").exists())
        body = exporter.local_metrics(self.root / "results", self.root / ".runtime").decode()
        self.assertNotIn("llmmonitor_bench_avg_ts", body)

    @unittest.skipUnless(Path("/proc/stat").exists() or psutil is not None, "Host sampling dependency")
    def test_host_sample_has_real_process_memory_and_timestamp(self):
        sampler = Sampler()
        sample = sampler.sample(os.getpid())
        self.assertGreater(sample["values"]["process_resident_memory_bytes"], 0)
        self.assertGreater(sample["values"]["host_memory_total_bytes"], 0)
        time.sleep(0.02)
        sample = sampler.sample(os.getpid())
        self.assertIn("process_cpu_cores", sample["values"])

    def test_detached_collector_starts_updates_and_stops_by_token(self):
        # Separate project to avoid touching a user's running worker or state.
        shutil.copytree(ROOT / "llmmonitor", self.root / "llmmonitor", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / ".env.example", self.root / ".env.example")
        def cli(*arguments):
            return subprocess.run([sys.executable, "-m", "llmmonitor.cli", *arguments], cwd=self.root,
                                  capture_output=True, text=True, timeout=20)
        try:
            start = cli("collect")
            self.assertEqual(start.returncode, 0, start.stderr)
            deadline = time.monotonic() + 8
            snapshot = self.root / ".runtime" / "host.json"
            while not snapshot.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(snapshot.exists())
            duplicate = cli("collect")
            self.assertEqual(duplicate.returncode, 1)
        finally:
            stop = cli("collect-stop")
            self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse((self.root / ".runtime" / "collector.json").exists())

    def test_detached_server_supervisor_stops_its_own_child(self):
        shutil.copytree(ROOT / "llmmonitor", self.root / "llmmonitor", ignore=shutil.ignore_patterns("__pycache__"))
        program = "from llmmonitor.runtime import launch; import sys; launch('server', [sys.executable, '-u', '-c', 'import time; print(123, flush=True); time.sleep(60)'])"
        try:
            start = subprocess.run([sys.executable, "-c", program], cwd=self.root, capture_output=True, text=True, timeout=15)
            self.assertEqual(start.returncode, 0, start.stderr)
            data = json.loads((self.root / ".runtime" / "server.json").read_text())
            self.assertIn("token", data)
            self.assertNotEqual(data["pid"], data["worker_pid"])
        finally:
            stop = subprocess.run([sys.executable, "-m", "llmmonitor.cli", "stop"], cwd=self.root, capture_output=True, text=True, timeout=20)
            self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertFalse((self.root / ".runtime" / "server.json").exists())
        self.assertIn("123", (self.root / ".runtime" / "server.log").read_text())

    def test_checked_in_dashboards_and_provisioning_have_valid_structure(self):
        uids = set()
        for file in (ROOT / "grafana" / "dashboards").glob("*.json"):
            dashboard = json.loads(file.read_text())
            self.assertNotIn(dashboard["uid"], uids)
            uids.add(dashboard["uid"])
            for panel in dashboard["panels"]:
                for target in panel.get("targets", []):
                    self.assertEqual(panel["datasource"]["uid"], "prometheus")
                    self.assertTrue(target["expr"])
        self.assertEqual(uids, {"llmmonitor-home", "llmmonitor-overview", "llmmonitor-hardware", "llmmonitor-benchmarks"})


if __name__ == "__main__":
    unittest.main()
