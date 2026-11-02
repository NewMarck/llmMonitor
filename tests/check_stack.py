"""Assert real Grafana provisioning and Prometheus scrapes on an isolated CI stack."""
import base64
import json
import os
import time
import urllib.parse

from llmmonitor.common import ROOT, atomic_json, config, request


def main():
    if os.environ.get("CI") != "true":
        raise SystemExit("Questo test scrive fixture nello stack isolato CI; usa diagnose sul tuo stack.")
    cfg = config()
    credentials = f"{cfg['GRAFANA_ADMIN_USER']}:{cfg['GRAFANA_ADMIN_PASSWORD']}"
    headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
    atomic_json(ROOT / "results" / "ci-fixture" / "result.json", {
        "run_id": "ci-fixture", "kind": "offline", "model": "ci-fixture", "started_at": time.time(),
        "status": "success", "records": [{"n_prompt": 64, "n_gen": 0, "avg_ts": 25.0}],
    })
    deadline = time.monotonic() + 45
    failure = None
    while time.monotonic() < deadline:
        try:
            atomic_json(ROOT / ".runtime" / "host.json", {"values": {"sample_timestamp_seconds": time.time()}})
            with request("http://127.0.0.1:3000/api/search?tag=llmMonitor", headers=headers) as response:
                dashboards = json.load(response)
            expected = {"llmmonitor-" + name for name in ("home", "hardware", "overview", "benchmarks")}
            assert expected <= {row["uid"] for row in dashboards}, dashboards
            for expression, value in (("up{job=\"llmmonitor\"}", 1), ("up{job=\"llama\"}", 1),
                                      ("llamacpp:predicted_tokens_seconds", 17.5), ("llmmonitor_bench_avg_ts", 25),
                                      ("llmmonitor_host_collector_up", 1)):
                query = urllib.parse.urlencode({"query": expression})
                with request("http://127.0.0.1:9090/api/v1/query?" + query) as response:
                    result = json.load(response)["data"]["result"]
                assert result and float(result[0]["value"][1]) == value, (expression, result)
            print("Grafana: 4 dashboard provisionate. Prometheus: upstream, hardware e benchmark verificati.")
            return
        except (OSError, ValueError, AssertionError, KeyError) as exc:
            failure = exc
            time.sleep(1)
    raise AssertionError(f"Stack Docker non pronto: {failure}")


if __name__ == "__main__":
    main()
