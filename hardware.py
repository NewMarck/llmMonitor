"""Host sampling; unsupported measurements are omitted, never replaced with zeros."""
import csv
import io
import math
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from .common import console_options

try:
    import psutil
except ImportError:
    psutil = None


class Sampler:
    def __init__(self):
        self.cpu = None
        self.process = None
        if platform.system() == "Darwin" and psutil is None:
            raise RuntimeError("Collector: esegui scripts/bootstrap per installare psutil nell'ambiente del progetto")

    def portable_sample(self, values, pid, now):
        ticks = psutil.cpu_times()
        total = sum(ticks) - getattr(ticks, "guest", 0) - getattr(ticks, "guest_nice", 0)
        idle = ticks.idle + getattr(ticks, "iowait", 0)
        if self.cpu and total > self.cpu[0]:
            values["host_cpu_usage_ratio"] = max(0, min(1, 1 - (idle - self.cpu[1]) / (total - self.cpu[0])))
        self.cpu = total, idle
        memory = psutil.virtual_memory()
        values.update(host_memory_total_bytes=memory.total, host_memory_available_bytes=memory.available)
        if pid:
            try:
                process = psutil.Process(pid)
                with process.oneshot():
                    identity = pid, process.create_time()
                    times = process.cpu_times()
                    cpu_seconds = times.user + times.system
                    values.update(process_resident_memory_bytes=process.memory_info().rss,
                                  process_cpu_seconds_total=cpu_seconds, process_threads=process.num_threads(), process_pid=pid)
                if self.process and self.process[0] == identity and now > self.process[1]:
                    values["process_cpu_cores"] = max(0, (cpu_seconds - self.process[2]) / (now - self.process[1]))
                self.process = identity, now, cpu_seconds
            except (psutil.Error, OSError):
                self.process = None

    def sample(self, pid=None):
        now = time.time()
        values = {"sample_timestamp_seconds": now}
        proc = Path("/proc")
        if psutil is not None:
            self.portable_sample(values, pid, now)
        elif (proc / "stat").exists():
            ticks = [int(n) for n in (proc / "stat").read_text().splitlines()[0].split()[1:9]]
            total, idle = sum(ticks), ticks[3] + ticks[4]
            if self.cpu and total > self.cpu[0]:
                values["host_cpu_usage_ratio"] = 1 - (idle - self.cpu[1]) / (total - self.cpu[0])
            self.cpu = total, idle
            memory = {}
            for line in (proc / "meminfo").read_text().splitlines():
                key, val = line.split(":", 1)
                memory[key] = int(val.split()[0]) * 1024
            values.update(host_memory_total_bytes=memory["MemTotal"],
                          host_memory_available_bytes=memory["MemAvailable"])
            if pid:
                try:
                    # The process name can contain spaces and parentheses.
                    stat = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
                    cpu_seconds = (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")
                    identity = pid, stat[19]
                    values["process_resident_memory_bytes"] = int(stat[21]) * os.sysconf("SC_PAGE_SIZE")
                    values["process_cpu_seconds_total"] = cpu_seconds
                    values["process_threads"] = int(stat[17])
                    values["process_pid"] = pid
                    if self.process and self.process[0] == identity and now > self.process[1]:
                        # One busy CPU core is 100%; multi-threaded processes may exceed it.
                        values["process_cpu_cores"] = max(0, (cpu_seconds - self.process[2]) / (now - self.process[1]))
                    self.process = identity, now, cpu_seconds
                except (OSError, ValueError, IndexError):
                    self.process = None
        elif platform.system() == "Windows":
            # Host physical RAM is available without third-party Python packages.
            import ctypes

            class Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]

            memory = Memory()
            memory.length = ctypes.sizeof(memory)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
                values.update(host_memory_total_bytes=memory.total, host_memory_available_bytes=memory.available)
        gpu_rows = []
        gpu_cmd = shutil.which("nvidia-smi") or shutil.which("nvidia-smi.exe")
        if gpu_cmd:
            fields = ["index", "name", "utilization.gpu", "memory.used", "memory.total", "temperature.gpu", "power.draw"]
            try:
                result = subprocess.run([gpu_cmd, "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"],
                                        capture_output=True, text=True, timeout=2, check=True, **console_options())
                for row in csv.reader(io.StringIO(result.stdout)):
                    gpu = {"index": row[0].strip(), "name": row[1].strip()}
                    for key, cell, scale in zip(
                        ["utilization_ratio", "memory_used_bytes", "memory_total_bytes", "temperature_celsius", "power_watts"],
                        row[2:], [0.01, 1024**2, 1024**2, 1, 1]
                    ):
                        try:
                            value = float(cell) * scale
                            if math.isfinite(value):
                                gpu[key] = value
                        except ValueError:
                            pass
                    gpu_rows.append(gpu)
            except (OSError, subprocess.SubprocessError, IndexError):
                pass
        return {"values": values, "gpus": gpu_rows, "platform": platform.platform()}
