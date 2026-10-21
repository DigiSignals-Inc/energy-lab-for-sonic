# ADR-0005: Keep Prometheus, drop Grafana

- **Status:** Accepted
- **Date:** 2026-10-08

## Context
Until we prepared the repo for public release, `./run.sh virtual` used Docker Compose to start Prometheus and Grafana next to the collector and the Command Center. The collector fed Prometheus, and both Grafana and the Command Center read from Prometheus.

Grafana had one dashboard. Apart from one packets-per-second panel, it showed the metrics the Command Center already shows: port traffic, modelled temperature and power, drops, ECN marks, BGP peers and ECMP next hops. It was a second UI for that data. The Command Center only linked out to it.

The Grafana setup was not safe to publish:
- Both containers used host networking. Grafana served port 3003 on every interface, and so did Prometheus on `:9090`.
- Grafana allowed anonymous Viewer access and kept the default admin password. Its panel HTML sanitizing was turned off (`GF_PANELS_DISABLE_SANITIZE_HTML`).
- Neither image was pinned to a version.
- `run.sh` stopped with an error if the Docker Compose plugin was missing.

Our rule for the public repo is to ship no part we don't need. Grafana fails that rule. Prometheus passes it, because the Command Center depends on it.

## Decision
We keep Prometheus and remove Grafana and the Compose file.

- The Command Center reads the collector's metrics only through the Prometheus query API. It never calls the collector directly. `_prom_query` in [app/app.py](../../app/app.py) calls `/api/v1/query` on `PROM_URL`, which defaults to `http://127.0.0.1:9090`. The snapshot sampler waits `SNAPSHOT_PERIOD` seconds (default 2) between builds. It uses PromQL such as `rate(fabric_port_tx_bytes[1m])` to turn counters into rates.
- `monitoring()` in [run.sh](../../run.sh) starts one container with a plain `docker run`. The image is `PROM_IMAGE` (`prom/prometheus:v3.15.0`) and the name is `PROM_CONTAINER` (`energy-lab-for-sonic-prometheus`). The flags are `--network host`, `--rm` and `--web.listen-address=127.0.0.1:9090`, and it mounts `build/prometheus.yml` read-only. Host networking lets it scrape the collector on 127.0.0.1.
- `PROM_TMPL` in [virtual-fabric/fabgen.py](../../virtual-fabric/fabgen.py) generates that file. It defines one job, `sonic-fabric`, which scrapes `127.0.0.1:9200` every 10 s (a little longer than the collector's 8 s sweep, so no scrape repeats a stale value) with `honor_labels: true`.
- The collector, [app/collector/fabric_exporter.py](../../app/collector/fabric_exporter.py), serves its metrics on `127.0.0.1:9200` only.
- `./run.sh status` shows the Prometheus container, and `./run.sh down` removes it. `./run.sh preview` starts no Prometheus. It sets `PROM_URL` to an empty value, so the Command Center does not query one.
- The Command Center page no longer links to Grafana, and `run.sh` no longer checks for Docker Compose.

## Alternatives considered
- **Keep Grafana and lock it down:** It would still be a second UI for the same data. It would also be one more image to download, and one more service to secure, update and document.
- **Have the Command Center read the collector directly:** Its traffic, drop and congestion queries use PromQL `rate()` over 30-second or 1-minute windows. We would have to rewrite its data layer to store samples and compute rates ourselves. We would also lose the standard query API that anyone can use.
- **Keep Docker Compose for the one container:** A Compose file would add the plugin as a dependency and a second file to keep in step with `run.sh`. It gives nothing that one `docker run` line does not.

## Consequences
- There is one less image to download, and the lab no longer needs Docker Compose.
- Prometheus and the collector listen on localhost only, as the Command Center does by default (see [ARCHITECTURE.md](../ARCHITECTURE.md#security)).
- The pinned version means every run gets the same Prometheus. To upgrade, change `PROM_IMAGE` and test a full `./run.sh virtual`.
- The Prometheus UI at http://localhost:9090 is still there for your own PromQL queries. If you want Grafana, run your own on the same machine and point it at that address.
- Because of `--rm` and no named volume, nothing persists between runs. Each `./run.sh virtual` starts with no metric history, and `./run.sh down` discards the run's data.
