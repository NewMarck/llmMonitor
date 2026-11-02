"""Generate the provisioned Grafana JSON. Run after editing this file."""
import json
from pathlib import Path

DESTINATION = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
DATASOURCE = {"type": "prometheus", "uid": "prometheus"}


def variable(name, query, all_value=".*"):
    return dict(name=name, label=name.replace("_", " ").title(), type="query", datasource=DATASOURCE,
                query=query, refresh=1, includeAll=True, multi=True, allValue=all_value,
                current={"selected": True, "text": "All", "value": "$__all"}, options=[])


def panel(title, expression, x, y, w=12, h=8, unit="short", kind="timeseries", description="", legend="{{__name__}}"):
    result = dict(title=title, type=kind, datasource=DATASOURCE, gridPos=dict(x=x, y=y, w=w, h=h),
                  description=description, targets=[dict(refId="A", expr=expression, legendFormat=legend,
                  instant=kind in ("stat", "table", "bargauge"), range=kind == "timeseries", format="table" if kind == "table" else "time_series")],
                  fieldConfig={"defaults": {"unit": unit, "decimals": 2, "color": {"mode": "palette-classic"}}, "overrides": []},
                  options={"legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
                           "tooltip": {"mode": "multi", "sort": "desc"}})
    if kind == "stat":
        result["options"] = {"reduceOptions": {"calcs": ["lastNotNull"], "values": False}, "colorMode": "value", "graphMode": "area"}
    if kind == "table":
        result["options"] = {"showHeader": True, "cellHeight": "sm", "sortBy": [{"displayName": "Value", "desc": True}]}
        result["transformations"] = [{"id": "organize", "options": {"excludeByName": {"Time": True, "job": True, "instance": True}}}]
    if kind == "bargauge":
        result["options"] = {"displayMode": "gradient", "orientation": "horizontal",
                             "reduceOptions": {"calcs": ["lastNotNull"], "values": False}, "showUnfilled": True}
        result["fieldConfig"]["defaults"]["min"] = 0
    return result


def text(title, message, y, h=3):
    return dict(title=title, type="text", gridPos=dict(x=0, y=y, w=24, h=h), options=dict(mode="markdown", content=message))


def save(uid, title, panels, variables=None):
    for index, item in enumerate(panels, 1):
        item["id"] = index
    dashboard = dict(uid=uid, title=title, tags=["llmMonitor", "llama.cpp"], schemaVersion=41, version=1,
                     editable=False, timezone="browser", refresh="5s", time={"from": "now-1h", "to": "now"},
                     templating={"list": variables or []}, annotations={"list": []}, panels=panels,
                     links=[{"type": "dashboards", "title": "llmMonitor", "tags": ["llmMonitor"], "asDropdown": True, "includeVars": True}])
    DESTINATION.mkdir(parents=True, exist_ok=True)
    (DESTINATION / (uid.removeprefix("llmmonitor-") + ".json")).write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n")


def main():
    panels = [text("llmMonitor · Il tuo laboratorio locale", "Apri [Hardware](/d/llmmonitor-hardware) per GPU e RAM, [Benchmark](/d/llmmonitor-benchmarks) per i test salvati, oppure [llama.cpp Live](/d/llmmonitor-overview) per il server. **Avviare Grafana non avvia il modello.** Il server deve essere attivo con `--metrics` per mostrare dati di inferenza. Se Exporter è offline, esegui `diagnose` dagli script del progetto.", 0, 5)]
    for index, (title, expression) in enumerate([
        ("Exporter raggiungibile", 'up{job="llmmonitor"}'),
        ("Collector hardware attivo", "llmmonitor_host_collector_up"),
        ("Server llama.cpp raggiungibile", 'up{job="llama"}'),
        ("Benchmark riusciti salvati", "count(llmmonitor_run_status == 1) or vector(0)"),
    ]):
        item = panel(title, expression, index * 6, 5, 6, 5, kind="stat")
        if index < 3:
            item["fieldConfig"]["defaults"].update(
                mappings=[{"type": "value", "options": {"0": {"text": "FERMO", "color": "red"}, "1": {"text": "ATTIVO", "color": "green"}}}],
                color={"mode": "thresholds"}, thresholds={"mode": "absolute", "steps": [{"value": None, "color": "red"}, {"value": 1, "color": "green"}]})
        panels.append(item)
    panels += [
        panel("GPU · utilizzo", "llmmonitor_gpu_utilization_ratio", 0, 10, unit="percentunit", legend="GPU {{gpu}} · {{name}}"),
        panel("GPU · VRAM", "llmmonitor_gpu_memory_used_bytes", 12, 10, unit="bytes", legend="GPU {{gpu}} · {{name}}"),
        panel("RAM host utilizzata", "llmmonitor_host_memory_total_bytes - llmmonitor_host_memory_available_bytes", 0, 18, unit="bytes"),
        panel("GPU · temperatura", "llmmonitor_gpu_temperature_celsius", 12, 18, unit="celsius", legend="GPU {{gpu}} · {{name}}"),
        panel("Benchmark nativi salvati · token/s", "llmmonitor_bench_avg_ts", 0, 26, 24, 10, "suffix:tok/s", "table"),
        text("Se non compaiono dati", "**GPU/RAM:** il collector deve essere attivo (`collect` oppure `up`). **Benchmark:** apri la dashboard Benchmark; i risultati sono letti da `results/`. **Modello:** avvia llama-server con `--metrics`, poi genera traffico con `load`. Un benchmark `llama-bench` offline non alimenta le metriche del server HTTP. Per diagnosticare il collegamento esegui `diagnose`.", 36, 5),
    ]
    save("llmmonitor-home", "llmMonitor · Home", panels)

    source = 'job="llama",server=~"$server"'
    native = lambda name: 'llamacpp:' + name + '{' + source + '}'
    panels = [text("llama.cpp · Live", "Metriche native del server, campionate ogni 5 secondi. **DOWN**: controlla `--metrics`, URL e log exporter. I pannelli senza dati indicano metriche non esposte dalla versione in uso. I throughput nativi si aggiornano quando llama.cpp pubblica i dati delle richieste; non sono misure per singolo token.", 0, 4)]
    for index, (title, expression, unit) in enumerate([
        ("Server raggiungibile", 'up{' + source + '}', "short"),
        ("Prefill · token/s", native("prompt_tokens_seconds"), "suffix:tok/s"),
        ("Generazione · token/s", native("predicted_tokens_seconds"), "suffix:tok/s"),
        ("Richieste attive", native("requests_processing"), "short"),
        ("Richieste in coda", native("requests_deferred"), "short"),
        ("Contesto massimo osservato", native("n_tokens_max"), "short"),
    ]):
        panels.append(panel(title, expression, index * 4, 4, 4, 5, unit, "stat"))
    panels += [
        panel("Throughput prefill", native("prompt_tokens_seconds"), 0, 9, unit="suffix:tok/s"),
        panel("Throughput generazione", native("predicted_tokens_seconds"), 12, 9, unit="suffix:tok/s"),
        panel("Richieste attive e in attesa", '{' + source + ',__name__=~"llamacpp:requests_(processing|deferred)"}', 0, 17),
        panel("Token elaborati al secondo · media finestra", 'rate(' + native("prompt_tokens_total") + '[$__rate_interval])', 12, 17,
              unit="suffix:tok/s", description="Rate del contatore prompt; include i periodi di inattività."),
        panel("Token generati al secondo · media finestra", 'rate(' + native("tokens_predicted_total") + '[$__rate_interval])', 0, 25, unit="suffix:tok/s"),
        panel("Secondi spesi in prefill e decode al secondo", 'rate({' + source + ',__name__=~"llamacpp:(prompt_seconds_total|tokens_predicted_seconds_total)"}[$__rate_interval])', 12, 25),
        panel("Chiamate decode al secondo", 'rate(' + native("n_decode_total") + '[$__rate_interval])', 0, 33, unit="ops"),
        panel("Slot occupati medi per decode", native("n_busy_slots_per_decode"), 12, 33),
        panel("KV cache · se esposta dalla versione", native("kv_cache_usage_ratio"), 0, 41, unit="percentunit"),
        panel("KV cache token · se esposta dalla versione", native("kv_cache_tokens"), 12, 41),
        panel("Tutte le metriche native · valori attuali", '{' + source + ',__name__=~"llamacpp:.*"}', 0, 49, 24, 12, kind="table",
              description="Nessuna allowlist: include automaticamente tutte le metriche native llamacpp: della versione installata."),
    ]
    save("llmmonitor-overview", "llmMonitor · llama.cpp Live", panels, [variable("server", 'label_values(up{job="llama"}, server)')])

    panels = [text("Hardware host", "Il collector gira sull'host e scrive uno snapshot condiviso con Docker. **Collector = 0**: dati assenti o più vecchi di 15 secondi. CPU, RAM e processo vengono letti tramite psutil. GPU NVIDIA tramite `nvidia-smi`, solo per le misure supportate. Le risorse WSL si riferiscono all'ambiente Linux. Durante `bench` il processo osservato passa a llama-bench. Le GPU Apple non espongono le metriche NVIDIA: quei pannelli restano senza dati.", 0, 4)]
    panels += [
        panel("Collector attivo", "llmmonitor_host_collector_up", 0, 4, 6, 5, kind="stat"),
        panel("CPU host", "llmmonitor_host_cpu_usage_ratio", 6, 4, 6, 5, "percentunit", "stat"),
        panel("RAM host utilizzata", "llmmonitor_host_memory_total_bytes - llmmonitor_host_memory_available_bytes", 12, 4, 6, 5, "bytes", "stat"),
        panel("RAM processo", "llmmonitor_process_resident_memory_bytes", 18, 4, 6, 5, "bytes", "stat"),
        panel("CPU host", "llmmonitor_host_cpu_usage_ratio", 0, 9, unit="percentunit"),
        panel("RAM host utilizzata", "llmmonitor_host_memory_total_bytes - llmmonitor_host_memory_available_bytes", 12, 9, unit="bytes"),
        panel("CPU processo · core occupati", "llmmonitor_process_cpu_cores", 0, 17,
              description="1 = un core al 100%. Può superare 1. Richiede server gestito, bench o collect --pid PID."),
        panel("RAM processo", "llmmonitor_process_resident_memory_bytes", 12, 17, unit="bytes"),
        panel("GPU utilizzo", "llmmonitor_gpu_utilization_ratio", 0, 25, unit="percentunit", legend="GPU {{gpu}} · {{name}}"),
        panel("GPU VRAM utilizzata", "llmmonitor_gpu_memory_used_bytes", 12, 25, unit="bytes", legend="GPU {{gpu}} · {{name}}"),
        panel("GPU temperatura", "llmmonitor_gpu_temperature_celsius", 0, 33, unit="celsius", legend="GPU {{gpu}} · {{name}}"),
        panel("GPU potenza", "llmmonitor_gpu_power_watts", 12, 33, unit="watt", legend="GPU {{gpu}} · {{name}}"),
        panel("Tutte le misure hardware disponibili", '{__name__=~"llmmonitor_(host|process|gpu)_.*"}', 0, 41, 24, 10, kind="table"),
    ]
    save("llmmonitor-hardware", "llmMonitor · Hardware", panels)

    selection = '{run_id=~"$run_id",model=~"$model"}'
    panels = [text("Laboratorio benchmark", "Run salvate su disco: `bench` misura prefill/decode nativi; `load` misura il servizio HTTP sotto carico. Filtra **run** e **modello** e confronta configurazioni omogenee. I valori delle run sono risultati salvati, non throughput live. Stato: **1** successo, **0** in corso, **-1** fallita, **-2** interrotta. I risultati restano visibili finché conservati in `results/`.", 0, 4)]
    panels += [
        panel("Stato delle run", "llmmonitor_run_status" + selection, 0, 4, 24, 7, kind="table"),
        panel("Confronto configurazioni · token/s", "llmmonitor_bench_avg_ts" + selection, 0, 11, 24, 8, "suffix:tok/s", "bargauge",
              legend="{{model}} · {{test}} · {{threads}} thread · {{run_id}}"),
        panel("llama-bench · token/s per modello e configurazione", "llmmonitor_bench_avg_ts" + selection, 0, 19, 24, 10,
              "suffix:tok/s", "table", "pp = prompt processing; tg = token generation. case distingue tutte le combinazioni originali."),
        panel("llama-bench · deviazione standard token/s", "llmmonitor_bench_stddev_ts" + selection, 0, 29, 24, 8, "suffix:tok/s", "table"),
        panel("HTTP · throughput aggregato", "llmmonitor_http_throughput_tokens_seconds" + selection, 0, 37, 12, 8, "suffix:tok/s", "table"),
        panel("HTTP · latenza p95", "llmmonitor_http_latency_seconds_p95" + selection, 12, 37, 12, 8, "s", "table"),
        panel("HTTP · tempo al primo token p95", "llmmonitor_http_ttft_seconds_p95" + selection, 0, 45, 12, 8, "s", "table"),
        panel("HTTP · richieste fallite", "llmmonitor_http_errors" + selection, 12, 45, 12, 8, kind="table"),
        panel("HTTP · latenza p50", "llmmonitor_http_latency_seconds_p50" + selection, 0, 53, 12, 8, "s", "table"),
        panel("Fine run", "1000 * llmmonitor_run_finished_timestamp_seconds" + selection, 12, 53, 12, 8, "dateTimeFromNow", "table"),
    ]
    save("llmmonitor-benchmarks", "llmMonitor · Benchmark", panels, [
        variable("model", "label_values(llmmonitor_run_status, model)"),
        variable("run_id", 'label_values(llmmonitor_run_status{model=~"$model"}, run_id)'),
    ])


if __name__ == "__main__":
    main()
