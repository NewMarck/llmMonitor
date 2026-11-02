import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from llmmonitor import environment, hardware
from llmmonitor.common import atomic_json, console_options
from llmmonitor.diagnostics import inspect_stack


class EnvironmentTests(unittest.TestCase):
    def test_backend_selection_uses_metal_on_darwin(self):
        with patch("llmmonitor.environment.platform.system", return_value="Darwin"), patch("llmmonitor.environment.shutil.which", return_value=None):
            self.assertEqual(environment.default_backend(), "metal")

    def test_backend_selection_uses_cuda_only_with_a_toolkit(self):
        with patch("llmmonitor.environment.platform.system", return_value="Linux"):
            with patch("llmmonitor.environment.shutil.which", return_value=None):
                self.assertEqual(environment.default_backend(), "cpu")
            with patch("llmmonitor.environment.shutil.which", return_value="/opt/cuda/bin/nvcc"):
                self.assertEqual(environment.default_backend(), "cuda")

    def test_metal_build_enables_metal_and_disables_cuda(self):
        with patch("llmmonitor.environment.platform.system", return_value="Darwin"):
            command = environment.cmake_configure(Path("source with spaces"), Path("build with spaces"), "metal")
        self.assertIn("-DGGML_METAL=ON", command)
        self.assertIn("-DGGML_CUDA=OFF", command)
        self.assertIn("source with spaces", command)

    def test_cpu_build_disables_accelerators_on_darwin(self):
        with patch("llmmonitor.environment.platform.system", return_value="Darwin"):
            command = environment.cmake_configure(Path("src"), Path("out"), "cpu")
        self.assertIn("-DGGML_METAL=OFF", command)
        self.assertIn("-DGGML_CUDA=OFF", command)

    def test_foreign_accelerator_is_rejected_before_build(self):
        with patch("llmmonitor.environment.platform.system", return_value="Linux"), self.assertRaises(ValueError):
            environment.cmake_configure(Path("src"), Path("out"), "metal")
        with patch("llmmonitor.environment.platform.system", return_value="Darwin"), self.assertRaises(ValueError):
            environment.cmake_configure(Path("src"), Path("out"), "cuda")

    def test_binary_discovery_honors_path_without_sibling_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            root.mkdir()
            binary = Path(directory) / ("llama-server.exe" if os.name == "nt" else "llama-server")
            binary.touch()
            binary.chmod(0o755)
            with patch("llmmonitor.environment.shutil.which", return_value=str(binary)):
                self.assertEqual(environment.find_binary({}, "llama-server", root), binary.resolve())

    def test_binary_discovery_handles_source_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "llama source"
            bindir = source / "build" / "bin" / "Release"
            bindir.mkdir(parents=True)
            binary = bindir / ("llama-bench.exe" if os.name == "nt" else "llama-bench")
            binary.touch()
            binary.chmod(0o755)
            self.assertEqual(environment.find_binary({"LLAMA_CPP_DIR": "llama source"}, "llama-bench", root), binary.resolve())

    def test_windows_gpu_poll_does_not_open_a_console(self):
        result = SimpleNamespace(stdout="0, GPU, 20, 1024, 8192, 50, 22\n")
        with patch("llmmonitor.common.os", SimpleNamespace(name="nt")), \
             patch("llmmonitor.common.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True), \
             patch("llmmonitor.hardware.shutil.which", return_value="nvidia-smi.exe"), \
             patch("llmmonitor.hardware.subprocess.run", return_value=result) as run:
            snapshot = hardware.Sampler().sample()
        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
        self.assertEqual(snapshot["gpus"][0]["memory_used_bytes"], 1024**3)

    def test_posix_processes_do_not_receive_windows_flags(self):
        with patch("llmmonitor.common.os", SimpleNamespace(name="posix")):
            self.assertEqual(console_options(), {})

    def test_atomic_snapshot_retries_a_temporary_windows_share_lock(self):
        replace = os.replace
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "host.json"
            atomic_json(file, {"sample": 1})
            calls = []

            def locked_once(source, destination):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError("File held by reader")
                return replace(source, destination)

            with patch("llmmonitor.common.os.replace", side_effect=locked_once):
                atomic_json(file, {"sample": 2})
            self.assertEqual(json.loads(file.read_text()), {"sample": 2})
            self.assertEqual(len(calls), 2)
            self.assertEqual(list(Path(directory).glob(".tmp-*")), [])

    def test_portable_sampler_tracks_cpu_memory_and_process_identity(self):
        times = namedtuple("Times", "user system idle")
        process = Mock()
        process.oneshot.side_effect = lambda: contextlib.nullcontext()
        process.create_time.return_value = 100
        process.cpu_times.side_effect = [SimpleNamespace(user=1, system=1), SimpleNamespace(user=2, system=2)]
        process.memory_info.return_value = SimpleNamespace(rss=4096)
        process.num_threads.return_value = 4
        api = SimpleNamespace(cpu_times=Mock(side_effect=[times(2, 2, 6), times(5, 3, 12)]),
                              virtual_memory=Mock(return_value=SimpleNamespace(total=8192, available=4096)),
                              Process=Mock(return_value=process), Error=RuntimeError)
        with patch("llmmonitor.hardware.psutil", api), patch("llmmonitor.hardware.shutil.which", return_value=None), \
             patch("llmmonitor.hardware.time.time", side_effect=[1000, 1002]):
            sampler = hardware.Sampler()
            initial = sampler.sample(123)
            following = sampler.sample(123)
        self.assertNotIn("host_cpu_usage_ratio", initial["values"])
        self.assertAlmostEqual(following["values"]["host_cpu_usage_ratio"], 0.4)
        self.assertEqual(following["values"]["process_cpu_cores"], 1)
        self.assertEqual(following["values"]["process_resident_memory_bytes"], 4096)

    def test_darwin_missing_dependency_fails_with_an_actionable_error(self):
        with patch("llmmonitor.hardware.psutil", None), patch("llmmonitor.hardware.platform.system", return_value="Darwin"):
            with self.assertRaisesRegex(RuntimeError, "bootstrap"):
                hardware.Sampler()

    def test_diagnostics_checks_real_series_and_keeps_credentials_out_of_report(self):
        def response(url, **kwargs):
            if "/api/search" in url:
                self.assertTrue(kwargs["headers"]["Authorization"].startswith("Basic "))
                body = [{"uid": "llmmonitor-" + name, "title": name} for name in ("home", "overview", "hardware", "benchmarks")]
            elif "/api/health" in url:
                body = {"database": "ok"}
            elif "query=up" in url:
                body = {"data": {"result": [{"metric": {"job": "llmmonitor"}, "value": [0, "1"]}]}}
            else:
                body = {"data": {"result": [{"value": [0, "2"]}]}}
            return io.BytesIO(json.dumps(body).encode())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / ".runtime" / "host.json", {"values": {"sample_timestamp_seconds": time.time()}})
            with patch("llmmonitor.diagnostics.request", side_effect=response):
                report = inspect_stack({"GRAFANA_ADMIN_PASSWORD": "private-password"}, root)
            self.assertTrue(all(row["ok"] for row in report["checks"]))
            text = (root / ".runtime" / "diagnostics.json").read_text()
            self.assertNotIn("private-password", text)
            self.assertNotIn("Basic", text)


if __name__ == "__main__":
    unittest.main()
