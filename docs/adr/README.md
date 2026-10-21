# Architecture Decision Records

Each record explains one design decision: the situation, what we chose, what we rejected
and why, and what it costs. [ARCHITECTURE.md](../ARCHITECTURE.md) describes how the
pieces fit together; these records explain why they are built that way.

| Record | Decision | Status | Date |
|---|---|---|---|
| [ADR-0001](0001-community-sonic-only.md) | Community SONiC only, run as docker-sonic-vs in containerlab | Accepted; image choice superseded by ADR-0011 | 2026-09-28 |
| [ADR-0002](0002-one-command-local-first.md) | One command, local first, on Linux, macOS and Windows | Accepted | 2026-10-04 |
| [ADR-0003](0003-fabric-json-single-source-of-truth.md) | fabric.json is the single source of truth | Accepted | 2026-10-01 |
| [ADR-0004](0004-measured-vs-modeled-labels.md) | Every value is labeled measured or modeled | Accepted | 2026-10-01 |
| [ADR-0005](0005-prometheus-yes-grafana-no.md) | Keep Prometheus, drop Grafana | Accepted | 2026-10-08 |
| [ADR-0006](0006-localhost-by-default.md) | The lab listens on localhost by default | Accepted | 2026-10-08 |
| [ADR-0007](0007-apple-silicon-boot-without-syslog.md) | On Apple silicon, finish the switch boot without syslog | Accepted | 2026-10-05 |
| [ADR-0008](0008-boot-order-and-routing-check.md) | Wait for a full boot, start BGP after zebra, and prove routing | Accepted | 2026-10-05 |
| [ADR-0009](0009-python-standard-library-web-server.md) | The Command Center uses only the Python standard library | Accepted | 2026-10-04 |
| [ADR-0010](0010-apache-2-and-org-home.md) | Apache 2.0, published by the DigiSignals-Inc organization | Accepted (history part superseded by ADR-0012) | 2026-10-08 |
| [ADR-0011](0011-official-sonic-image.md) | Run the newest official SONiC 202605 build, downloaded from SONiC on the first run | Accepted | 2026-10-21 |
| [ADR-0012](0012-fresh-public-history-and-dco.md) | Publish a fresh history, and sign off every commit | Accepted | 2026-10-21 |

## Adding a record

Copy the newest record, give it the next number, and keep the same sections: Context,
Decision, Alternatives considered and Consequences. Never rewrite an accepted record to
change the decision. Write a new one, and mark the old one `Superseded by ADR-NNNN`.
