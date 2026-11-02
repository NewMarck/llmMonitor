# Preparare le viste per gli screenshot

Usa dati misurati con un tuo modello GGUF. La demo produce run distinte con lo stesso prompt e parametri, variando i thread:

```bash
./scripts/llm-monitor up
./scripts/llm-monitor stop
./scripts/llm-monitor demo --model /percorso/modello.gguf --threads 2,4
```

La demo iniziale usa CPU, 64 token di prompt, 16 di generazione e due ripetizioni. Sono prove brevi per verificare il flusso; aumenta `--repetitions`, `--prompt-tokens` e `--tokens` per confronti più stabili. Usa `--gpu-layers` per scegliere l'offload.

Apri [Benchmark](http://localhost:3000/d/llmmonitor-benchmarks), seleziona il modello e le due run dal filtro `Run Id`. Lascia `All` se vuoi includere l'intera cronologia. Le tabelle distinguono prefill e generazione e mostrano thread, offload e identificativo della run.

Per mostrare lo stato durante l'esecuzione, lascia aperta la dashboard durante `demo`: lo stato passa da 0 (in corso) a 1 (successo), oppure a un valore negativo in caso di errore. I punteggi compaiono a run completata; non vengono simulati durante l'esecuzione.

Per i grafici di inferenza live:

```bash
./scripts/llm-monitor serve --model /percorso/modello.gguf --parallel 2
./scripts/llm-monitor load --requests 20 --concurrency 2 --tokens 128
```

Apri [llama.cpp Live](http://localhost:3000/d/llmmonitor-overview) e [Hardware](http://localhost:3000/d/llmmonitor-hardware); scegli un intervallo che includa la prova. Le misure disponibili dipendono dal server e dall'hardware. Per una figura condivisibile, conserva nel testo il modello, i parametri e il numero di ripetizioni; oscura eventuali percorsi personali visibili nei dati.

Puoi salvare gli screenshot scelti in `docs/imgs/` e collegarli nel README. Non includere `.env`, log con dati privati o file dei modelli.
