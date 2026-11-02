"""Inspect the actual Grafana -> Prometheus -> exporter data path."""
import base64
import json
import time
import urllib.parse

from .common import ROOT, atomic_json, request
from .exporter import read_json


def inspect_stack(cfg, root=ROOT):
    checks = []
    grafana = f"http://127.0.0.1:{cfg.get('GRAFANA_PORT', '3000')}"
    prometheus = f"http://127.0.0.1:{cfg.get('PROMETHEUS_PORT', '9090')}"
    credentials = f"{cfg.get('GRAFANA_ADMIN_USER', 'admin')}:{cfg.get('GRAFANA_ADMIN_PASSWORD', '')}"
    auth = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}

    def fetch(component, url, predicate, headers=None):
        try:
            with request(url, timeout=4, headers=headers) as response:
                body = json.load(response)
            ok, detail = predicate(body)
            checks.append(dict(component=component, ok=bool(ok), detail=detail))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            checks.append(dict(component=component, ok=False, detail=str(exc)))

    fetch("Grafana", grafana + "/api/health", lambda body: (body.get("database") == "ok", "Database " + body.get("database", "sconosciuto")))
    fetch("Dashboard", grafana + "/api/search?tag=llmMonitor", lambda body: (
        all(uid in {row.get("uid") for row in body} for uid in
            ("llmmonitor-home", "llmmonitor-overview", "llmmonitor-hardware", "llmmonitor-benchmarks")),
        [row.get("title") for row in body]), auth)

    def targets(body):
        rows = body["data"]["result"]
        status = {row["metric"]["job"]: float(row["value"][1]) for row in rows}
        return status.get("llmmonitor") == 1, status

    query = lambda expression: prometheus + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
    fetch("Scrape Prometheus", query("up"), targets)
    fetch("Metriche benchmark", query("count(llmmonitor_run_status) or vector(0)"),
          lambda body: (True, int(float(body["data"]["result"][0]["value"][1]))))
    snapshot = read_json(root / ".runtime" / "host.json")
    age = time.time() - (snapshot or {}).get("values", {}).get("sample_timestamp_seconds", 0)
    checks.append(dict(component="Collector host", ok=0 <= age < 15,
                       detail=f"Ultimo campione: {age:.1f} s fa" if snapshot else "Nessuno snapshot"))
    report = dict(timestamp=time.time(), checks=checks, dashboards={
        name: grafana + "/d/llmmonitor-" + name for name in ("home", "hardware", "benchmarks", "overview")})
    atomic_json(root / ".runtime" / "diagnostics.json", report)
    return report
