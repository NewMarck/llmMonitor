import concurrent.futures
import csv
import datetime
import http.client
import json
import math
import platform
import statistics
import subprocess
import time
import uuid
from pathlib import Path

from .common import atomic_json, console_options, request


def new_run(root, kind, model, parameters):
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    directory = root / "results" / run_id
    directory.mkdir(parents=True)
    result = dict(schema_version=1, run_id=run_id, kind=kind, model=model, parameters=parameters,
                  started_at=time.time(), status="running", host=platform.platform())
    atomic_json(directory / "result.json", result)
    return directory, result


def validate_records(records):
    if not isinstance(records, list) or not records:
        raise ValueError("llama-bench non ha prodotto record JSON")
    for row in records:
        if not isinstance(row, dict):
            raise ValueError("llama-bench: il record deve essere un oggetto")
        for field in ("avg_ts", "n_prompt", "n_gen"):
            if field not in row or not isinstance(row[field], (float, int)) or not math.isfinite(row[field]) or row[field] < 0:
                raise ValueError(f"Record llama-bench non valido: {field}")
    return records


def offline(root, binary, models, args):
    failed = False
    for model in models:
        model_label = Path(model).name
        model = Path(model).expanduser().resolve()
        if not model.is_file():
            raise ValueError(f"Modello non trovato: {model}")
        parameters = dict(prompt=args.prompt_tokens, generation=args.tokens, repetitions=args.repetitions,
                          threads=args.threads, gpu_layers=args.gpu_layers, batch=args.batch)
        directory, result = new_run(root, "offline", model_label, parameters)
        command = [str(binary), "-m", str(model), "-o", "json", "-p", args.prompt_tokens,
                   "-n", args.tokens, "-r", str(args.repetitions), "-t", args.threads,
                   "-ngl", args.gpu_layers, "-b", args.batch]
        result["command"] = command
        atomic_json(directory / "result.json", result)
        print(f"Benchmark {model.name}: {directory.name}", flush=True)
        try:
            # Keep native output, timings, hardware and build metadata intact.
            with (directory / "stdout.json").open("w", encoding="utf-8") as stdout, (directory / "stderr.log").open("w", encoding="utf-8") as stderr:
                child = subprocess.Popen(command, stdout=stdout, stderr=stderr, **console_options())
                atomic_json(root / ".runtime" / "benchmark.json", {"pid": child.pid, "run_id": directory.name})
                try:
                    returncode = child.wait()
                except KeyboardInterrupt:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                    raise
                finally:
                    (root / ".runtime" / "benchmark.json").unlink(missing_ok=True)
            if returncode:
                raise ValueError(f"llama-bench terminato con codice {returncode}; vedi {directory / 'stderr.log'}")
            result["records"] = validate_records(json.loads((directory / "stdout.json").read_text(encoding="utf-8")))
            result["status"] = "success"
        except KeyboardInterrupt:
            result.update(status="interrupted", finished_at=time.time())
            atomic_json(directory / "result.json", result)
            raise
        except (OSError, ValueError) as exc:
            result.update(status="failed", error=str(exc))
            failed = True
            print(str(exc), flush=True)
        result["finished_at"] = time.time()
        atomic_json(directory / "result.json", result)
    return 1 if failed else 0


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


def completion(url, key, prompt, tokens, timeout, seed, model=""):
    started = time.perf_counter()
    first = None
    final = None
    body = dict(prompt=prompt, n_predict=tokens, stream=True, cache_prompt=False, temperature=0, seed=seed)
    if model:
        body["model"] = model
    try:
        with request(url.rstrip("/") + "/completion", body, key, timeout) as response:
            for line in response:
                # timeout is also enforced as a total wall time, not just a socket idle timeout.
                if time.perf_counter() - started > timeout:
                    raise TimeoutError("Timeout totale della richiesta")
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload == b"[DONE]":
                    continue
                event = json.loads(payload)
                if event.get("error"):
                    raise ValueError(str(event["error"]))
                if first is None and event.get("content"):
                    first = time.perf_counter() - started
                if event.get("stop"):
                    final = event
        if final is None:
            raise ValueError("Stream interrotto senza evento finale")
        timings = final.get("timings", {})
        predicted = final.get("tokens_predicted", timings.get("predicted_n"))
        if not isinstance(predicted, int) or predicted < 0:
            raise ValueError("Conteggio token assente nel risultato llama.cpp")
        record = dict(ok=True, latency_seconds=time.perf_counter() - started, tokens=predicted, timings=timings)
        if first is not None:
            record["ttft_seconds"] = first
        return record
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return dict(ok=False, latency_seconds=time.perf_counter() - started, error=str(exc))


def http_benchmark(root, args, key):
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    if args.warmup:
        print("Warmup…", flush=True)
        warm = completion(args.url, key, prompt, args.tokens, args.timeout, args.seed, args.model)
        if not warm["ok"]:
            raise ValueError("Warmup fallito: " + warm["error"])
    parameters = dict(concurrency=args.concurrency, requests=args.requests, tokens=args.tokens,
                      timeout=args.timeout, seed=args.seed, warmup=args.warmup, prompt=prompt, url=args.url)
    directory, result = new_run(root, "http", args.model or "server-default", parameters)
    print(f"Carico HTTP: {directory.name}", flush=True)
    started = time.perf_counter()
    records = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            pending = {executor.submit(completion, args.url, key, prompt, args.tokens, args.timeout, args.seed + i, args.model): i
                       for i in range(args.requests)}
            for future in concurrent.futures.as_completed(pending):
                record = future.result()
                record.update(request_id=pending[future], seed=args.seed + pending[future])
                records.append(record)
                print(f"  {len(records)}/{args.requests}: {'ok' if records[-1]['ok'] else records[-1]['error']}", flush=True)
    except KeyboardInterrupt:
        result.update(status="interrupted", records=records, finished_at=time.time())
        atomic_json(directory / "result.json", result)
        raise
    elapsed = time.perf_counter() - started
    successful = [row for row in records if row["ok"]]
    summary = dict(requests=len(records), errors=len(records) - len(successful), duration_seconds=elapsed,
                   generated_tokens=sum(row["tokens"] for row in successful))
    summary["throughput_tokens_seconds"] = summary["generated_tokens"] / elapsed
    summary["requests_seconds"] = len(successful) / elapsed
    for keyname in ("latency_seconds", "ttft_seconds"):
        values = [row[keyname] for row in successful if keyname in row]
        if values:
            summary[keyname + "_mean"] = statistics.mean(values)
            summary[keyname + "_p50"] = percentile(values, 0.50)
            summary[keyname + "_p95"] = percentile(values, 0.95)
    result.update(records=records, summary=summary, finished_at=time.time(),
                  status="success" if not summary["errors"] else "failed")
    atomic_json(directory / "result.json", result)
    print(json.dumps(summary, indent=2))
    return int(bool(summary["errors"]))


def compare(root, output=None):
    rows = []
    for file in sorted((root / "results").glob("*/result.json")):
        try:
            run = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if run.get("status") != "success":
            continue
        base = dict(run_id=run["run_id"], kind=run["kind"], model=run["model"])
        if run["kind"] == "offline":
            for record in run["records"]:
                rows.append(dict(base, test=f"pp{record['n_prompt']}+tg{record['n_gen']}",
                                 tokens_s=record["avg_ts"], stddev=record.get("stddev_ts", ""),
                                 threads=record.get("n_threads", ""), gpu_layers=record.get("n_gpu_layers", ""),
                                 batch=record.get("n_batch", ""), build=record.get("build_commit", "")))
        else:
            rows.append(dict(base, test=f"http/c{run['parameters']['concurrency']}",
                             tokens_s=run["summary"]["throughput_tokens_seconds"],
                             latency_p95=run["summary"].get("latency_seconds_p95", ""),
                             ttft_p95=run["summary"].get("ttft_seconds_p95", "")))
    for row in rows:
        print(f"{row['run_id']}  {row['model']}  {row['test']}  {row['tokens_s']:.2f} tok/s"
              f"  threads={row.get('threads', '-')} gpu_layers={row.get('gpu_layers', '-')}")
    if output:
        fields = ["run_id", "kind", "model", "test", "tokens_s", "stddev", "threads", "gpu_layers", "batch", "build", "latency_p95", "ttft_p95"]
        with Path(output).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    return 0
