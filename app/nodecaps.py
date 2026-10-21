#!/usr/bin/env python3
"""Node binding + capability manifest — the ONE place a platform difference lives.

Shared, stdlib-only, imported defensively by BOTH processes (app/app.py and
app/collector/fabric_exporter.py) so the Command Center and the exporter can never
disagree about what a node can measure. If this module is missing or broken the
callers fall back to today's behaviour, so it can never stop the demo.

WHY THIS EXISTS — the three landmines:

  1. SILENT ZERO. On a real Dell Z9100-ON running community SONiC the ASIC
     temperature is ABSENT (pmon_daemon_control.json ships skip_thermalctld:true,
     so TEMPERATURE_INFO never reaches STATE_DB). A pipeline that publishes 0.0
     for a missing sensor makes 'temp < AGENT_CRIT' true forever: the thermal
     agent never drains, never logs, never errors — it silently dies. ABSENT must
     therefore be a first-class state, distinct from a real measured zero.

  2. DATAPATH COUNTERS. On docker-sonic-vs the Linux netdev IS the datapath, so
     /sys/class/net/<port>/statistics is the true traffic signal. On real
     Tomahawk silicon that netdev is only the SAI host-interface representor and
     counts punted CPU traffic — every bps/drop/error reads ~0 with rc=0 and no
     error. Real nodes must read SAI counters out of COUNTERS_DB instead, and
     mark the field ABSENT when neither source is available.

  3. tc RED / netem. Both act on that same netdev, so on real silicon they return
     rc=0 and never touch the ASIC datapath. Scenarios that depend on them must
     refuse on a physical node rather than reporting success.

SCHEMA (all fields optional; every default reproduces today's behaviour exactly)

    {
      "id": "SPINE-1", "kind": "spine", ...,
      "site":     "lab-1",                      # free-form location tag
      "platform": "dell-z9100-on",              # selects the capability defaults
      "nos":      "community-sonic",            # community-sonic|enterprise-sonic|virtual
      "transport":"ssh",                        # ssh|gnmi|exec
      "physical": true,                         # real metal (no modeling allowed)
      "counter_source": "sai",                  # sai|netdev  (defaulted from physical)
      "capabilities": {                         # per-capability overrides
        "has_pmon_temp": false, "has_pmon_fan": false,
        "has_psu_power": true,
        "has_sai_counters": true, "has_pfc_counters": true
      },
      "endpoints": {"ssh": {...}, "gnmi": {...}}
    }

Nothing here implements the hardware swap — this is the schema + defaults the
swap will later flip.
"""

# ── the capability flags, in a fixed order (the API and the UI iterate this) ──
CAP_FLAGS = (
    "has_pmon_temp",
    "has_pmon_fan",
    "has_psu_power",
    "has_sai_counters",
    "has_pfc_counters",
)

# extra, non-mandated flags kept alongside (same shape, same override path)
EXTRA_FLAGS = (
    "has_sai_ecn",        # SAI_QUEUE_STAT_WRED_ECN_MARKED_PACKETS actually reported
    "has_netdev_datapath",  # the Linux netdev IS the forwarding path (vs a representor)
)

ALL_FLAGS = CAP_FLAGS + EXTRA_FLAGS

# ── metric ids the pipeline can mark ABSENT, and the capability each needs ─────
# Keys are the legacy per-switch field names used by build_context()/ui.html so
# the mapping needs no translation layer anywhere.
METRIC_CAP = {
    "asic_c":     "has_pmon_temp",
    "cpu_c":      "has_pmon_temp",
    "inlet_c":    "has_pmon_temp",
    "outlet_c":   "has_pmon_temp",
    "fan_rpm":    "has_pmon_fan",
    "power_w":    "has_psu_power",
    "psu_in_w":   "has_psu_power",
    "tx_bps":     "_counters",
    "load_bps":   "_counters",
    "drops_ps":   "_counters",
    "intensity":  "_counters",
    "ecn_ps":     "_congestion",
    "ecn_marks":  "_ecn_marks",
}

# human-readable, verbatim-displayable reasons. The UI never learns these words —
# it prints whatever string arrives in `note`.
REASONS = {
    "has_pmon_temp": (
        "this platform ships pmon_daemon_control.json with skip_thermalctld:true "
        "(fans are driven autonomously by the board FPGA), so TEMPERATURE_INFO "
        "is absent from STATE_DB, and so from gNMI, until thermalctld is enabled "
        "(see real-hardware/HARDWARE.md; there is no ASIC die sensor either way)"),
    "has_pmon_fan": (
        "this platform ships pmon_daemon_control.json with skip_thermalctld:true, "
        "so FAN_INFO is absent from STATE_DB until thermalctld is enabled"),
    "has_psu_power": "psud is not publishing PSU_INFO on this platform",
    "has_sai_counters": (
        "no SAI counters: COUNTERS_DB is not populated and the front-panel netdev "
        "is only a CPU representor on this platform"),
    "has_pfc_counters": (
        "PFC counters require 'counterpoll port enable' on this platform"),
    "has_sai_ecn": (
        "SAI ECN-marked counters appear only after 'sudo counterpoll wredqueue enable' "
        "(the WRED_ECN_QUEUE_STAT group ships disabled)"),
    "_ecn_marks": (
        "kernel tc RED CE marks do not exist on real silicon — the front-panel "
        "netdev is a CPU representor, not the datapath"),
    "_congestion": (
        "modeled congestion is not published for a physical node — real ECN/WRED "
        "must come from COUNTERS_DB"),
}

# ── per-platform / per-NOS capability defaults ────────────────────────────────
# EVERY emulated node (every node fabgen generates) resolves
# to _VIRTUAL, whose flags are all True — so nothing about today's pipeline moves.
_VIRTUAL = {
    "has_pmon_temp": True,          # MODELED by the exporter from real load
    "has_pmon_fan": True,           # MODELED
    "has_psu_power": True,          # MODELED
    "has_sai_counters": False,      # docker-sonic-vs does NOT populate COUNTERS_DB
    "has_pfc_counters": True,       # MODELED
    "has_sai_ecn": True,            # MODELED
    "has_netdev_datapath": True,    # the netdev IS the datapath on a vs
}

# Dell Z9100-ON on community SONiC 202505 — see real-hardware/bench/bench-knowledge.md
_Z9100_COMMUNITY = {
    "has_pmon_temp": False,
    "has_pmon_fan": False,
    "has_psu_power": True,          # psud -> PSU_INFO every 3s, real SMF PMC register
    "has_sai_counters": True,
    "has_pfc_counters": True,
    "has_sai_ecn": False,           # off until 'counterpoll wredqueue enable'; supported on Tomahawk-1 (bench, 2026-09)
    "has_netdev_datapath": False,   # front-panel netdev is the SAI host-if representor
}

# a physical box running Enterprise SONiC (fuller PMON coverage). Used only when a node
# declares "nos": "enterprise-sonic" in fabric.json.
_PHYSICAL_ENTERPRISE = {
    "has_pmon_temp": True,
    "has_pmon_fan": True,
    "has_psu_power": True,
    "has_sai_counters": True,
    "has_pfc_counters": True,
    "has_sai_ecn": True,
    "has_netdev_datapath": False,
}

PLATFORM_DEFAULTS = {
    "dell-z9100-on": _Z9100_COMMUNITY,
    "z9100-on": _Z9100_COMMUNITY,
    "z9100": _Z9100_COMMUNITY,
}

NOS_DEFAULTS = {
    "virtual": _VIRTUAL,
    "enterprise-sonic": _PHYSICAL_ENTERPRISE,
    "community-sonic": _Z9100_COMMUNITY,
}

VALID_NOS = ("community-sonic", "enterprise-sonic", "virtual")
VALID_TRANSPORT = ("ssh", "gnmi", "exec")


def _norm(v, allowed, default):
    v = (str(v or "").strip().lower()) if v is not None else ""
    return v if v in allowed else default


def node_binding(node, exec_mode="docker"):
    """Resolve ONE fabric.json node dict into its full hardware binding.

    Pure + defensive: never raises, never reads the network, never touches disk.
    Defaults are chosen so a node object exactly as fabgen emits it today (no
    site/platform/nos/physical keys at all) resolves to the emulated behaviour
    that fabgen generates.
    """
    node = node or {}
    nid = node.get("id") or ""
    kind = node.get("kind") or ""
    physical = bool(node.get("physical", False))

    # NOS: declared > inferred. A physical node with no declaration is assumed to
    # be community SONiC (what the Z9100-ONs run); anything else is the virtual image.
    nos = _norm(node.get("nos"), VALID_NOS, "")
    if not nos:
        nos = "community-sonic" if physical else "virtual"

    platform = (node.get("platform") or node.get("hw") or "").strip()
    transport = _norm(node.get("transport"), VALID_TRANSPORT,
                      "ssh" if exec_mode == "ssh" else "exec")

    # capability defaults: platform manifest > NOS manifest > virtual.
    # An EMULATED node always resolves to _VIRTUAL whatever it claims to model,
    # because the exporter models its energy — this is the rule that keeps the
    # generated fabrics unchanged whatever `hw` label they carry.
    if not physical:
        base = dict(_VIRTUAL)
    else:
        base = dict(PLATFORM_DEFAULTS.get(platform.lower())
                    or NOS_DEFAULTS.get(nos)
                    or _Z9100_COMMUNITY)

    # explicit per-node overrides win over everything
    caps = dict(base)
    overrides = node.get("capabilities")
    if not isinstance(overrides, dict):        # tolerate a malformed spec field
        overrides = {}
    for k in ALL_FLAGS:
        if k in overrides:
            caps[k] = bool(overrides[k])
        elif k in node:                     # also accept flat has_* keys
            caps[k] = bool(node[k])

    # counter source: declared > derived. netdev only where the netdev really is
    # the datapath; sai on real silicon.
    csrc = (node.get("counter_source") or "").strip().lower()
    if csrc not in ("netdev", "sai"):
        csrc = "netdev" if caps.get("has_netdev_datapath", not physical) else "sai"
    if csrc == "sai" and not caps.get("has_sai_counters"):
        csrc = "none"                      # neither source available -> ABSENT

    endpoints = node.get("endpoints")
    if not isinstance(endpoints, dict):
        endpoints = {}
    return {
        "id": nid,
        "kind": kind,
        "site": node.get("site") or "default",
        "platform": platform,
        "nos": nos,
        "transport": transport,
        "physical": physical,
        "counter_source": csrc,
        "caps": caps,
        "driver": node.get("driver") or ("sonic-%s" % nos),
        "endpoints": endpoints,
        # a physical node is NEVER modeled — the exporter must publish real
        # readings or ABSENT, never a synthesized number under a real badge.
        "modeled_energy": not physical,
    }


def bindings_for(nodes, exec_mode="docker", kinds=None):
    """{node_id: binding} for a list of fabric.json node dicts."""
    out = {}
    for n in nodes or []:
        if kinds and n.get("kind") not in kinds:
            continue
        nid = n.get("id")
        if nid:
            out[nid] = node_binding(n, exec_mode)
    return out


def metric_absent(binding, metric):
    """(absent: bool, reason: str) for ONE metric on ONE node.

    This is the single decision point behind the silent-zero guard: everything
    downstream (exporter publish, snapshot value, agent input, UI badge) asks
    this and nothing else.
    """
    if not binding:
        return False, ""
    caps = binding.get("caps") or {}
    need = METRIC_CAP.get(metric)
    if need is None:
        return False, ""
    if need == "_counters":
        if binding.get("counter_source") == "none":
            return True, REASONS["has_sai_counters"]
        return False, ""
    if need == "_congestion":
        if binding.get("physical") and not caps.get("has_sai_ecn"):
            return True, REASONS["_congestion"]
        return False, ""
    if need == "_ecn_marks":
        # kernel tc RED CE marks: only real where the netdev is the datapath
        if not caps.get("has_netdev_datapath", True):
            return True, REASONS["_ecn_marks"]
        return False, ""
    if not caps.get(need, True):
        return True, REASONS.get(need, "not available on this platform")
    return False, ""


def absent_metrics(binding):
    """{metric: reason} for every metric this node cannot produce."""
    out = {}
    for m in METRIC_CAP:
        gone, why = metric_absent(binding, m)
        if gone:
            out[m] = why
    return out


# ── action catalogue: what a node class can actually be made to do ────────────
# Every scenario declares the action ids it needs; a node that cannot support one
# makes the scenario UNAVAILABLE, with the reason surfaced verbatim.
def action_support(binding):
    """{action_id: {supported, reason, alternative}} for one node binding."""
    b = binding or {}
    caps = b.get("caps") or {}
    netdev = bool(caps.get("has_netdev_datapath", True))
    physical = bool(b.get("physical"))
    transport = b.get("transport", "exec")

    def a(ok, reason="", alt=None, caveat=""):
        d = {"supported": bool(ok), "reason": reason}
        if alt:
            d["alternative"] = alt
        if caveat:
            d["caveat"] = caveat
        return d

    netdev_reason = (
        "the front-panel netdev is a CPU representor on real silicon — "
        "tc/netem returns rc=0 and never touches the ASIC datapath")

    return {
        "tc_red_ecn": a(netdev, "" if netdev else netdev_reason,
                        alt="wred_retune"),
        "netem_loss": a(netdev, "" if netdev else netdev_reason,
                        alt="port_error_inject"),
        "container_kill": a(transport == "exec",
                            "" if transport == "exec"
                            else "no container to kill — this node is reached over %s"
                                 % transport,
                            alt="port_shutdown"),
        "port_shutdown": a(True),
        "bgp_graceful_shutdown": a(True),
        "ping_flood": a(True, caveat=(
            "CoPP caps CPU-sourced traffic (~6000 pps) on real silicon, so a "
            "switch-sourced ping cannot congest a 100G port — use a host NIC"
        ) if physical else ""),
        "wred_retune": a(bool(caps.get("has_sai_counters")),
                         "" if caps.get("has_sai_counters")
                         else "no SAI queue/WRED counters on this node",
                         caveat="Tomahawk honours only the GREEN WRED profile"
                         if physical else ""),
    }
