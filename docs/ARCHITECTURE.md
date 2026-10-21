# Architecture

energy-lab-for-sonic runs a small data center of community SONiC switches on one machine,
reads what the switches are doing, models the energy side, and shows everything in one
dashboard. This page explains how the pieces fit together, what is measured and what is
modeled, and why it is built this way.

## The picture

```
 your machine: Linux, or a Linux VM that run.sh creates on macOS, or WSL2 on Windows
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                                                                              │
 │   containerlab fabric (official community SONiC 202605, docker-sonic-vs)     │
 │                                                                              │
 │        spine1        spine2                                                  │
 │          │  ╲      ╱  │          eBGP from every leaf to every spine, ECMP   │
 │        leaf1        leaf2                                                    │
 │          │            │                                                      │
 │        host1        host2        Alpine containers that send and receive     │
 │                                                                              │
 │      ▲  actions: traffic, failures,          │  every 8 s: port counters,    │
 │      │  queues (docker exec)                 │  BGP, routes, ECN marks       │
 │      │                                       ▼  (docker exec)                │
 │      │                          collector  127.0.0.1:9200                    │
 │      │                          models power, temperature, fans              │
 │      │                                       │                               │
 │      │                                       ▼  scraped every 10 s           │
 │      │                          Prometheus 127.0.0.1:9090                    │
 │      │                                       │                               │
 │      │                                       ▼  queried every 2 s            │
 │      └──────────────────────── Command Center 127.0.0.1:9300  ◄── browser    │
 │                                 map, scenarios, traffic, assistant           │
 └──────────────────────────────────────────────────────────────────────────────┘
```

## The pieces

| Piece | Where | What it does |
|---|---|---|
| **run.sh** | `run.sh` | The one command. It checks the machine, installs Docker and containerlab if they are missing, designs the fabric, starts the switches, waits until each one has fully booted, configures BGP, checks that traffic crosses the fabric, then starts Prometheus, the collector and the Command Center. |
| **Fabric designer** | `virtual-fabric/fabgen.py` | Turns three numbers (spines, leaves, hosts per leaf) into `build/`: `fabric.json`, the containerlab topology, `configure-fabric.sh` (links, addresses, eBGP, ECMP, hosts) and `prometheus.yml`. |
| **fabric.json** | `build/fabric.json` | The single source of truth: nodes, links, addresses, and how to reach each node. The collector and the Command Center both read it. |
| **Switches** | containerlab with `docker-sonic-vs:202605`: the newest official community SONiC 202605 build, which run.sh downloads from SONiC's build pipeline on the first run and records in `build/sonic-image.txt` (run.sh adds Debian's `libasan8`: these builds' syncd needs it, but the image leaves it out) | Community SONiC virtual switches (Debian bookworm, FRR 10.5): real Linux networking and real BGP, but no switching chip, buffers or sensors. |
| **Collector** | `app/collector/fabric_exporter.py` | Reads every switch, models the energy side, and serves Prometheus metrics. Modeled series say so in their descriptions, and missing ones are reported as absent. |
| **Prometheus** | `prom/prometheus`, pinned version | Stores the time series for the current run. The Command Center reads it through its query API. |
| **Command Center** | `app/app.py`, `app/ui.html`, `app/studio.html` | A web server written with the Python standard library. It serves the dashboard and its API, runs scenarios and traffic, and answers the assistant. The server, scenarios, traffic generation and assistant all live in one file, `app/app.py` (about 5,700 lines). |
| **Assistant** | in `app/app.py` | Offline rules by default. It can use any OpenAI-compatible model, or Anthropic on Azure, instead. |
| **Coverage audit** | `app/coverage_audit.py` | Reads one leaf's SONiC databases (STATE_DB, COUNTERS_DB, CONFIG_DB, APPL_DB), its kernel counters and FRR in one command, and sorts every field into populated, null or absent, so you can see which telemetry a platform really provides. |
| **Capability manifest** | `app/nodecaps.py` | The one place that says what each kind of node can measure: the virtual image, community SONiC hardware, or Enterprise SONiC hardware. |

## How the data flows

1. **The switches carry real traffic.** Hosts send pings and TCP streams across the
   fabric, and the switches route them with BGP and ECMP.
2. **The collector reads every switch** every 8 seconds (`INTERVAL`) with `docker exec`:
   - **Port counters** from the kernel (`/sys/class/net/<port>/statistics`). The
     virtual image has no switching chip, so SONiC's own counter database stays empty,
     and the kernel is the true traffic signal.
   - **BGP sessions** (`vtysh -c "show ip bgp summary json"`).
   - **Next hops per route** in the kernel routing table, which shows ECMP and
     convergence.
   - **ECN marks** from a RED queue. Only the congestion storm installs that queue, and
     the storm needs an ssh-mode fabric of virtual switches, which ./run.sh virtual doesn't
     build, so in the virtual lab this reads 0 and ECN is modeled.

   From the measured load it works out each switch's intensity, then models power,
   temperature, fans, ECN, PFC and WRED from it.
3. **Prometheus** scrapes the collector every 10 seconds, a little longer than the
   collector's 8-second sweep, so most scrapes see a fresh value.
4. **The Command Center** samples Prometheus every 2 seconds into one consistent
   snapshot, and the page polls its API every few seconds.
5. **When you press a button**, the Command Center runs commands in the switches and
   hosts with `docker exec`: ping floods, TCP streams, `tc` queues, packet loss with
   `netem`, BGP changes with `vtysh`, or stopping a switch. The effect shows up in the
   following snapshots.

**How long until you see it.** The collector sweeps every 8 s, plus however long the
reads take, because each sweep waits for every switch. Prometheus scrapes every 10 s, the
Command Center samples it every 2 s, and the page polls every 3 s. So a change first shows
up roughly 15–25 seconds after you press a button. That is worked out from these
intervals, not measured. Traffic, drop and ECN rates are averaged over 30 seconds to 1
minute in the Command Center's queries, so those numbers keep rising for up to about a
minute before they reach their full value.

## Measured or modeled

A virtual switch has no fans, power supplies or sensors, so the lab never shows a
modeled number as if it were measured. Every value carries its source:

| Badge in the UI | Meaning | Examples |
|---|---|---|
| **REAL** | Measured from the switch's kernel or control plane | Port counters, BGP sessions, ECMP next hops, ping times, and ECN marks on real switches |
| **MODELED** | Computed from measured load | Power (`145 + 255 × intensity` watts), chip temperature (`42 + 36 × intensity` °C), fan speed, ECN, PFC and WRED, and all GPU-cluster metrics |
| **NULL / ABSENT** | The platform doesn't provide it | Hardware PFC counters and sensors on the virtual image |
| **gNMI** | Read (polled) from a real switch over gNMI Get | Real hardware only |

### How the model works

Intensity is the switch's measured load over the last poll (bits in and out) divided by a
reference rate (`REF_BPS`, 5 Mbit/s by default), clamped to the range 0–1. On virtual
switches a small idle floor (0.10–0.20) keeps the demo alive at zero traffic; real
switches get no floor. Temperature and fans then move toward their targets gradually
(`LAG`). So flooding the fabric really does push the modeled temperature up. The model
follows the traffic; it is not a measurement of any real switch.

- **Where the constants come from.** The 145 W idle floor is calibrated to a real Dell
  Z9100-ON, measured at 143 W total at idle (two PSUs, no optics) with Dell OS9's
  `show environment` on 19 Aug 2026, before SONiC was installed; the recording is in
  [`real-hardware/fixtures/z9100-os9-baseline.json`](../real-hardware/fixtures/z9100-os9-baseline.json).
  Only the idle floor is calibrated; the 400 W loaded ceiling and the 42–78 °C range have
  no recorded measurement behind them. Under Dell OS9 the same switch's Bcm56960 sensor
  read 37 °C at idle, below the model's 42 °C floor; under SONiC this platform exposes no
  ASIC die sensor.
- **The swing is for illustration.** On our bench, congestion changed a real Z9100's power
  by +0.31 W against a 3 W noise floor (see "The null result" in
  [`real-hardware/bench/bench-knowledge.md`](../real-hardware/bench/bench-knowledge.md)).
  A real switch's power is mostly fixed. The model makes the swing large on purpose, so
  the link between traffic and energy is easy to see; read its shape, not its size.
- **The model saturates.** Any load at or above `REF_BPS` gives intensity 1, so power
  pins at 400 W and the chip at 78 °C. The 5 Mbit/s default is chosen so one traffic surge
  (about 4.6 Mbit/s on each switch in its path) lands near the top without pinning, and
  the thermal auto-heal scenario still crosses its 72 °C trigger. Bigger floods still
  flatten at the top. Raising `REF_BPS` shows their shape, but then the auto-heal
  scenario may never reach 72 °C.
- **Real switches are never modeled.** When a node is bound to real hardware
  (`fabric_node_physical=1`), the collector publishes only the sensors the platform
  really has and models nothing.

### Reading the labels outside the dashboard

The UI badges are the clearest signal. If you query Prometheus directly, rely on these
instead:

- Modeled series say `MODELED` in their help text (for example
  `sonic_temperature_celsius`, `sonic_psu_output_power_watts`,
  `sonic_ecn_marked_packets_total`, and every `gpu_*` series). They carry no
  `source` label, so a query result on its own does not tell you a value is modeled.
- `fabric_node_physical{switch}` is 1 for nodes bound to real hardware.
- A metric the platform can't produce is not published at all, and
  `fabric_metric_absent{switch,metric}` is set to 1. A missing series therefore means
  "absent", never zero.

## When something fails

- **A switch can't be read.** Each `docker exec` read has a 30 s timeout, and BGP reads
  are capped at 4 s. Each sweep waits for every switch, so one stuck switch delays that
  sweep for all of them, by up to 30 s. When a read fails or returns nothing (for example,
  the container is stopped), the collector sets `fabric_switch_up` to 0, marks the
  switch's values absent and drops their series. The dashboard then shows them as absent,
  not as stale or invented numbers. The next good read brings them back.
- **A link or a spine fails.** The virtual switches don't pass the loss of a link to BGP,
  so the far end notices only when its BGP hold timer runs out. `configure-fabric.sh`
  sets 3/9 s timers (keepalive/hold, as in FRR's datacenter profile) instead of FRR's
  default 60/180 s, so the leaves reroute within about 9 s instead of 3 minutes, during
  which half the traffic went to the dead spine.
- **A switch restarts.** Its kernel counters reset; the collector detects the reset and
  reports 0 load for that poll instead of a negative spike. A restarted container doesn't
  get its links back, though: containerlab wires them only when it deploys the lab, and
  only root on the host could re-create them. So the spine-failure scenario never kills a
  container. It cuts all of the spine's fabric links and stops its BGP, which looks the
  same to the network, and ending the scenario brings both back. To recover from a
  container that really stopped, run `./run.sh virtual` again.
- **gNMI is unreachable.** A failed gNMI read (unreachable, TLS, auth, timeout) is never
  fatal; those series are skipped, not filled with stand-in values.
- **Prometheus stops.** Its container is started with `--rm` and no volume, so stopping
  it deletes the history. The lab keeps no history across runs.

## Where it runs

| Machine | How |
|---|---|
| **Linux (x86_64)** | Directly. |
| **macOS** | `run.sh` creates a Linux VM with Lima, using Apple's virtualization and, on Apple silicon, Rosetta for the x86_64 switch image. The VM shares the repo folder, and Lima forwards the ports to the Mac's localhost. Under Rosetta, Ubuntu's AppArmor profile stops the switches' syslog, so `run.sh` finishes their boot without it. |
| **Windows** | WSL2 with Ubuntu. WSL forwards the ports to Windows' localhost. |
| **Cloud VM** | The same as Linux. Reach the dashboard through an SSH tunnel. |
| **Any machine with Python** | `./run.sh preview` runs only the Command Center, showing the designed map with no switches. |

## Real switches

The lab is built to run against real SONiC hardware too, but that path is only partly
done:

| Capability | Status |
|---|---|
| Collector **ssh mode**: reaches switches over SSH instead of `docker exec`, from a hand-written `fabric.json` | In the code |
| Collector reads SONiC's counter database and real sensors on hardware | In the code |
| Collector reads over **gNMI** | In the code; optional, needs `pygnmi`, skipped when the target is unreachable |
| Our two Dell Z9100-ON switches connected to the Command Center | **Not finished** |
| Bench scripts, bring-up runbook and recorded switch data in [`real-hardware/bench/`](../real-hardware/bench/) | In use today |

Until the Z9100s are connected, the **gNMI** badge and the hardware rows in the table
above describe what the code supports, not something you can see in the dashboard yet.

## Security

The Command Center can change the lab, and it does so with `docker exec` and
`sudo containerlab`. Its API only runs fixed actions with checked inputs, but the process
behind it has Docker access, which is effectively root on the host. **Treat access to
port 9300 like root access to the host**, not just control of a demo.

- **Everything listens on localhost only:** the Command Center on 9300, Prometheus on
  9090 and the collector on 9200.
- **Other websites can't drive it either.** Without a token, the Command Center answers
  only requests addressed to `localhost`, `127.0.0.1` or `[::1]`, which stops DNS
  rebinding. Its buttons must send `Content-Type: application/json`; anything else gets
  a 415, and a browser won't send JSON to another site without the server's permission.
- **To share it**, prefer an SSH tunnel. If you must bind to a network address, set
  `COPILOT_BIND=0.0.0.0` and a long random `COPILOT_TOKEN`; the Command Center refuses
  to start on a network address without a token. Open `/?token=<token>` once: the server
  then sets an `HttpOnly`, `SameSite=Strict` cookie, so bookmark the page without the
  query string, because the token otherwise stays in browser history. The page is served
  over plain HTTP, so on an untrusted network the token travels in the clear; use the
  SSH tunnel there.
- **No request text reaches a shell.** Every command is built from fixed lists and from
  names checked against `fabric.json`.
- **The assistant sends nothing out** unless you configure a model. If you do, each
  question sends the live fabric data to that provider.
- `run.sh` uses `sudo` to install Docker and containerlab and to run containerlab.

## Design choices

Each choice has a full record, with what we rejected and why, in
[Architecture Decision Records](adr/README.md).

- **containerlab with docker-sonic-vs:** the lightest way to run real SONiC control
  planes on a laptop. The image is SONiC's own 202605 build, downloaded from the SONiC
  project on the first run rather than hosted by us; run.sh records which build it got.
- **fabric.json as the single source of truth:** the configure script, the collector
  and the Command Center are all generated from or read the same file, so they can't
  disagree about the network.
- **Prometheus between the collector and the dashboard:** it separates reading the
  switches from showing them, keeps the history for the current run, and gives anyone a
  standard query API.
- **The Python standard library for the web server:** preview needs nothing installed.
  The collector needs two packages, `prometheus-client` and `pygnmi`, and their dependencies.
- **Measured and modeled are always labeled,** because a virtual switch has no sensors.
- **Localhost by default,** because the dashboard can change the lab and, through
  Docker, the host.

## Limits

- The virtual image has no switching chip: no buffers, hardware PFC or sensors. Those
  values are modeled or absent.
- Throughput in the virtual lab is small (megabits per second). Read the shapes, not
  the magnitudes, and remember the energy model saturates at `REF_BPS`.
- The switch image is x86_64 only. Apple silicon runs it through Rosetta: boot is
  slower, and the switches run without syslog.
- The switch image is the newest official 202605 build on the day of a machine's first
  run. SONiC's pipeline deletes builds after about 60 days, so it can't be pinned for
  good: two machines set up weeks apart can run different 202605 builds. `./run.sh status`
  names the build, and `LAB_SONIC_IMAGE` runs an image you have already tested. A new
  build or branch means re-checking the boot and BGP steps in `run.sh`.
- Each switch needs about 1.5 GB of memory. The default fabric needs about 7 GB free.
- The dashboard can't resize the fabric. Run `./run.sh virtual SPINES LEAVES HOSTS`
  instead.
- Prometheus keeps no history across runs.
- Scenarios in the default `2 2 1` lab: traffic surge, cable cut, gray failure, spine
  failure, graceful drain, bad config push, thermal auto-heal and incast. Elephant flow
  needs at least 3 leaves. The congestion storm needs an ssh-mode fabric of virtual
  switches (it uses the kernel tc RED, which real Tomahawk silicon bypasses), and the
  GPU-cluster scenarios need a GPU fabric with rail loopbacks; the dashboard greys them
  out and says why.

## Repository map

| Path | What's there |
|---|---|
| `run.sh` | The one command |
| `.env.example` | Every setting, with its default |
| `app/` | Command Center, assistant, coverage audit, capability manifest |
| `app/collector/` | The collector |
| `virtual-fabric/` | The fabric designer, and the script that rebuilds a running fabric |
| `real-hardware/` | Z9100 runbook, traffic-PC setup, bench scripts and recorded data |
| `tools/` | Telemetry coverage analyzer and its tests |
| `docs/` | This page, and the [Architecture Decision Records](adr/README.md) |
