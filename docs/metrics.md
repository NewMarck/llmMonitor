# Metriche

## Server llama.cpp

L'exporter inoltra integralmente il corpo di `/metrics` su `/llama`, includendo nomi e label non ancora conosciuti dal progetto. Prometheus raccoglie questo endpoint nel job `llama`. L'assenza dell'endpoint, `--metrics` disabilitato, errori di rete o autenticazione producono uno scrape fallito (`up{job="llama"}=0`), non un falso stato sano.

La dashboard Live contiene grafici dedicati alle misure note e una tabella di tutte le serie il cui nome inizia con `llamacpp:`. Per qualsiasi metrica, usa Explore in Grafana:

```promql
{job="llama", __name__=~"llamacpp:.*"}
```

Esempi di serie native. L'inventario effettivo è determinato dall'endpoint del server collegato:

| Serie | Tipo nativo | Significato |
| --- | --- | --- |
| `llamacpp:prompt_tokens_total` | Counter | Token prompt elaborati |
| `llamacpp:prompt_seconds_total` | Counter | Tempo cumulato di prefill, secondi |
| `llamacpp:tokens_predicted_total` | Counter | Token generati |
| `llamacpp:tokens_predicted_seconds_total` | Counter | Tempo cumulato di generazione, secondi |
| `llamacpp:n_decode_total` | Counter | Chiamate a `llama_decode()` |
| `llamacpp:n_tokens_max` | Counter nel sorgente | Massimo numero di token osservato; mostrato come valore assoluto |
| `llamacpp:prompt_tokens_seconds` | Gauge | Throughput prefill nativo, token/s |
| `llamacpp:predicted_tokens_seconds` | Gauge | Throughput generazione nativo, token/s |
| `llamacpp:requests_processing` | Gauge | Richieste in elaborazione |
| `llamacpp:requests_deferred` | Gauge | Richieste in attesa |
| `llamacpp:n_busy_slots_per_decode` | Gauge | Media slot occupati per decode |

Alcune versioni espongono `kv_cache_usage_ratio` e `kv_cache_tokens`; i relativi pannelli restano senza dati se le serie non esistono. Il progetto non le ricostruisce a partire da altre grandezze. I throughput nativi e i contatori si aggiornano secondo l'implementazione del server, spesso al completamento dei task: un grafico a 5 secondi non garantisce visibilità per singolo token.

`rate(counter[$__rate_interval])` misura il flusso medio nella finestra, includendo i periodi di inattività. È una grandezza diversa dal throughput di esecuzione restituito dal server.

## Hardware dell'host

| Serie `llmmonitor_*` | Unità / comportamento |
| --- | --- |
| `host_collector_up` | 1 con snapshot recente, 0 dopo 15 s senza campioni |
| `sample_timestamp_seconds` | Timestamp Unix del campione |
| `host_cpu_usage_ratio` | Da 0 a 1; differenza tra campioni CPU |
| `host_memory_total_bytes` | RAM totale |
| `host_memory_available_bytes` | RAM disponibile |
| `process_resident_memory_bytes` | RSS del processo osservato |
| `process_cpu_seconds_total` | CPU cumulata del processo |
| `process_cpu_cores` | Core occupati, 1 = un core al 100%; può superare 1 |
| `process_threads` | Numero di thread del processo |
| `process_pid` | PID osservato |
| `gpu_utilization_ratio` | Utilizzo NVIDIA da 0 a 1 |
| `gpu_memory_used_bytes`, `gpu_memory_total_bytes` | VRAM NVIDIA in byte |
| `gpu_temperature_celsius` | Temperatura NVIDIA |
| `gpu_power_watts` | Potenza NVIDIA |

Le serie GPU hanno label `gpu` e `name`. Valori `N/A` o non accessibili non producono una serie. La prima raccolta non pubblica CPU calcolata per differenza; attende il campione successivo. WSL può non esporre alcune misure NVML anche se l'inferenza CUDA funziona. Le misure GPU riguardano l'intera scheda e non esclusivamente llama.cpp.

Il processo osservato è il server gestito dagli script oppure il `--pid` esplicito. Durante un benchmark nativo, la raccolta passa al PID di llama-bench; a fine test ritorna al server/PID configurato.

## Risultati benchmark

Le serie dei risultati sono gauge persistenti, non contatori che rappresentano eventi live. Le label `run_id`, `kind` e `model` identificano l'esperimento.

- `llmmonitor_run_status`: 0 in corso, 1 successo, -1 fallito, -2 interrotto.
- `llmmonitor_run_started_timestamp_seconds`, `llmmonitor_run_finished_timestamp_seconds`: intervallo dell'esperimento.
- `llmmonitor_bench_avg_ts`, `llmmonitor_bench_stddev_ts`: media e deviazione standard token/s native.
- `llmmonitor_bench_avg_ns`, `llmmonitor_bench_stddev_ns`: tempi nativi, nanosecondi.
- `llmmonitor_bench_model_size`, `llmmonitor_bench_model_n_params`: metadati nativi quando presenti.
- `llmmonitor_http_throughput_tokens_seconds`: token generati con successo / durata totale misurata.
- `llmmonitor_http_requests_seconds`: richieste riuscite / durata totale misurata.
- `llmmonitor_http_latency_seconds_mean`, `llmmonitor_http_latency_seconds_p50`, `llmmonitor_http_latency_seconds_p95`: latenza delle richieste riuscite.
- `llmmonitor_http_ttft_seconds_mean`, `llmmonitor_http_ttft_seconds_p50`, `llmmonitor_http_ttft_seconds_p95`: tempo al primo chunk con contenuto, solo dove misurabile.
- `llmmonitor_http_requests`, `llmmonitor_http_errors`, `llmmonitor_http_generated_tokens`, `llmmonitor_http_duration_seconds`: totali della run.
- `llmmonitor_invalid_result_files`: file risultato ignorati perché non leggibili o non validi.

Ogni record offline ha anche `case`, `test`, `threads`, `gpu_layers`, `batch` e `build`. `case` è l'indice originale del record e impedisce collisioni tra combinazioni con altri parametri. I dati completi sono nel JSON originale; le label sono intenzionalmente limitate. Le run HTTP aggiungono `concurrency` e `tokens`.

I percentili usano interpolazione lineare sui campioni ordinati. Un HTTP 200 con stream incompleto resta un errore. Le run HTTP con errori conservano conteggi e riepilogo; le run native fallite non pubblicano punteggi. `compare` include soltanto run riuscite.
