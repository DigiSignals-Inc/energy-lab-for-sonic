#!/usr/bin/env python3
"""Fabric telemetry exporter for a fabgen-generated SONiC leaf-spine fabric.

Config-driven: the switch list, per-switch port lists and the exec transport
all come from fabric.json (the single source of truth emitted by fabgen.py)
instead of hardcoded constants.
Point it at a build with FABRIC_JSON=/path/to/fabric.json (default
../build/fabric.json next to this file).

Two classes of signal, kept strictly separate below and in the code:

REAL  — measured off each switch every poll via one docker-exec:
        * per-port kernel traffic counters (rx/tx bytes & packets)
        * per-port kernel health counters (rx/tx dropped & errors)
        * control-plane: BGP peers in Established state, kernel-FIB 'via' count
        * derived aggregate load (bits/s) and a 0..1 traffic 'intensity'
        Why kernel counters: docker-sonic-vs is a *virtual* switch — its software
        ASIC doesn't populate SONiC's COUNTERS_DB (show interfaces counters = N/A),
        but the packets really traverse the Linux interfaces, so kernel stats are
        the true traffic signal here. On real HW you'd read COUNTERS_DB via gNMI.

MODELED — energy (temp/power/fan) and congestion (ECN/WRED/PFC) that a virtual
        switch has NO physical sensors for. KEY IDEA: the model is driven by the
        REAL measured traffic intensity — so actually flooding the fabric pushes
        modeled heat, power, fan speed and congestion up. It is labeled 'modeled'
        because the numbers are synthesized from real load, not read from silicon.
        Metric names are part of the Command Center's contract: renaming one
        breaks its queries and alert thresholds.

Serves :9200/metrics. Per-switch state (bps baseline, thermal/fan inertia,
monotonic congestion accumulators) is KEPT across polls. Switches are polled
concurrently (stdlib ThreadPool, max 8 workers) so a 12-switch fabric still
completes each sweep well inside INTERVAL.
"""
import concurrent.futures
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

try:
    from prometheus_client import Gauge, start_http_server
except ImportError:
    if "--check" in sys.argv:
        # --check is a stdlib-only config smoke-test (used by CI / fabgen
        # --validate): stub the metric API so the module-level Gauge
        # definitions import cleanly without prometheus_client installed.
        class Gauge:                                    # noqa: shadows the real API
            def __init__(self, *a, **k): pass
            def labels(self, *a, **k): return self
            def set(self, *a, **k): pass

        def start_http_server(*a, **k):
            sys.exit("fabric_exporter: prometheus_client not installed — "
                     "pip install prometheus-client")
    else:
        sys.exit("fabric_exporter: prometheus_client not installed — "
                 "pip install prometheus-client")

# ---- optional: pygnmi, the REAL gNMI/OpenConfig client -----------------------
# Used ONLY for the genuinely-gNMI-sourced series (fabric_gnmi_out_octets, read
# from each switch's telemetry server on :8080). Guarded so --check (stdlib
# only) and any host without pygnmi installed still import cleanly — the exporter
# simply skips the gNMI read and never publishes a modeled value as "live".
try:
    from pygnmi.client import gNMIclient
    _HAVE_GNMI = True
except Exception:
    _HAVE_GNMI = False

# ---- shared node-binding / capability manifest (stdlib-only, see app/nodecaps.py)
# Imported defensively: if it is missing the exporter falls back to treating every
# node as emulated, which is EXACTLY today's behaviour — it can never stop a demo.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    import nodecaps as _caps
except Exception as _e:                                     # pragma: no cover
    _caps = None
    print(f"fabric_exporter: nodecaps unavailable ({_e}) — every node treated as emulated",
          file=sys.stderr)


# ---- fabric.json — the single source of truth (emitted by fabgen.py) --------
FABRIC_JSON = os.environ.get(
    "FABRIC_JSON",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "build", "fabric.json"))


# Node kinds the exporter scrapes. Access (ToR) switches are real SONiC instances
# too — include them so the whole datacenter carries live telemetry, not just the
# spine/leaf underlay.
SWITCH_KINDS = ("spine", "leaf", "access")


def _load_fabric(path):
    """Load and sanity-check fabric.json; exit with a clear one-liner on any problem."""
    if not os.path.isfile(path):
        sys.exit(f"fabric_exporter: fabric.json not found at '{path}' — "
                 "run fabgen.py first, or point FABRIC_JSON at a build")
    try:
        with open(path) as f:
            fab = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"fabric_exporter: cannot parse '{path}': {e}")
    for key in ("nodes", "exec"):
        if key not in fab:
            sys.exit(f"fabric_exporter: '{path}' is missing required key '{key}'")
    mode = fab["exec"].get("mode", "docker")
    if mode not in ("docker", "limactl", "ssh"):
        sys.exit(f"fabric_exporter: unknown exec mode '{mode}' (want docker|limactl|ssh)")
    switches = [n for n in fab["nodes"] if n.get("kind") in SWITCH_KINDS]
    if not switches:
        sys.exit(f"fabric_exporter: no spine/leaf nodes in '{path}'")
    for n in switches:
        if not n.get("id") or not n.get("ports"):
            sys.exit(f"fabric_exporter: switch node without id/ports in '{path}': {n}")
    return fab


def _derive(fab):
    """Pull the switch list, per-switch ports, exec mode + container prefix out of
    a loaded fabric dict. One place, so the initial load and a live reload agree."""
    switch_ports = {n["id"]: list(n["ports"])
                    for n in fab["nodes"] if n.get("kind") in SWITCH_KINDS}
    exec_mode = fab["exec"].get("mode", "docker")
    prefix = fab["exec"].get("container_prefix", f"clab-{fab.get('name', 'lab')}-")
    # ssh mode: per-switch OOB mgmt IP (the exporter reaches the real switch over SSH)
    mgmt = {n["id"]: n.get("mgmt_ip")
            for n in fab["nodes"] if n.get("kind") in SWITCH_KINDS}
    # node kind per switch — leaves get the extra REAL tc RED-ecn marks probe
    kind = {n["id"]: n.get("kind")
            for n in fab["nodes"] if n.get("kind") in SWITCH_KINDS}
    # per-node hardware binding + capability manifest (schema in app/nodecaps.py).
    # Every node in today's fabric.json resolves to the emulated defaults, so this
    # is a pure addition until a node declares "physical": true.
    if _caps is not None:
        binds = _caps.bindings_for(fab["nodes"], exec_mode, kinds=SWITCH_KINDS)
    else:
        binds = {}
    return switch_ports, list(switch_ports), exec_mode, prefix, mgmt, kind, binds


def _binding(switch):
    """Binding for one switch; a permissive emulated default when unresolved."""
    b = BINDINGS.get(switch)
    if b:
        return b
    return {"id": switch, "physical": False, "counter_source": "netdev",
            "caps": {}, "modeled_energy": True, "nos": "virtual",
            "transport": "exec", "platform": "", "site": "default"}


def _absent(binding, metric):
    """(absent, reason) — the ONE silent-zero decision point (see nodecaps)."""
    if _caps is None:
        return False, ""
    try:
        return _caps.metric_absent(binding, metric)
    except Exception:
        return False, ""


FABRIC = _load_fabric(FABRIC_JSON)
BINDINGS = {}
SWITCH_PORTS, SWITCHES, EXEC_MODE, CONTAINER_PREFIX, MGMT, SWITCH_KIND, BINDINGS = _derive(FABRIC)
SW_PW = os.environ.get("SW_PW", "YourPaSsWoRd")   # ssh mode: switch admin password
try:
    FAB_MTIME = os.path.getmtime(FABRIC_JSON)
except OSError:
    FAB_MTIME = 0.0
LIMA_VM = os.environ.get("LIMA_VM", "sonic")   # limactl mode only

# ---- env-overridable seeding-model constants (contract defaults) ------------
INTERVAL = float(os.environ.get("INTERVAL", "8"))
REF_BPS = float(os.environ.get("REF_BPS", "5000000"))
TEMP_FLOOR = float(os.environ.get("TEMP_FLOOR", "42"))
TEMP_SPAN = float(os.environ.get("TEMP_SPAN", "36"))
LAG = float(os.environ.get("LAG", "0.12"))
KNEE = float(os.environ.get("KNEE", "0.65"))
# each sweep runs one SSH probe AND one gNMI read per switch (IO-bound), so size
# the pool for both together — 10 switches = 20 concurrent tasks.
MAX_WORKERS = min(20, max(8, len(SWITCHES) * 2))

# ---- REAL: traffic ----------------------------------------------------------
TRAFFIC = {k: Gauge(f"fabric_port_{k}", f"kernel {k} per fabric port", ["switch", "port"])
           for k in ["rx_bytes", "tx_bytes", "rx_packets", "tx_packets"]}
# ---- REAL: per-port health --------------------------------------------------
HEALTH = {k: Gauge(f"fabric_port_{k}", f"kernel {k} per fabric port", ["switch", "port"])
          for k in ["rx_dropped", "tx_dropped", "rx_errors", "tx_errors"]}
# ---- REAL: control-plane ----------------------------------------------------
G_BGP = Gauge("fabric_bgp_established", "count of BGP peers in Established state", ["switch"])
G_FIB = Gauge("fabric_fib_vias", "count of 'via' next-hops in the kernel FIB (ECMP/convergence proxy)", ["switch"])
# ---- REAL: diagnostic -------------------------------------------------------
G_INTEN = Gauge("fabric_switch_intensity", "0..1 traffic intensity driving the energy model", ["switch"])
G_LOAD = Gauge("fabric_switch_load_bps", "measured aggregate load, bits/s", ["switch"])
# ---- REAL: congestion — kernel tc RED-ecn CE marks (NOT ASIC WRED, NOT gNMI) ----
# Summed 'marked N' across a leaf's uplink netdevs. 0 unless a RED-ecn qdisc is
# armed (the congestion storm scenario); climbs live while the storm runs.
G_ECN_MARKS = Gauge("fabric_ecn_marks_total",
                    "REAL kernel tc RED CE marks summed over a leaf's uplink netdevs", ["switch"])

# ---- REAL: gNMI/OpenConfig — the genuinely gNMI-sourced series ---------------
# out-octets per port, read over gNMI (one Get per sweep) from the
# switch's SONiC 'telemetry' container on :8080 (insecure TLS). This is the ONE
# series that is literally "over gNMI" — every other REAL signal is SSH/kernel.
# Only meaningful in ssh-mode fabrics (a real switch with a telemetry target);
# skipped (no stand-in values) when pygnmi is absent or the target is unreachable.
GNMI_LEAF = os.environ.get("GNMI_LEAF", "leaf1")         # the leaf whose gNMI reads are logged
GNMI_TARGET_PORT = int(os.environ.get("GNMI_TARGET_PORT", "8080"))
GNMI_USER = os.environ.get("GNMI_USER", "admin")
GNMI_TIMEOUT = float(os.environ.get("GNMI_TIMEOUT", "6"))   # hard cap; never blocks the poll
G_GNMI_OCTETS = Gauge("fabric_gnmi_out_octets",
                      "REAL out-octets read (polled) over gNMI Get/OpenConfig from the "
                      "switch's telemetry server on :8080 "
                      "(openconfig-interfaces .../state/counters/out-octets)",
                      ["switch", "port"])
G_GNMI_UP = Gauge("fabric_gnmi_up",
                  "1 if the last gNMI GET against this switch's :8080 telemetry "
                  "server succeeded this poll, else 0", ["switch"])

# ---- MODELED: energy ----------
G_TEMP = Gauge("sonic_temperature_celsius", "MODELED temperature (from real load)", ["instance", "sensor"])
G_PSU_OUT = Gauge("sonic_psu_output_power_watts", "MODELED PSU output power", ["instance", "psu"])
G_PSU_IN = Gauge("sonic_psu_input_power_watts", "MODELED PSU input power", ["instance", "psu"])
G_FAN = Gauge("sonic_fan_rpm", "MODELED fan speed", ["instance", "fan"])
# ---- MODELED: congestion (monotonic; _total, implemented as ever-rising Gauge)
G_ECN = Gauge("sonic_ecn_marked_packets_total", "MODELED ECN-marked packets (monotonic)", ["instance", "interface"])
G_WRED = Gauge("sonic_queue_wred_dropped_packets_total", "MODELED WRED drops (monotonic)", ["instance", "interface"])
G_PFC = Gauge("sonic_pfc_rx_pause_frames_total", "MODELED PFC RX pause frames (monotonic)", ["instance", "interface"])

PSUS = ["PSU 1", "PSU 2"]
FANS = ["fan1", "fan2"]

# ---- SILENT-ZERO GUARD: absence is PUBLISHED, never inferred from a 0 --------
# A Prometheus gauge cannot express "absent" — a missing series is indistinguishable
# from a dead exporter. So absence is a POSITIVE signal: for every field a node
# cannot produce we publish fabric_metric_absent{switch,metric}=1 AND we do NOT
# publish the value series at all. Consumers (app.py, the thermal agent, the UI)
# therefore see ABSENT, never a confident 0.0.
G_ABSENT = Gauge("fabric_metric_absent",
                 "1 if this metric is ABSENT on this node (no sensor / no counter "
                 "source); the value series is deliberately not published",
                 ["switch", "metric"])
G_CAP = Gauge("fabric_node_capability",
              "1 if this node declares this capability (see app/nodecaps.py)",
              ["switch", "capability"])
G_NODE_INFO = Gauge("fabric_node_info",
                    "always 1 — carries the node's hardware binding as labels",
                    ["switch", "site", "platform", "nos", "transport",
                     "counter_source", "physical"])
G_PHYSICAL = Gauge("fabric_node_physical",
                   "1 if this node is bound to real hardware (never modeled)",
                   ["switch"])
G_UP = Gauge("fabric_switch_up",
             "1 if the collector read this switch in the last sweep, 0 if it could not "
             "(container stopped, exec failed or timed out); its values are then absent",
             ["switch"])

# metrics whose absence we report every sweep (the legacy field names app.py uses)
_ABSENCE_TRACKED = ("asic_c", "cpu_c", "fan_rpm", "power_w", "psu_in_w",
                    "tx_bps", "load_bps", "drops_ps", "intensity",
                    "ecn_ps", "ecn_marks")
_BINDING_PUBLISHED = set()


def publish_binding(c):
    """Publish one node's binding + per-metric absence. Cheap, idempotent, every
    sweep — so a swap flips the badges live without a restart."""
    b = _binding(c)
    caps = b.get("caps") or {}
    if _caps is not None:
        flags = _caps.ALL_FLAGS
    else:
        flags = tuple(caps)
    for f in flags:
        G_CAP.labels(c, f).set(1.0 if caps.get(f) else 0.0)
    G_PHYSICAL.labels(c).set(1.0 if b.get("physical") else 0.0)
    key = (c, b.get("site"), b.get("platform"), b.get("nos"), b.get("transport"),
           b.get("counter_source"), "1" if b.get("physical") else "0")
    if key not in _BINDING_PUBLISHED:
        _BINDING_PUBLISHED.add(key)
    G_NODE_INFO.labels(*key).set(1.0)
    gone = {}
    for m in _ABSENCE_TRACKED:
        a, _why = _absent(b, m)
        G_ABSENT.labels(c, m).set(1.0 if a else 0.0)
        if a:
            gone[m] = True
    return b, gone


# ═══════════════════════════════════════════════════════════════════════════════
#  GPU CLUSTER ENERGY MODEL (MODELED — no virtual sensor exists inside a GPU server).
#  GPU servers (kind=="gpu") map to rails (leaves). app.py publishes the running
#  collective's state (training on/off, phased/continuous, step/burst, straggler,
#  coolant overrides) to GPU_STATE_FILE. From that we drive: per-server util ->
#  per-GPU/rack power (higher kW), a step-clock SAWTOOTH (power PEAKS in the compute
#  window, DIPS during the all-reduce comms window — anti-phase to the real fabric
#  load), a LIQUID-COOLING loop (coolant supply/return/flow, not fans), a 2-node RC
#  thermal lag (fast junction riding slow coolant), and a >~85C throttle. The real
#  network side stays real (app.py bursts real pings); everything here is MODELED —
#  badge it so. Cadence is slowed (~16s step) for stage visibility.
# ═══════════════════════════════════════════════════════════════════════════════
GPU_STATE_FILE = os.environ.get("GPU_STATE_FILE", os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".run", "gpu_state.json"))
GPU_IDLE_UTIL = float(os.environ.get("GPU_IDLE_UTIL", "0.05"))       # util with no training
GPU_COMMS_GATE = float(os.environ.get("GPU_COMMS_GATE", "0.35"))     # util during all-reduce (idle-wait)
GPU_IDLE_FRAC = float(os.environ.get("GPU_IDLE_FRAC", "0.35"))       # a GPU at 0% still draws 35% TDP
GPU_SERVER_OVERHEAD_W = float(os.environ.get("GPU_SERVER_OVERHEAD_W", "1500"))  # CPU+NVSwitch+NIC+fans
GPU_UTIL_LAG = float(os.environ.get("GPU_UTIL_LAG", "0.45"))         # EMA: rounds the square into a sawtooth
GPU_ACT_LAG = float(os.environ.get("GPU_ACT_LAG", "0.25"))          # EMA ramp of training activity up/down
COOLANT_SETPOINT = float(os.environ.get("COOLANT_SETPOINT", "32"))   # CDU supply C
COOLANT_FLOW_LPM = float(os.environ.get("COOLANT_FLOW_LPM", "42"))   # per-rack default
COOLANT_DT_CONST = float(os.environ.get("COOLANT_DT_CONST", "35"))   # dT = heat_kw/flow*const
COOLANT_LAG = float(os.environ.get("COOLANT_LAG", "0.15"))          # slow node
JUNC_BASE = float(os.environ.get("GPU_JUNC_BASE", "16"))
JUNC_SPAN = float(os.environ.get("GPU_JUNC_SPAN", "42"))
JUNC_LAG = float(os.environ.get("GPU_JUNC_LAG", "0.5"))            # fast node
GPU_THROTTLE_C = float(os.environ.get("GPU_THROTTLE_C", "85"))
GPU_THROTTLE_BAND = float(os.environ.get("GPU_THROTTLE_BAND", "12"))
GPU_THROTTLE_FLOOR = float(os.environ.get("GPU_THROTTLE_FLOOR", "0.55"))
HBM_PEAK_GBPS = float(os.environ.get("GPU_HBM_PEAK_GBPS", "3350"))     # H100 SXM ~3.35 TB/s
NVLINK_PEAK_GBPS = float(os.environ.get("GPU_NVLINK_PEAK_GBPS", "900"))  # H100 NVLink/GPU

# ---- MODELED: GPU cluster (per server + per rail/rack) ----------------------
G_GPU_UTIL = Gauge("gpu_util_percent", "MODELED GPU utilization %", ["server"])
G_GPU_POWER = Gauge("gpu_server_power_watts", "MODELED GPU server power (GPUs + overhead)", ["server"])
G_GPU_JUNC = Gauge("gpu_junction_celsius", "MODELED GPU junction temperature", ["server"])
G_GPU_HBM_T = Gauge("gpu_hbm_temp_celsius", "MODELED HBM temperature", ["server"])
G_GPU_HBM_BW = Gauge("gpu_hbm_bandwidth_gbps", "MODELED HBM bandwidth (GB/s)", ["server"])
G_GPU_NVLINK = Gauge("gpu_nvlink_gbps", "MODELED NVLink scale-up bandwidth (illustrative)", ["server"])
G_RACK_KW = Gauge("gpu_rack_power_kw", "MODELED rack/rail power (kW)", ["rail"])
G_RAIL_UTIL = Gauge("gpu_rail_util_percent", "MODELED average GPU util % on this rail", ["rail"])
G_COOLANT_S = Gauge("gpu_coolant_supply_celsius", "MODELED CDU coolant supply temp", ["rail"])
G_COOLANT_R = Gauge("gpu_coolant_return_celsius", "MODELED CDU coolant return temp", ["rail"])
G_COOLANT_F = Gauge("gpu_coolant_flow_lpm", "MODELED coolant flow (L/min)", ["rail"])
G_CDU_PUMP = Gauge("gpu_cdu_pump_kw", "MODELED CDU pump power (kW)", ["rail"])
G_GPU_TRAIN = Gauge("gpu_training_active", "1 if a training collective is running")


def _derive_gpus(fab):
    """([servers], {rail:[servers]}) from kind=='gpu' nodes. Each server carries its
    rail, TDP, gpu count, model, and a small deterministic per-server thermal offset."""
    servers, rails = [], {}
    idx = 0
    for n in fab.get("nodes", []):
        if n.get("kind") != "gpu":
            continue
        srv = {"id": n["id"], "rail": n.get("rail"),
               "tdp_w": float(n.get("tdp_w", 700)), "gpus": int(n.get("gpus", 8)),
               "model": n.get("gpu_model", "GPU"),
               "offset": 1.5 * (idx % 2)}          # 2nd server in a rack runs ~1.5C hotter
        idx += 1
        servers.append(srv)
        if srv["rail"]:
            rails.setdefault(srv["rail"], []).append(srv)
    return servers, rails


GPU_SERVERS, GPU_RAILS = _derive_gpus(FABRIC)
GPU_STATE = {}   # rail -> {"util","junction","coolant_ret","act"}


def _reset_gpu_state():
    GPU_STATE.clear()
    for rail in GPU_RAILS:
        GPU_STATE[rail] = {"util": GPU_IDLE_UTIL, "junction": COOLANT_SETPOINT + JUNC_BASE,
                           "coolant_ret": COOLANT_SETPOINT + 2.0, "act": 0.0}


_reset_gpu_state()


def _read_gpu_control():
    """Collective state published by app.py (training on/off, phase, straggler,
    coolant overrides). {} if absent — GPUs then ramp to idle."""
    try:
        with open(GPU_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def model_gpus(now):
    """MODELED GPU-cluster energy, once per sweep. Driven by the collective state
    app.py publishes + a wall-clock step clock (SAME phase as app.py's burst/compute
    windows, so GPU power lands in anti-phase with the real fabric load). Never raises."""
    if not GPU_RAILS:
        return
    ctl = _read_gpu_control()
    training = bool(ctl.get("training"))
    phased = bool(ctl.get("phased", True))
    step_ms = float(ctl.get("step_ms", 16000)) or 16000.0
    burst_ms = float(ctl.get("burst_ms", 6000)) or 6000.0
    straggler = ctl.get("straggler")
    flow_factor = ctl.get("coolant_flow_factor") or {}
    G_GPU_TRAIN.set(1.0 if training else 0.0)
    phase = (now * 1000.0) % step_ms
    # compute-phase gate: HIGH in the compute window, low during comms (anti-phase
    # to the fabric, which is busy during comms). Sustained => pinned high.
    if not training:
        base_gate = 0.0
    elif not phased:
        base_gate = 1.0
    else:
        base_gate = GPU_COMMS_GATE if phase < burst_ms else 1.0
    for rail, servers in GPU_RAILS.items():
        gs = GPU_STATE.setdefault(rail, {"util": GPU_IDLE_UTIL,
                                         "junction": COOLANT_SETPOINT + JUNC_BASE,
                                         "coolant_ret": COOLANT_SETPOINT + 2.0, "act": 0.0})
        gate = base_gate
        if training and phased and straggler and rail == straggler:
            # the straggler stays busy into the comms window (it holds up the ring)
            gate = 1.0 if phase < min(step_ms - 1, burst_ms * 1.8) else GPU_COMMS_GATE
        # training activity ramps up when training starts, decays when it stops
        gs["act"] += ((1.0 if training else 0.0) - gs["act"]) * GPU_ACT_LAG
        act = gs["act"]
        target_util = GPU_IDLE_UTIL + act * (gate - GPU_IDLE_UTIL) if training else GPU_IDLE_UTIL
        gs["util"] += (target_util - gs["util"]) * GPU_UTIL_LAG     # smooth -> sawtooth
        demand = _clamp(gs["util"])
        # thermal throttle: negative feedback from last sweep's junction temp
        throttle = _clamp(1.0 - max(0.0, gs["junction"] - GPU_THROTTLE_C) / GPU_THROTTLE_BAND,
                          GPU_THROTTLE_FLOOR, 1.0)
        eff = demand * throttle
        # per-server + per-rack power
        rack_kw = 0.0
        for srv in servers:
            tdp, ngpu = srv["tdp_w"], srv["gpus"]
            p_idle = GPU_IDLE_FRAC * tdp
            gpu_p = p_idle + eff * (tdp - p_idle)
            server_w = ngpu * gpu_p + GPU_SERVER_OVERHEAD_W
            rack_kw += server_w / 1000.0
            G_GPU_UTIL.labels(srv["id"]).set(eff * 100.0)
            G_GPU_POWER.labels(srv["id"]).set(server_w)
            G_GPU_HBM_BW.labels(srv["id"]).set(eff * HBM_PEAK_GBPS)
            G_GPU_NVLINK.labels(srv["id"]).set(eff * NVLINK_PEAK_GBPS)
        # liquid-cooling loop (per rack/rail)
        flow = max(2.0, COOLANT_FLOW_LPM * float(flow_factor.get(rail, 1.0) or 1.0))
        supply = COOLANT_SETPOINT + 1.0 * math.sin(2 * math.pi * now / 180.0)   # small CDU noise
        dT = rack_kw / flow * COOLANT_DT_CONST
        gs["coolant_ret"] += ((supply + dT) - gs["coolant_ret"]) * COOLANT_LAG  # slow node
        # junction: fast node riding coolant; a starved loop (hot return) raises it further
        junc_target = supply + JUNC_BASE + eff * JUNC_SPAN
        junc_target += max(0.0, gs["coolant_ret"] - (COOLANT_SETPOINT + 12)) * 0.8
        gs["junction"] += (junc_target - gs["junction"]) * JUNC_LAG             # fast node
        junc = gs["junction"]
        for srv in servers:
            G_GPU_JUNC.labels(srv["id"]).set(junc + srv["offset"])
            G_GPU_HBM_T.labels(srv["id"]).set(junc + srv["offset"] - 6.0)
        G_RACK_KW.labels(rail).set(rack_kw)
        G_RAIL_UTIL.labels(rail).set(eff * 100.0)
        G_COOLANT_S.labels(rail).set(supply)
        G_COOLANT_R.labels(rail).set(gs["coolant_ret"])
        G_COOLANT_F.labels(rail).set(flow)
        G_CDU_PUMP.labels(rail).set(0.5 + (flow / COOLANT_FLOW_LPM) * 1.5)      # ~0.5-2 kW pump


# One docker-exec per switch gathers EVERYTHING real: per-port stats (8 counters
# each, ports from fabric.json), FIB 'via' count, and BGP summary json — a
# single round-trip per switch.
# DATAPATH-COUNTER GUARD (landmine 2). Which counter source is the TRUTH depends
# on the node, not on the exporter:
#   netdev — docker-sonic-vs: the Linux netdev IS the forwarding path, so
#            /sys/class/net/<port>/statistics is the real traffic signal.
#   sai    — real Tomahawk: that netdev is only the SAI host-interface representor
#            and counts punted CPU traffic, so it would read ~0 with rc=0 and no
#            error. COUNTERS_DB (via sonic-db-cli / redis-cli) is the truth.
#   none   — neither available: publish NOTHING and mark the field ABSENT.
# The netdev branch below is byte-identical to the previous implementation.
_SAI_PORT_FIELDS = [
    ("rx_bytes",    "SAI_PORT_STAT_IF_IN_OCTETS"),
    ("tx_bytes",    "SAI_PORT_STAT_IF_OUT_OCTETS"),
    ("rx_packets",  "SAI_PORT_STAT_IF_IN_UCAST_PKTS"),
    ("tx_packets",  "SAI_PORT_STAT_IF_OUT_UCAST_PKTS"),
    ("rx_dropped",  "SAI_PORT_STAT_IF_IN_DISCARDS"),
    ("tx_dropped",  "SAI_PORT_STAT_IF_OUT_DISCARDS"),
    ("rx_errors",   "SAI_PORT_STAT_IF_IN_ERRORS"),
    ("tx_errors",   "SAI_PORT_STAT_IF_OUT_ERRORS"),
]
_SAI_PFC_FIELDS = ["SAI_PORT_STAT_PFC_3_RX_PKTS", "SAI_PORT_STAT_PFC_4_RX_PKTS"]

# shell shim: prefer sonic-db-cli, fall back to raw redis-cli on the right db index
_DBC_SH = (
    'DBC(){ if command -v sonic-db-cli >/dev/null 2>&1; then sonic-db-cli "$@"; '
    'else db=$1; shift; n=2; if [ "$db" = STATE_DB ]; then n=6; fi; '
    'redis-cli -n $n "$@"; fi; }; '
)


def _sai_port_block(ports):
    """One PORT line per port, read out of COUNTERS_DB. A port with no OID map
    entry emits PORTABSENT so the exporter marks it absent instead of zero."""
    fields = " ".join(f for _k, f in _SAI_PORT_FIELDS) + " " + " ".join(_SAI_PFC_FIELDS)
    return (
        _DBC_SH +
        f'for i in {" ".join(ports)}; do '
        'oid=$(DBC COUNTERS_DB HGET COUNTERS_PORT_NAME_MAP $i 2>/dev/null); '
        'if [ -z "$oid" ]; then echo SAIABSENT $i; continue; fi; '
        f'for f in {fields}; do '
        'v=$(DBC COUNTERS_DB HGET COUNTERS:$oid $f 2>/dev/null); '
        'echo SAI $i $f ${v:-NA}; done; done; '
    )


def _env_block():
    """REAL platform sensors out of STATE_DB (psud/thermalctld). Emits nothing at
    all when the daemons are not publishing — which is exactly the Z9100 case, and
    is why the caller marks those fields ABSENT rather than 0."""
    return (
        _DBC_SH +
        'echo ENVSTART; '
        'DBC STATE_DB KEYS "TEMPERATURE_INFO|*" 2>/dev/null | while read -r k; do '
        'nm=$(echo "${k#TEMPERATURE_INFO|}" | tr " " "_"); '
        'v=$(DBC STATE_DB HGET "$k" temperature 2>/dev/null); '
        '[ -n "$v" ] && echo TEMP "$nm" "$v"; done; '
        'DBC STATE_DB KEYS "FAN_INFO|*" 2>/dev/null | while read -r k; do '
        'nm=$(echo "${k#FAN_INFO|}" | tr " " "_"); '
        'v=$(DBC STATE_DB HGET "$k" speed_rpm 2>/dev/null); '
        '[ -z "$v" ] && v=$(DBC STATE_DB HGET "$k" speed 2>/dev/null); '
        '[ -n "$v" ] && echo FAN "$nm" "$v"; done; '
        'DBC STATE_DB KEYS "PSU_INFO|*" 2>/dev/null | while read -r k; do '
        'nm=$(echo "${k#PSU_INFO|}" | tr " " "_"); '
        'pi=$(DBC STATE_DB HGET "$k" input_power 2>/dev/null); '
        'po=$(DBC STATE_DB HGET "$k" output_power 2>/dev/null); '
        '[ -n "$pi" ] && echo PSUIN "$nm" "$pi"; '
        '[ -n "$po" ] && echo PSUOUT "$nm" "$po"; done; '
        'echo ENVEND; '
    )


def _inner_cmd(ports, marks=False, counter_source="netdev", env=False):
    if counter_source == "sai":
        cmd = _sai_port_block(ports)
    elif counter_source == "none":
        cmd = ""                       # no trustworthy counter source -> ABSENT
    else:
        cmd = (
            f'for i in {" ".join(ports)}; do d=/sys/class/net/$i/statistics; '
            'echo PORT $i $(cat $d/rx_bytes) $(cat $d/tx_bytes) $(cat $d/rx_packets) '
            '$(cat $d/tx_packets) $(cat $d/rx_dropped) $(cat $d/tx_dropped) '
            '$(cat $d/rx_errors) $(cat $d/tx_errors); done; '
        )
    cmd += (
        'echo FIB $(ip route show 2>/dev/null | grep -c via); '
        'echo BGPSTART; timeout 4 vtysh -c "show ip bgp summary json" 2>/dev/null || true; echo BGPEND'
    )
    if env:
        # physical node: read the REAL platform sensors instead of modeling them
        cmd += "; " + _env_block()
    if marks:
        # REAL tc RED-ecn CE marks: emit every 'marked N' from the RED qdisc stats
        # across this switch's fabric netdevs; the exporter sums them. Empty (→0)
        # when no RED qdisc is present, e.g. outside the congestion storm.
        cmd += (
            f'; echo MARKEDSTART; for i in {" ".join(ports)}; do '
            'tc -s qdisc show dev $i 2>/dev/null; done | grep -oE "marked [0-9]+"; '
            'echo MARKEDEND'
        )
    return cmd


def _exec_argv(switch):
    """argv reaching bash on/inside the switch, per fabric.json exec mode."""
    b = _binding(switch)
    csrc = b.get("counter_source", "netdev")
    # tc RED CE marks only exist where the netdev IS the datapath (landmine 3): on
    # real silicon that probe would return rc=0 and a permanent 0, so we skip it
    # and the field is published as ABSENT instead.
    marks = (SWITCH_KIND.get(switch) == "leaf"
             and (b.get("caps") or {}).get("has_netdev_datapath", True))
    env = bool(b.get("physical"))               # physical: read REAL sensors, never model
    body = _inner_cmd(SWITCH_PORTS[switch], marks, counter_source=csrc, env=env)
    inner = ["docker", "exec", CONTAINER_PREFIX + switch, "bash", "-c", body]
    if EXEC_MODE == "limactl":
        return ["limactl", "shell", LIMA_VM, "--", "sudo"] + inner
    if EXEC_MODE == "ssh":
        # Reach the REAL switch over its OOB mgmt IP; run the same probe as root.
        ip = MGMT.get(switch)
        user = FABRIC["exec"].get("user", "admin")
        remote = "sudo bash -c " + shlex.quote(body)
        return ["sshpass", "-p", SW_PW, "ssh",
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=8", "-o", "LogLevel=ERROR",
                f"{user}@{ip}", remote]
    return inner            # docker mode: run as root / docker group


# per-switch state kept across polls (each entry is touched by exactly one
# pool worker per sweep, so no locking is needed)
STATE = {}  # switch -> {"prev_bytes": float, "prev_t": float, "asic": float, "fan": float,
            #            "ecn"/"wred"/"pfc": {interface: float}}


def _init_state(c):
    STATE[c] = {
        "prev_bytes": None,
        "prev_t": None,
        "asic": TEMP_FLOOR,           # thermal inertia init
        "fan": 6000.0,                # fan inertia init
        "ecn": {p: 0.0 for p in SWITCH_PORTS[c]},
        "wred": {p: 0.0 for p in SWITCH_PORTS[c]},
        "pfc": {p: 0.0 for p in SWITCH_PORTS[c]},
    }


def _reset_state():
    """(Re)initialise per-switch state for the CURRENT SWITCHES list — used at
    startup and after a live fabric rebuild (bps baselines + thermal reset)."""
    STATE.clear()
    for c in SWITCHES:
        _init_state(c)


_reset_state()


def _maybe_reload():
    """Stat fabric.json at the top of each poll cycle; if its mtime changed (a live
    rebuild), re-derive SWITCHES / ports / exec and clear per-switch STATE. Never
    crashes on a transient half-written file — we keep the last good fabric and
    DON'T advance FAB_MTIME, so the next cycle retries.

    Safe to mutate the module globals here: it runs at the START of poll(), before
    any worker future is submitted, so no poll_switch worker is touching them."""
    global FABRIC, SWITCH_PORTS, SWITCHES, EXEC_MODE, CONTAINER_PREFIX, MGMT, SWITCH_KIND, FAB_MTIME
    global GPU_SERVERS, GPU_RAILS, BINDINGS
    try:
        m = os.path.getmtime(FABRIC_JSON)
    except OSError:
        return
    if m == FAB_MTIME:
        return
    try:
        with open(FABRIC_JSON) as f:
            fab = json.load(f)
        if "nodes" not in fab or "exec" not in fab:
            raise ValueError("missing nodes/exec")
        mode = fab["exec"].get("mode", "docker")
        if mode not in ("docker", "limactl", "ssh"):
            raise ValueError(f"bad exec mode {mode!r}")
        sp, sw, _mode, _pfx, _mgmt, _kind, _binds = _derive(fab)
        if not sw:
            raise ValueError("no spine/leaf nodes")
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
        # half-written or briefly broken during the rebuild — keep last good, retry
        print(f"fabric.json changed but not yet readable ({e}) — keeping last good",
              file=sys.stderr)
        return
    FABRIC, SWITCH_PORTS, SWITCHES, EXEC_MODE, CONTAINER_PREFIX, MGMT, SWITCH_KIND, BINDINGS = \
        fab, sp, sw, _mode, _pfx, _mgmt, _kind, _binds
    FAB_MTIME = m
    _reset_state()
    GPU_SERVERS, GPU_RAILS = _derive_gpus(fab)
    _reset_gpu_state()
    print(f"fabric.json changed -> reloaded {len(SWITCHES)} switches, "
          f"{len(GPU_SERVERS)} gpu server(s) (exec={EXEC_MODE}, prefix={CONTAINER_PREFIX})",
          file=sys.stderr)


def _step_real_telemetry(c, st):
    """REAL gNMI collection for telemetry:'real' nodes.
    TEMP/PSU/FAN map 1:1 onto this dashboard's existing gauge names via the vendored
    samples_from_state_db(). PFC/ECN/WRED are mapped locally instead of reusing the
    vendored port/queue parsers in gnmi_client.py -- this dashboard's gauge names
    (sonic_*_total, RX-only PFC) differ from the ones those parsers produce, so reusing
    them wholesale would either silently produce unused metrics or wrongly-named
    duplicates. Field names follow the SAI/COUNTERS_DB names, mapped onto this file's
    own gauge names.

    Real switch field names and port naming: not yet verified against hardware. Probe
    a switch by hand first rather than trusting this blind."""
    if not _HAVE_REAL_TELEMETRY:
        print(f"real telemetry for {c}: gnmi_client not importable — skipping", file=sys.stderr)
        return
    host = MGMT.get(c)
    if not host:
        return
    client = st.get("real_client")
    if client is None:
        try:
            client = SonicGnmiClient(host, GNMI_TARGET_PORT)
        except Exception as e:
            print(f"real telemetry for {c}: connect failed: {e}", file=sys.stderr)
            return
        st["real_client"] = client
    try:
        state = client.get("STATE_DB", STATE_PATHS)
        real_gauges = {"sonic_temperature_celsius": G_TEMP, "sonic_psu_output_power_watts": G_PSU_OUT,
                       "sonic_psu_input_power_watts": G_PSU_IN, "sonic_fan_rpm": G_FAN}
        for metric, labels, value in samples_from_state_db(c, state):
            real_gauges[metric].labels(**labels).set(value)

        ports = SWITCH_PORTS.get(c, [])
        counter_paths = [f"COUNTERS/{p}" for p in ports] + [f"COUNTERS/{p}/Queues" for p in ports]
        counters = client.get("COUNTERS_DB", counter_paths)
        for p in ports:
            stats = counters.get(f"COUNTERS/{p}") or {}
            rx_pfc = stats.get("SAI_PORT_STAT_PFC_3_RX_PKTS")
            if rx_pfc is not None:
                G_PFC.labels(c, p).set(float(rx_pfc))
            for qstats in (counters.get(f"COUNTERS/{p}/Queues") or {}).values():
                ecn = qstats.get("SAI_QUEUE_STAT_WRED_ECN_MARKED_PACKETS")
                if ecn is not None:
                    G_ECN.labels(c, p).set(float(ecn))
                wred = qstats.get("SAI_QUEUE_STAT_DROPPED_PACKETS")
                if wred is not None:
                    G_WRED.labels(c, p).set(float(wred))
    except Exception as e:
        st["real_client"] = None   # force reconnect next sweep
        print(f"real telemetry for {c}: {e}", file=sys.stderr)


def _mark_unreachable(c, why):
    """This sweep could not read switch c. Say so (fabric_switch_up=0), flag its values
    absent and drop their series, so a dead switch shows as missing instead of keeping
    its last values or being modeled as a quiet, healthy box. The next good sweep's
    publish_binding() clears the absent flags again."""
    G_UP.labels(c).set(0.0)
    for m in _ABSENCE_TRACKED:
        G_ABSENT.labels(c, m).set(1.0)
    gauges = [G_BGP, G_FIB, G_INTEN, G_LOAD, G_ECN_MARKS, G_TEMP, G_PSU_OUT, G_PSU_IN,
              G_FAN, G_ECN, G_WRED, G_PFC] + list(TRAFFIC.values()) + list(HEALTH.values())
    for g in gauges:
        # one child per label set; the switch is always the first label. _metrics is
        # prometheus_client internals, stable in the pinned 0.21.1.
        for key in list(getattr(g, "_metrics", {})):
            if key and key[0] == c:
                try:
                    g.remove(*key)
                except KeyError:
                    pass
    if c in STATE:
        STATE[c]["prev_bytes"] = None    # the next good read starts a fresh rate
    print(f"{c}: unreachable this sweep ({why}); values marked absent", file=sys.stderr)


def poll_switch(idx, c, now):
    # publish the node's binding + per-metric ABSENCE first, so a node that fails
    # to answer still carries an honest provenance record instead of nothing.
    bind, absent = publish_binding(c)
    try:
        r = subprocess.run(_exec_argv(c), capture_output=True, text=True, timeout=30)
        out = r.stdout
    except Exception as e:
        _mark_unreachable(c, f"read failed: {e.__class__.__name__}")
        return
    if not out.strip():                  # e.g. the container is stopped
        _mark_unreachable(c, (r.stderr or "no output").strip()[:80])
        return
    G_UP.labels(c).set(1.0)

    # ---- parse the single exec output ----
    agg_bytes = 0.0          # sum(rx+tx bytes) over ports -> bps baseline
    have_counters = False    # did ANY port yield a usable counter this sweep?
    sai_acc = {}             # port -> {sai_field: value} (sai counter source)
    sai_absent_ports = []
    env = {"temp": {}, "fan": {}, "psu_in": {}, "psu_out": {}}
    have_env = False
    fib_vias = 0
    bgp_est = 0
    bgp_lines = []
    in_bgp = False
    in_env = False
    ecn_marks = 0            # REAL tc RED CE marks summed over uplink netdevs
    in_marks = False
    have_marks = False       # did this probe include the leaf-only marks section?
    for ln in out.splitlines():
        p = ln.split()
        if not p:
            continue
        tag = p[0]
        if tag == "MARKEDSTART":
            in_marks, have_marks = True, True
            continue
        if tag == "MARKEDEND":
            in_marks = False
            continue
        if in_marks:
            # lines look like 'marked 1234' (grep -oE output)
            if tag == "marked" and len(p) >= 2:
                try:
                    ecn_marks += int(p[1])
                except ValueError:
                    pass
            continue
        if tag == "ENVSTART":
            in_env, have_env = True, True
            continue
        if tag == "ENVEND":
            in_env = False
            continue
        if in_env:
            # REAL platform sensors from STATE_DB: 'TEMP <name> <c>', 'FAN <name>
            # <rpm>', 'PSUIN/PSUOUT <name> <w>'. A sensor the platform does not
            # publish simply never appears -> the field stays ABSENT, never 0.
            if len(p) >= 3:
                try:
                    v = float(p[2])
                except ValueError:
                    continue
                if tag == "TEMP":
                    env["temp"][p[1]] = v
                elif tag == "FAN":
                    env["fan"][p[1]] = v
                elif tag == "PSUIN":
                    env["psu_in"][p[1]] = v
                elif tag == "PSUOUT":
                    env["psu_out"][p[1]] = v
            continue
        if tag == "SAIABSENT" and len(p) >= 2:
            sai_absent_ports.append(p[1])
            continue
        if tag == "SAI" and len(p) >= 4:
            # 'SAI <port> <SAI_FIELD> <value|NA>' — NA means the counter is not
            # reported by this ASIC, which is ABSENT, not zero.
            if p[3] != "NA":
                try:
                    sai_acc.setdefault(p[1], {})[p[2]] = float(p[3])
                except ValueError:
                    pass
            continue
        if tag == "PORT" and len(p) == 10:
            port, rb, tb, rp, tp, rd, td, re_, te = p[1:]
            TRAFFIC["rx_bytes"].labels(c, port).set(float(rb))
            TRAFFIC["tx_bytes"].labels(c, port).set(float(tb))
            TRAFFIC["rx_packets"].labels(c, port).set(float(rp))
            TRAFFIC["tx_packets"].labels(c, port).set(float(tp))
            HEALTH["rx_dropped"].labels(c, port).set(float(rd))
            HEALTH["tx_dropped"].labels(c, port).set(float(td))
            HEALTH["rx_errors"].labels(c, port).set(float(re_))
            HEALTH["tx_errors"].labels(c, port).set(float(te))
            agg_bytes += float(rb) + float(tb)
            have_counters = True
        elif tag == "FIB" and len(p) >= 2:
            try:
                fib_vias = int(p[1])
            except ValueError:
                fib_vias = 0
        elif tag == "BGPSTART":
            in_bgp = True
        elif tag == "BGPEND":
            in_bgp = False
        elif in_bgp:
            bgp_lines.append(ln)

    # ---- SAI counter source: fold COUNTERS_DB into the same gauges -----------
    for port, vals in sai_acc.items():
        for key, field in _SAI_PORT_FIELDS:
            v = vals.get(field)
            if v is None:
                continue                     # not reported by this ASIC -> ABSENT
            (TRAFFIC if key in TRAFFIC else HEALTH)[key].labels(c, port).set(v)
            if key in ("rx_bytes", "tx_bytes"):
                agg_bytes += v
                have_counters = True
        pfc = [vals[f] for f in _SAI_PFC_FIELDS if f in vals]
        if pfc and (bind.get("caps") or {}).get("has_pfc_counters"):
            # REAL PFC rx pause frames off the ASIC (monotonic, same series the
            # emulated path publishes as MODELED — provenance rides the binding).
            G_PFC.labels(c, port).set(sum(pfc))
    if sai_absent_ports:
        print(f"{c}: no COUNTERS_PORT_NAME_MAP entry for {','.join(sai_absent_ports)} "
              f"— datapath counters ABSENT (not zero)", file=sys.stderr)

    # count BGP peers in Established state (json 'state':'Established').
    # docker-sonic-vs vtysh may be slow/absent -> guarded, never stalls loop.
    try:
        bgp_est = "".join(bgp_lines).count('"Established"')
    except Exception:
        bgp_est = 0

    G_FIB.labels(c).set(fib_vias)
    G_BGP.labels(c).set(bgp_est)
    # REAL tc RED-ecn marks — only leaves probe it; 0 when no RED qdisc is armed.
    # On a node whose netdev is a CPU representor the probe is not run at all and
    # the field is published as ABSENT above, so it can never read a silent 0.
    if have_marks:
        G_ECN_MARKS.labels(c).set(ecn_marks)

    # ---- ABSENT vs ZERO: if this node has no trustworthy counter source, publish
    # nothing and say so. A flat 0 here is exactly the landmine we are defusing.
    if not have_counters and not absent.get("load_bps"):
        if bind.get("counter_source") == "none" or bind.get("physical"):
            G_ABSENT.labels(c, "load_bps").set(1.0)
            G_ABSENT.labels(c, "tx_bps").set(1.0)
            G_ABSENT.labels(c, "drops_ps").set(1.0)
            G_ABSENT.labels(c, "intensity").set(1.0)
            print(f"{c}: datapath counters unavailable this sweep — marked ABSENT",
                  file=sys.stderr)
            return

    # ---- REAL -> derived load & intensity ----
    st = STATE[c]
    if st["prev_bytes"] is None or st["prev_t"] is None:
        bps = 0.0                        # first poll => 0
    else:
        dt = now - st["prev_t"]
        dbytes = agg_bytes - st["prev_bytes"]
        # guard counter resets (switch restart) and zero dt
        bps = 8.0 * dbytes / dt if dt > 0 and dbytes >= 0 else 0.0
    st["prev_bytes"] = agg_bytes
    st["prev_t"] = now

    inten_load = _clamp(bps / REF_BPS if REF_BPS > 0 else 0.0)
    if bind.get("modeled_energy", True):
        # emulated: a small idle hum keeps the demo alive at zero traffic
        inten_idle = 0.15 + 0.05 * math.sin(2 * math.pi * now / 120 + idx * 1.7)
        inten = _clamp(max(inten_idle, inten_load))
    else:
        # PHYSICAL: no synthetic hum — intensity is purely the measured load, so a
        # quiet real switch reads quiet instead of inventing a 0.15 floor.
        inten = inten_load

    G_LOAD.labels(c).set(bps)
    G_INTEN.labels(c).set(inten)

    if not bind.get("modeled_energy", True):
        # ── PHYSICAL NODE: never model. Publish the REAL STATE_DB sensors we got,
        # and publish NOTHING (plus absent=1) for the ones this platform does not
        # have. This is the silent-zero guard at its most load-bearing: on a real
        # Z9100 the ASIC temperature is genuinely absent, and a 0.0 here would make
        # the thermal agent's 'temp < CRIT' true forever. ──────────────────────
        asic = _publish_real_env(c, bind, env, have_env)
        print(f"{c} inten={inten:.2f} asic="
              f"{('%.1fC' % asic) if asic is not None else 'ABSENT'} "
              f"load={bps:.0f}bps bgp={bgp_est} vias={fib_vias} "
              f"[physical/{bind.get('counter_source')}]")
        return

    # ---- MODELED energy (driven by REAL intensity, thermal/fan inertia) ----
    asic_target = TEMP_FLOOR + inten * TEMP_SPAN          # 42..78 C
    st["asic"] += (asic_target - st["asic"]) * LAG
    asic = st["asic"]
    cpu = asic * 0.75
    inlet = 24 + 0.3 * math.sin(2 * math.pi * now / 90 + idx)   # small noise
    outlet = asic * 0.6 + 10
    G_TEMP.labels(c, "ASIC").set(asic)
    G_TEMP.labels(c, "CPU").set(cpu)
    G_TEMP.labels(c, "inlet").set(inlet)
    G_TEMP.labels(c, "outlet").set(outlet)

    # Idle floor calibrated to a REAL Dell Z9100-ON: measured 143 W total at idle
    # (64+79 W across 2 PSUs, no optics) via OS9 `show environment`, 19 Aug 2026 —
    # see real-hardware/fixtures/z9100-os9-baseline.json. Was 180 (26% over metal).
    total_watts = 145 + inten * 255                       # 145 (measured idle) .. 400 (loaded)
    psu_out = total_watts / 2.0                           # per PSU (2 PSUs)
    psu_in = psu_out / 0.92
    # Only the per-PSU gauges belong in this loop. Everything below it is
    # per-switch state and must advance ONCE per sweep: until 31 Aug 2026 the fan
    # EMA and the ECN/WRED/PFC accumulators were nested here, so with 2 PSUs they
    # stepped twice per sweep and every modeled congestion rate read 2x high.
    for psu in PSUS:
        G_PSU_OUT.labels(c, psu).set(psu_out)
        G_PSU_IN.labels(c, psu).set(psu_in)

    fan_target = 6000 + max(0.0, asic - 45) * 350
    st["fan"] += (fan_target - st["fan"]) * 0.2
    for fan in FANS:
        G_FAN.labels(c, fan).set(st["fan"])

    # ---- MODELED congestion (monotonic; only above the KNEE) ----
    over = max(0.0, inten - KNEE)
    for port in SWITCH_PORTS[c]:
        if over > 0:
            st["ecn"][port] += (over * 20000) ** 1.2 * INTERVAL
            st["wred"][port] += (over * 900) ** 1.4 * INTERVAL
            st["pfc"][port] += (over * 8000) ** 1.3 * INTERVAL
        G_ECN.labels(c, port).set(st["ecn"][port])
        G_WRED.labels(c, port).set(st["wred"][port])
        G_PFC.labels(c, port).set(st["pfc"][port])

    # one concise line per switch for live tuning
    print(f"{c} inten={inten:.2f} asic={asic:.1f}C load={bps:.0f}bps "
          f"bgp={bgp_est} vias={fib_vias}")


def _pick(d, *needles):
    """First value in d whose key contains any needle (case-insensitive)."""
    for n in needles:
        for k, v in d.items():
            if n.lower() in k.lower():
                return v
    return None


def _publish_real_env(c, bind, env, have_env):
    """Publish REAL platform sensors for a physical node; mark every field the
    platform does not produce ABSENT (and publish no value for it). Returns the
    ASIC temperature or None when it is absent."""
    caps = bind.get("caps") or {}
    temps, fans = env["temp"], env["fan"]

    asic = None
    if caps.get("has_pmon_temp"):
        asic = _pick(temps, "asic", "switch", "npu")
        cpu = _pick(temps, "cpu")
        inlet = _pick(temps, "inlet", "intake", "front")
        outlet = _pick(temps, "outlet", "exhaust", "rear")
        for sensor, val in (("ASIC", asic), ("CPU", cpu),
                            ("inlet", inlet), ("outlet", outlet)):
            if val is not None:
                G_TEMP.labels(c, sensor).set(val)
        # declared present but the sweep returned nothing -> ABSENT, not 0
        G_ABSENT.labels(c, "asic_c").set(0.0 if asic is not None else 1.0)
        G_ABSENT.labels(c, "cpu_c").set(0.0 if cpu is not None else 1.0)
    else:
        G_ABSENT.labels(c, "asic_c").set(1.0)
        G_ABSENT.labels(c, "cpu_c").set(1.0)

    if caps.get("has_pmon_fan"):
        got = False
        for name, rpm in sorted(fans.items()):
            G_FAN.labels(c, name).set(rpm)
            got = True
        G_ABSENT.labels(c, "fan_rpm").set(0.0 if got else 1.0)
    else:
        G_ABSENT.labels(c, "fan_rpm").set(1.0)

    if caps.get("has_psu_power"):
        got = False
        for name, w in sorted(env["psu_in"].items()):
            G_PSU_IN.labels(c, name).set(w)
            got = True
        for name, w in sorted(env["psu_out"].items()):
            G_PSU_OUT.labels(c, name).set(w)
            got = True
        G_ABSENT.labels(c, "power_w").set(0.0 if got else 1.0)
        G_ABSENT.labels(c, "psu_in_w").set(0.0 if got else 1.0)
    else:
        G_ABSENT.labels(c, "power_w").set(1.0)
        G_ABSENT.labels(c, "psu_in_w").set(1.0)

    if not have_env:
        print(f"{c}: STATE_DB sensor read returned nothing — platform sensors "
              f"marked ABSENT (not zero)", file=sys.stderr)
    return asic


def _gnmi_extract_int(val):
    """Pull the integer counter out of a pygnmi json_ietf 'val' — it is either a
    bare scalar or a single-key dict like {'openconfig-interfaces:out-octets': '123'}."""
    if isinstance(val, dict):
        for v in val.values():
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def poll_gnmi_switch(sw):
    """REAL gNMI read for ONE switch: read (polled) its per-port out-octets with a gNMI
    Get to its :8080 telemetry server (OpenConfig, insecure TLS, admin auth). Defensive by construction:
      * only runs in ssh-mode with pygnmi present + a known mgmt IP,
      * hard GNMI_TIMEOUT so it can never stall the sweep,
      * on ANY failure sets fabric_gnmi_up=0 and LEAVES the last-good octets in
        place (a Prometheus gauge keeps its previous value), so a blip never
        publishes a modeled or zeroed number as live.
    One of these runs per switch per sweep, so the whole fabric is read (polled) over
    gNMI Get. This is not a Subscribe stream."""
    if not _HAVE_GNMI or EXEC_MODE != "ssh":
        return
    ip = MGMT.get(sw)
    ports = SWITCH_PORTS.get(sw, [])
    if not ip or not ports:
        return
    paths = ["openconfig-interfaces:interfaces/interface[name=%s]/state/counters/out-octets" % p
             for p in ports]
    try:
        with gNMIclient(target=(ip, GNMI_TARGET_PORT), username=GNMI_USER,
                        password=SW_PW, insecure=False, skip_verify=True,
                        timeout=GNMI_TIMEOUT) as c:
            resp = c.get(path=paths, encoding="json_ietf")
        got = 0
        for notif in (resp or {}).get("notification", []):
            for upd in notif.get("update", []):
                m = re.search(r"name=([^\]]+)", upd.get("path", "") or "")
                if not m:
                    continue
                val = _gnmi_extract_int(upd.get("val"))
                if val is not None:
                    G_GNMI_OCTETS.labels(sw, m.group(1)).set(float(val))
                    got += 1
        G_GNMI_UP.labels(sw).set(1.0 if got else 0.0)
        if got and sw == GNMI_LEAF:   # log just GNMI_LEAF, not 10 lines a sweep
            print("gnmi %s: %d port counter(s) read (polled) over gNMI Get on :%d" % (sw, got, GNMI_TARGET_PORT))
    except Exception as e:          # unreachable / TLS / auth / timeout — never fatal
        G_GNMI_UP.labels(sw).set(0.0)
        print("gnmi %s: %s" % (sw, e), file=sys.stderr)


def poll(executor):
    _maybe_reload()          # pick up a live rebuild before this sweep
    now = time.time()
    # start the gNMI reads first so they get workers immediately (one per switch —
    # the whole fabric is read (polled) over gNMI Get), then the SSH sweep
    futures = [executor.submit(poll_gnmi_switch, c) for c in SWITCHES]
    futures += [executor.submit(poll_switch, idx, c, now)
                for idx, c in enumerate(SWITCHES)]
    for f in futures:
        try:
            f.result()
        except Exception as e:          # poll_switch guards itself; belt & braces
            print("worker:", e, file=sys.stderr)
    try:
        model_gpus(now)                 # MODELED GPU-cluster energy (after the switch barrier)
    except Exception as e:
        print("gpu model:", e, file=sys.stderr)


def _clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


if __name__ == "__main__":
    if "--check" in sys.argv:           # config smoke-test for CI / fabgen --validate
        print(f"fabric={FABRIC.get('name', '?')} exec={EXEC_MODE} "
              f"prefix={CONTAINER_PREFIX} switches={len(SWITCHES)}")
        for _c in SWITCHES:
            _b = _binding(_c)
            _a = sorted((_caps.absent_metrics(_b) if _caps else {}))
            print(f"  {_c}: {' '.join(SWITCH_PORTS[_c])}")
            print(f"      binding: nos={_b['nos']} platform={_b['platform'] or '-'} "
                  f"transport={_b['transport']} physical={_b['physical']} "
                  f"counters={_b['counter_source']} absent={','.join(_a) or 'none'}")
        sys.exit(0)
    start_http_server(9200, addr="127.0.0.1")   # only Prometheus on this machine reads it
    print(f"fabric exporter on 127.0.0.1:9200/metrics  ({len(SWITCHES)} switches, "
          f"exec={EXEC_MODE}, INTERVAL={INTERVAL}s REF_BPS={REF_BPS:.0f})")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        while True:
            try:
                poll(pool)
            except Exception as e:
                print("loop:", e, file=sys.stderr)
            time.sleep(INTERVAL)
