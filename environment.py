"""Resolve local executables and build backends without machine-specific paths."""
import os
import platform
import shutil
from pathlib import Path

from .common import ROOT


def resolve_path(value, root=ROOT):
    candidate = Path(value).expanduser()
    return (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def source_directory(cfg, root=ROOT):
    if cfg.get("LLAMA_CPP_DIR"):
        return resolve_path(cfg["LLAMA_CPP_DIR"], root)
    for candidate in (root / ".runtime" / "llama.cpp", root.parent / "llama.cpp", root.parent / "llamacpp"):
        if (candidate / "CMakeLists.txt").is_file():
            return candidate.resolve()
    return root / ".runtime" / "llama.cpp"


def find_binary(cfg, name, root=ROOT):
    suffix = ".exe" if os.name == "nt" else ""
    directories = []
    if cfg.get("LLAMA_BIN_DIR"):
        directories.append(resolve_path(cfg["LLAMA_BIN_DIR"], root))
    source = source_directory(cfg, root)
    directories.extend((source / "build" / "bin", source / "build" / "bin" / "Release"))
    for directory in directories:
        candidate = directory / (name + suffix)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which(name + suffix)
    if found:
        return Path(found).resolve()
    raise ValueError(f"{name} non trovato. Esegui install --clone oppure imposta LLAMA_BIN_DIR.")


def default_backend():
    if platform.system() == "Darwin":
        return "metal"
    return "cuda" if shutil.which("nvcc") else "cpu"


def cmake_configure(source, build, backend):
    if backend == "metal" and platform.system() != "Darwin":
        raise ValueError("Il backend Metal richiede macOS")
    if backend == "cuda" and platform.system() == "Darwin":
        raise ValueError("CUDA non è disponibile su macOS; usa metal o cpu")
    return ["cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release",
            "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_BUILD_TOOLS=ON", "-DLLAMA_BUILD_SERVER=ON",
            "-DGGML_CUDA=" + ("ON" if backend == "cuda" else "OFF"),
            "-DGGML_METAL=" + ("ON" if backend == "metal" else "OFF")]
