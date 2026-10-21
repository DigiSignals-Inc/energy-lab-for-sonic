# ADR-0004: Every value is labeled measured or modeled

- **Status:** Accepted
- **Date:** 2026-10-01

## Context
The lab exists to show congestion, power and heat on one time axis. Its switches run `docker-sonic-vs`: real Linux networking and real BGP (FRR), but no switching chip, buffers, fans, power supplies or sensors. The software ASIC leaves SONiC's COUNTERS_DB empty, so `show interfaces counters` reads N/A. Packets still cross the Linux interfaces, so kernel counters are the true traffic signal. Nothing on a virtual switch reports watts, degrees or fan speed. If we showed synthetic energy numbers next to real counters with no label, readers would take them for switch readings. A missing sensor published as 0 misleads in the same way.

## Decision
We measure what the virtual switch really has, model energy and hardware congestion from that measured load, and label every value with its source.

- **Measured.** [fabric_exporter.py](../../app/collector/fabric_exporter.py) runs one `docker exec` per switch per sweep and waits `INTERVAL` (8 s) between sweeps. It reads port counters from `/sys/class/net/<port>/statistics`, BGP sessions from `vtysh -c "show ip bgp summary json"`, the count of `via` next hops in the kernel routing table and, on leaves, ECN CE marks from `tc -s qdisc show` (`fabric_ecn_marks_total`).
- **Real ECN marks.** The congestion storm in [app.py](../../app/app.py) (`_storm_tc_cmd`) installs an `htb` root on one leaf's uplinks. Only traffic to destination port 5201 (the demo TCP flow) enters a 1 Mbit/s class with a `red ... ecn` qdisc. BGP and all other traffic use the unshaped 10 Gbit/s default class. Without the storm the marks stay at 0. The storm runs only on an ssh-mode fabric (`scen_storm_start` refuses otherwise, and `SSH_ONLY_SCENARIOS` greys its button out in the docker lab that [run.sh](../../run.sh) builds), so there `fabric_ecn_marks_total` stays 0.
- **Modeled.** The input is the measured load (`fabric_switch_load_bps`): rx + tx bits/s across a switch's ports since the last poll. Intensity is load / `REF_BPS` (5,000,000 by default; it was 1,500,000 until 2026-10-08), clamped to 0–1, with an idle floor of 0.15 ± 0.05 (120 s cycle) on virtual nodes. From intensity the collector computes:
  - an ASIC temperature target of `TEMP_FLOOR + TEMP_SPAN × intensity` (42–78 °C). The temperature moves a fraction `LAG` toward it each poll (0.12 in the collector; [run.sh](../../run.sh) passes 0.2);
  - total power of `145 + 255 × intensity` W over two PSUs, with PSU input = output / 0.92;
  - a fan target of 6000 rpm plus 350 rpm per °C of ASIC temperature above 45. The fan moves 0.2 of the way toward it each poll;
  - ECN, WRED and PFC counters that grow only while intensity is above `KNEE` (0.65).

  The settings are in [.env.example](../../.env.example). The GPU pod's `gpu_*` series are modeled too.
- **In Prometheus.** The modeled switch series keep their `sonic_*` names (`sonic_temperature_celsius`, `sonic_psu_output_power_watts`, `sonic_fan_rpm`, `sonic_ecn_marked_packets_total`, ...), and their help text starts with MODELED. `fabric_node_physical` is 1 when a node's values come from real hardware. A field a node cannot produce gets `fabric_metric_absent{switch,metric}=1` and no value series, never a 0.
- **In the UI.** [ui.html](../../app/ui.html) gives each value a badge such as REAL, REAL·but·zero (a measured 0), MODELED, NULL, ABSENT or `gNMI`. The correlation strip's gNMI badge reads `gNMI · live` when gNMI data arrives and `gNMI · stale` when no gNMI data arrives, which is always the case in the run.sh lab. The header chip reads "traffic measured · energy & congestion modeled". The assistant's prompt tells it to say that heat and power are modeled whenever it quotes them.
- **Coverage audit.** [coverage_audit.py](../../app/coverage_audit.py) reads one leaf's STATE_DB, COUNTERS_DB, CONFIG_DB and APPL_DB, its kernel counters and FRR in one batched command: over SSH on real switches (ssh mode), and through `docker exec` in the run.sh lab. It sorts each field into POPULATED (a non-zero value), NULL (the table or framework exists but there is no value, or the value is 0) or ABSENT (no key at all), plus UNKNOWN when the probe fails. In the run.sh lab it shows which fields the virtual image leaves empty.

## Alternatives considered
- **Show only measured data:** a virtual switch measures nothing about energy, so the lab could not put congestion next to power and heat, and that is its purpose.
- **Model without saying so:** the numbers would look like switch readings and be quoted as measurements.
- **Publish 0 where a platform has no value:** a 0 looks measured. For ASIC temperature it would keep the thermal agent's "below critical" check true forever, so the agent would never act and would give no sign of it.

## Consequences
- Modeled values follow the shape of the measured traffic, not the magnitude of any real switch. Never quote them as measurements. Only the 145 W floor was set from a hardware reading: one of our bench switches drew 143 W at idle, with no optics, on 2026-08-19 ([real-hardware/fixtures/](../../real-hardware/fixtures/)). The same reading agrees with the 6000 rpm fan floor.
- 5 Mbit/s of rx + tx on a switch drives intensity to 1. We raised it from 1.5 Mbit/s on 2026-10-08, because every flood pinned the model at its maximum. 5 Mbit/s puts one traffic surge (about 4.6 Mbit/s per switch) near the top without pinning, and the thermal auto-heal scenario still reaches its 72 °C trigger. Change `REF_BPS` if your traffic levels are different.
- The size of the swing is for illustration. On our bench, congestion changed a real Z9100's power by +0.31 W against a 3 W noise floor ("The null result" in [bench-knowledge.md](../../real-hardware/bench/bench-knowledge.md)).
- With no traffic, the idle floor keeps the modeled power of virtual nodes at about 170–196 W, not 145 W.
- There are two ECN series: the measured `fabric_ecn_marks_total` and the modeled `sonic_ecn_marked_packets_total`. The switch ECN panels use the measured one. PFC and WRED stay modeled.
- In the run.sh lab the switch ECN panels always read 0. Only the modeled `sonic_ecn_marked_packets_total` moves under load.
- The `sonic_*` names do not say "modeled", and their help text says MODELED even when a physical node publishes real readings under them. Queries must check `fabric_node_physical`.
- A node marked `"physical": true` in `fabric.json` is never modeled ([nodecaps.py](../../app/nodecaps.py)). The collector reads sensors from STATE_DB and, where they exist, SAI counters from COUNTERS_DB. It marks anything missing as absent and skips the tc RED probe, because the kernel interface is not the datapath on real hardware. Our bench switches are not yet connected this way.
- Every new metric or panel needs a label, and every new platform difference goes in `nodecaps.py`.
