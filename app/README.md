# app

The Command Center: the live dashboard and AI assistant. `./run.sh virtual` starts it on
port 9300; see the [main README](../README.md). Connecting real switches (ssh mode) is in
the code but not finished ([docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md) F11); the
real-hardware bench uses its own `real-hardware/bench/congestion-dashboard-win.py`.

| File | What it does |
|---|---|
| `app.py` | The Command Center web server, API and AI assistant |
| `ui.html`, `studio.html` | The dashboard pages |
| `nodecaps.py` | What each kind of node can measure, shared with the collector |
| `coverage_audit.py` | Checks which metrics each switch is actually reporting |
| `collector/fabric_exporter.py` | Reads the switches and exports metrics to Prometheus |
| `collector/gnmi_client.py` | gNMI reader for real switches |
