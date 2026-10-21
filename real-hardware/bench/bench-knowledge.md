# The bench — everything the agent is allowed to assert

This file is the agent's grounding. It is loaded verbatim into the system prompt on every
question, alongside a live telemetry snapshot. If something is not in here and not in the live
snapshot, the agent must say it does not know rather than guess.

## The physical setup

Two **Dell Z9100-ON** switches, Broadcom Tomahawk (BCM56960), 32 × QSFP28 100G, 2 × 750 W PSUs,
Atom C2538 CPU. Both run **community SONiC 202505** (build `202505.1196025-ff3c0c9f6`,
18 Aug 2026).

| host | serial | role | HWSKU | .bcm profile |
|---|---|---|---|---|
| `swa` (`BENCH_SWA`) | SERIAL-A | spine side | `Force10-Z9100-C32` | `th-z9100-32x100G.config.bcm` |
| `swb` (`BENCH_SWB`) | SERIAL-B | leaf side, has the breakout | `Force10-Z9100` | `th-z9100-flex-all.config.bcm` |

**They are not a matched pair.** Only swb has the 4×25G breakout. Do not describe them as
identically configured.

Two Ubuntu hosts:
- `pc2` (`BENCH_PC2`) — **sender**, ConnectX-4 100G (`rocep3s0`, PCI 0000:03:00.0, `ens5np0`)
- `pc1` (`BENCH_PC1`) — **receiver**, ConnectX-4 Lx 25G (`rocep1s0`, `ens4np0`), target IP 10.100.1.1

## The traffic path

```
pc2 (100G) → swa Ethernet0
             swa Ethernet8 ──2 m DAC── swb Ethernet0    (100G inter-switch)
             swb Ethernet8 (25G, one lane of a 4×25G breakout) → pc1
```

**The congested exit is swb Ethernet8.** The mismatch is 4:1 — 100G arriving, 25G leaving.
The path is carried by **static routes**, not BGP. swa has one BGP neighbour (10.255.0.0) stuck
in Active with zero messages ever exchanged, on the down port Ethernet4. Never claim eBGP
forwards this traffic.

100G DACs need `fec rs` set explicitly or the link stays down. The 25G breakout lane needs
autoneg off and FireCode FEC at both ends.

## What the dashboard measures, and where it comes from

Everything is read live. The dashboard polls every 2 s (platform sensors every ~6 s).

| shown | source |
|---|---|
| offered load | `SAI_PORT_STAT_IF_IN_OCTETS` on swb **Ethernet0** (arriving from swa) |
| delivered load | `SAI_PORT_STAT_IF_OUT_OCTETS` on swb **Ethernet8** (leaving to pc1) |
| drops | `SAI_PORT_STAT_IF_OUT_DISCARDS` on swb Ethernet8 |
| PFC | `SAI_PORT_STAT_PFC_3_TX_PKTS` on swb Ethernet0 — pause sent **upstream** |
| ECN | Broadcom chip counter `EGR_ECN_COUNTER.xe8`, polled on swb into `/tmp/ecn_count` |
| queue watermark | `SAI_QUEUE_STAT_SHARED_WATERMARK_BYTES` on Ethernet8 queue 3 |
| PSU watts | `STATE_DB PSU_INFO|PSU1/PSU2` field `power` — **DC output only** |
| temperatures | `STATE_DB TEMPERATURE_INFO` — 8 sensors per switch |
| fans | `STATE_DB FAN_INFO` — 12 per switch |
| host CNP counters | `/sys/class/infiniband/rocep{1,3}s0/ports/1/hw_counters/` |

**gNMI** runs on both switches on `:8080`, `--noTLS`, on built-in defaults — there is **no
TELEMETRY or GNMI table in CONFIG_DB at all**. A Get returns PFC counters, queue counters, PSU
power, all 8 temperatures and all 12 fans. **Get is proven; Subscribe is not.** A Subscribe
(sample @2s) was seen once, from a single `gnmi-probe-hw.py` run on 2026-08-20, and not repeated
(the on-box `gnmi_cli` forces TLS against a noTLS server). The dashboard itself reads over SSH +
`sonic-db-cli`, not gNMI — same Redis data, different front door.

## The five traffic modes the buttons drive

1. **Healthy baseline** — one RoCE flow, software-paced to 12G (`ib_write_bw --rate_limit=12
   --rate_limit_type=SW`, port 18515). Offered ≈ delivered, zero drops — **but NOT quiet.**
   See "Why the baseline still pauses" below before answering any question about it.
2. **Inject congestion** — baseline plus a full-rate aggressor on port 18516. Demand exceeds
   25G, PFC and ECN surge, **drops stay 0**.
3. **Lossy flood** — `iperf3 -u -b 80G -P 8`. UDP ignores backpressure, so the switch drops.
4. **Bursty traffic** — ~6 s on, ~5 s off, repeating. PFC and ECN pulse and fall to **zero** in
   the gaps.
5. **Single-flow loop** — one RoCE queue pair, no rate limit. The control law without other
   flows averaging it out. **This is ConnectX firmware DCQCN, not the 2015 paper's algorithm.**

Stop kills everything. Hardware rate limiting is unavailable with `-R`/rdma_cm, so pacing is
software and therefore bursty.

### Why the baseline still pauses — measured 1 Sep 2026

The baseline averages ~12–13 Gb/s into a 25 Gb/s exit, so **on average it is NOT
oversubscribed**. It still produces sustained PFC and ECN. Measured while `phase: baseline`:

| | |
|---|---|
| delivered (server rate, 12 consecutive polls) | 9.6 – 13.5 Gb/s, mean ~12.5 |
| delivered (raw counter delta, two 5–6 s windows) | **8.2 Gb/s and 21.7 Gb/s** |
| PFC pause | 7,223 – 14,420 /s |
| ECN marks | 19,947 – 39,309 /s |
| egress queue watermark | **319,072 B (312 KB)** |
| drops | **0** |

**The cause is burstiness, not oversubscription.** The sender is a ConnectX-4 **100G** NIC and
`--rate_limit_type=SW` is *software* pacing: it sends a batch at full line rate then sleeps. The
average comes out at 12 Gb/s; the instantaneous rate inside each batch is ~100 Gb/s. A 4:1 step
cannot absorb a 100G lump, so the queue fills on every burst, crosses Kmin, marks and pauses,
then drains in the gap.

The watermark is the proof: **312 KB is above Kmax (300 KB)**. A genuinely smooth 12 Gb/s stream
into a 25 Gb/s port would leave that queue near-empty.

**Never explain baseline PFC as "more arrives than the exit can drain."** That is true of the
flood modes and false here — 12 into 25 is not oversubscribed. The honest sentence is: *it
arrives in lumps.*

A quiet baseline would need hardware packet pacing, which `-R`/rdma_cm rules out on this bench.
That constraint is real and has not been worked around.

### The control experiment — mode `reverse`, measured 1 Sep 2026

The **Healthy reference** button runs the same RoCE traffic in the opposite direction:
**pc1 (25G) -> pc2 (100G)**, so the path never narrows. Same two NICs, same `ib_write_bw`, same
bursty software pacing. The only variable removed is the 4:1 step.

| | forward (into the step) | reverse (no narrowing) |
|---|---|---|
| throughput | ~12.5 G avg | **~23 G sustained** |
| PFC | 7,223 – 14,420 /s | **0** |
| ECN | 19,947 – 39,309 /s | **0** |
| drops | 0 | **0** |

Zero, held for a full minute, at nearly twice the throughput. **This proves the speed step is the
cause and the traffic is not.** When asked why the baseline pauses, cite this: it is the cleanest
evidence on the bench, and it is a measurement rather than an argument.

## Measured results (30–31 Aug 2026)

| condition | offered | delivered | drops | PFC | ECN |
|---|---|---|---|---|---|
| lossy UDP | 67.9 G | 29.2 G | **682,208/s** | — | — |
| lossless RoCE, 1 flow | 21.9 G | 21.9 G | 0 | — | — |
| healthy baseline | ~12.5 G avg | ~12.5 G avg | 0 | **7,223–14,420/s** | **19,947–39,309/s** |
| inject congestion | 26.2–27.3 G | 24.0–27.3 G | **0** | 48,704–86,711/s | 164,988–179,920/s |
| bursty | pulses | pulses | 0 | 16,106→50,888→**0** | 0→85,197→**0** |

Readings near 27 G slightly overshoot the true 25 G line rate — a 2-second counter-polling
artefact, not real throughput above line rate.

The earlier "healthy baseline: PFC low, ECN ~0" row was wrong and was corrected on 1 Sep 2026
after direct measurement. Baseline pauses and marks continuously; only the *bursty* mode returns
to a true zero, and only in its gaps.

## The findings

1. **The SAI ECN counter is not missing on Tomahawk-1.** The `WRED_ECN_QUEUE_STAT`
   flex-counter group **ships disabled**. `sudo counterpoll wredqueue enable` makes
   `SAI_QUEUE_STAT_WRED_ECN_MARKED_PACKETS` appear in COUNTERS_DB and read over gNMI.
   The capability query said `isSupported: true` all along.
2. **thermalctld ships disabled** — `skip_thermalctld: true` in `pmon_daemon_control.json`,
   so `TEMPERATURE_INFO` and `FAN_INFO` are absent until it is enabled. `skip_ledd` is still
   true; only thermalctld was flipped.
3. **The stock QoS template deadlocks QosOrch.** A `PORT_QOS_MAP|global` entry holds a
   reference that prevents a removal from completing; deleting it let per-port PFC and
   TC_TO_QUEUE bind. Do **not** run `config qos reload` before a demo — the shipped template
   re-emits the global entry.
4. **The 4×25G breakout needs the flexport HWSKU** (`Force10-Z9100`), plus a
   `/etc/sonic/sonic-environment` cache fix and a `docker rm` to rebuild the mount.
5. **100G DACs need FEC RS set explicitly.**

## The null result — state this plainly, never soften it

Congestion produced **no measurable change** in power or temperature at this load.

- Peak demo load is **27 Gb/s into a 3.2 Tb/s chip = 0.85%**.
- Traffic fully on vs fully off: **+0.31 W AC** against a **3.1 W peak-to-peak noise floor**;
  **+0.11 W DC** against **4.8 W p-p**. Not statistically significant.
- Board temperature moved **+0.02 °C**. It wanders 0.3 °C at rest and quantises in 0.2 °C steps.
- All 12 fans held their speed.
- **There is no ASIC die sensor.** `ASIC_TEMPERATURE_INFO` reads zeros. "ASIC On-board Rear" is
  a *board* sensor beside the chip — never call it a die temperature.
- swa and swb draw nearly identical power (~136 W vs ~133 W) even though only swb carries the
  congestion.

The honest framing: a switch's power is mostly fixed — SerDes, PHYs, optics, CPU and fans are
on regardless — and the traffic-dependent part tracks packet rate, not queue depth. Buffer
occupancy has **no first-order energy cost**: SRAM burns energy per access, not per second of
storage. So a congested fabric burns near-identical watts while delivering less work.

## Fans — the quirk worth knowing

12 per switch (5 trays × 2, plus 2 PSU fans). **STATUS: FIXED on both switches, 6 Sep 2026.**
All 24 fans across swa and swb now report `status = True` with `speed_target` matching the
actual speed. If asked "are the fans OK" the answer is **yes, and here is what was wrong** —
do NOT describe this in the present tense as a live fault.

### Root cause, read off the switch 6 Sep 2026 — do not call this a "known quirk"

The Dell platform plugin at `/usr/local/lib/python3.11/dist-packages/sonic_platform/fan.py`:

| line | code | consequence |
|---|---|---|
| 255 | `def get_target_speed(): return 0` | the target is **hardcoded to zero** |
| 195 | `get_speed_tolerance(): return 20` | fixed 20% band |
| 180 | `get_speed()` = `(100 * rpm) // 16000` | actual reads ~52% |

thermalctld compares actual against target within the tolerance. **52 against 0 fails a 20%
band every single time**, so every fan is permanently "Not OK". Nothing is wrong with the fans.

Dell already fixed this on the **sibling S6100**, same SMF FPGA and same code lineage, where
`get_target_speed()` returns `self.get_speed()` with a comment saying it is to avoid exactly
this false alarm. The Z9100 plugin predates that fix.

**We applied that same one-line fix on 6 Sep 2026** — `return self.get_speed()` — to both
switches and restarted pmon. All 24 fans went to `status = True` with target tracking speed.

A trap worth knowing: `thermalctld` runs inside the **pmon container**, which holds its OWN
copy of `fan.py` (different inode from the host). Patching only the host changes nothing. Both
copies were patched; backups are `fan.py.orig-<timestamp>` in each. A `config reload` that
recreates the container, or a platform-package upgrade, reverts it.

### RPM: the hardware DOES expose it — earlier wording here was wrong

`speed_target` is **0** and **STATE_DB carries a percentage, never RPM** — both true. But the
claim "this platform exposes no RPM" was **false**. Raw RPM sits in sysfs and the plugin reads
it directly, then divides it away before SONiC ever sees it:

```
/sys/devices/platform/SMF.512/hwmon/hwmon0/fan{1..12}_input
  fan1_input  = 8438      fan2_input = 8623      fan12_input = 9056     (RPM, measured 6 Sep)
MAX_Z9100_FAN_SPEED = 16000    ->    8438/16000 = 52%   which is what SONiC reports
```

So the precise statement is: **SONiC reports a percentage; the RPM is one layer down in sysfs
and is readable.** Say it that way — "the platform exposes no RPM" is wrong and checkable.

Fan control is autonomous in the SMF FPGA with no `thermal_policy.json`, which is why fans are
effectively a third clock alongside the counters and the 3-second PSU refresh.

## Congestion control specifics

**WRED profile** `AZURE_LOSSLESS` on swb: `ecn_all`, green min 100,000 B, green max 300,000 B,
drop probability 5%.

**PFC xoff** on the 100G ingress PG (Ethernet0) is **177,632 B**; on the 25G profile, 53,248 B.

**Kmin sweep (a failed fix, worth knowing):** tested 100 / 160 / 256 KB. ECN went
150,000 → 9,500 → 1,300 per second, but **PFC stayed 50,000–137,000/s at every setting**. PFC
fires on the *ingress* priority group when arrival outruns the 25G drain — the speed mismatch
itself, not a threshold error. At 256 KB, Kmin crossed the PFC trigger and ECN collapsed 99%.
Reverted to the shipped 100 KB / 300 KB, verified in ASIC_DB.
The viable window is narrow: 50 µs of drain at 25 G is 156,250 B, and xoff is 177,632 B — about
21 KB of room.

**The CNP-limiter experiment (the strongest result).** `np_min_time_between_cnps` in
`/sys/kernel/debug/mlx5/0000:03:00.0/cc_params/`. ConnectX **ships it at 0** — no CNP rate
limiting, which is the DCQCN+ behaviour. Setting it to 50 µs imposes the 2015 paper's limiter:

| | offered | CNP sent |
|---|---|---|
| limiter 0 (shipped) | 18.8–23.1 G | 44,010–48,246/s |
| limiter 50 µs | 1.5–12.9 G | 709–3,060/s |
| restored to 0 | 22.5–23.2 G | 46,222–49,557/s |

Feedback starved ~39×, throughput collapsed ~65%, ECN roughly doubled, and it **recovers** when
removed. Two independent runs, zero overlap. **Methodology trap: `cc_params` apply at
queue-pair creation — the knob must be set before flows start, or nothing happens.**
We are not "enabling DCQCN+"; we ran the experiment in reverse.

ConnectX-4 defaults, all diverging from the paper: `np_min_time_between_cnps 0`,
`rp_clamp_tgt_rate 0`, `rp_ai_rate 200`, `rp_hai_rate 1000`, `rp_time_reset 300`,
`rp_byte_reset 32767`, `rp_gd 11`, `rp_min_dec_fac 50`.

**No PFC watchdog is configured** (`default_pfcwd_status: disable`), so the health metric a
well-tuned deployment would watch does not exist here. **The Z9100-ON does not support INT**,
so HPCC is theory on this bench, never a demo.

## Things that do not exist here — never promise them

- Transceiver DOM is `N/A` on every port: DACs are copper and have no optics sensors.
- PSU `input_power`, `output_voltage`, `output_current` are empty; PSU `temp` is `N/A`.
- No ASIC die temperature.
- No fan RPM in STATE_DB (percentage only); raw RPM is in SMF sysfs.
- gNMI Subscribe is unproven on this hardware: seen once from the probe on 2026-08-20, not repeated.

## The emulated fabric (a different system)

The virtual lab in `virtual-fabric/` is community SONiC virtual switches (`docker-sonic-vs`)
in containerlab: 2 spines, 2 leaves and 1 host per leaf by default. Traffic, routes and
counters there are **real**; power, temperature and fan RPM are **modelled** from real load
(`watts = 145 + intensity × 255`, `asic = 42 + intensity × 36 °C`). Always badge those as
modelled. Its throughput figures are tiny and not quotable — read shape, never magnitude.
