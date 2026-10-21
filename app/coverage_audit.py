# -*- coding: utf-8 -*-
"""
coverage_audit — the LIVE COVERAGE AUDIT for the Command Center.

Runs a telemetry-coverage audit against ONE live SONiC leaf: for a fixed CATALOG
of energy/telemetry fields (grouped by category) it probes the switch's OWN redis
STATE_DB / COUNTERS_DB / CONFIG_DB and a couple of `show`/vtysh CLIs over a single
batched SSH command, then classifies each field as:

  POPULATED — key exists AND value is non-empty / non-zero   (real observed data)
  NULL      — key/framework exists but the value is 0 / empty (present, not backed)
  ABSENT    — no key at all                                   (feature not present)

Each field also carries the LABELED expectation for a real merchant-silicon
SONiC hardware platform with full PMON (labeled, not probed) so the matrix contrasts
"what this virtual switch can produce" against "what a real box fills in". The
grey/red rows are the point: on a vs (asic_type=vs, no ASIC, no PMON) the
hardware-only fields (PMON temp/fan/PSU, SAI PFC/ECN/WRED counters) cannot be
populated — which is the point of the audit.

Provenance honesty: every field is labeled with the ACTUAL source it was read from
(redis STATE_DB db6 / COUNTERS_DB db2 / CONFIG_DB db4 / APPL_DB db0 / kernel
/sys/class/net / FRR vtysh CLI). We never claim gNMI for an SSH/redis read.

The module is transport-agnostic: build_audit(target, probe_fn) takes a callable
probe_fn(remote_str) -> (rc, stdout, stderr) that runs ONE sudo-capable remote
shell string on the target and never raises. app.py wires that to SSH or docker exec;
tests can wire it to anything. Nothing here imports app.py, so importing this
module can never break app.py startup.
"""
import json
import re
import time

# The reference sensor port + the redis DB numbers on this SONiC build.
_REF_PORT = "Ethernet0"
_DB_APPL, _DB_COUNTERS, _DB_CONFIG, _DB_STATE = 0, 2, 4, 6

# A real merchant-silicon box we contrast against (labeled, not probed).
PLATFORM_REF = "a typical hardware SONiC switch with full PMON (expected, not probed)"

# One batched, sudo-capable remote command. Every probe is wrapped in its own
# marker so a single SSH round-trip fills the whole matrix; parsing is defensive
# (missing / errored sections just read as absent/empty, never raise).
def _remote_cmd(port=_REF_PORT):
    st, cn, cf, ap = _DB_STATE, _DB_COUNTERS, _DB_CONFIG, _DB_APPL
    S = "/sys/class/net/%s/statistics" % port
    return (
        'echo @@ASIC; redis-cli -n %d hget "DEVICE_METADATA|localhost" asic_type; '
        'echo @@HWSKU; redis-cli -n %d hget "DEVICE_METADATA|localhost" hwsku; '
        'echo @@PADMIN; redis-cli -n %d hget "PORT_TABLE|%s" admin_status; '
        'echo @@POPER; redis-cli -n %d hget "PORT_TABLE|%s" netdev_oper_status; '
        'echo @@PSPEED; redis-cli -n %d hget "PORT_TABLE|%s" oper_speed; '
        'echo @@RXB; cat %s/rx_bytes 2>/dev/null; '
        'echo @@TXB; cat %s/tx_bytes 2>/dev/null; '
        'echo @@RXP; cat %s/rx_packets 2>/dev/null; '
        'echo @@TXP; cat %s/tx_packets 2>/dev/null; '
        'echo @@CNTMAP; redis-cli -n %d hlen COUNTERS_PORT_NAME_MAP; '
        'echo @@QMAP; redis-cli -n %d hlen COUNTERS_QUEUE_NAME_MAP; '
        'echo @@CNTSIZE; redis-cli -n %d dbsize; '
        'echo @@WRED; redis-cli -n %d keys "WRED_PROFILE|*" 2>/dev/null | grep -c .; '
        # ── ACTUAL SAI counter VALUES (resolve the port/queue OID via the name map,
        #    then HGET the counter row). Empty when the map is empty (vs) → reads 0. ──
        'echo @@ECNVAL; o=$(redis-cli -n %d hget COUNTERS_PORT_NAME_MAP %s); '
        '[ -n "$o" ] && redis-cli -n %d hget "COUNTERS:$o" SAI_PORT_STAT_ECN_MARKED_PACKETS; '
        'echo @@PFCVAL; o=$(redis-cli -n %d hget COUNTERS_PORT_NAME_MAP %s); '
        '[ -n "$o" ] && redis-cli -n %d hget "COUNTERS:$o" SAI_PORT_STAT_PFC_3_RX_PKTS; '
        'echo @@QDROPVAL; q=$(redis-cli -n %d hget COUNTERS_QUEUE_NAME_MAP %s:3); '
        '[ -n "$q" ] && redis-cli -n %d hget "COUNTERS:$q" SAI_QUEUE_STAT_DROPPED_PACKETS; '
        'echo @@BGP; sudo vtysh -c "show bgp summary json" 2>/dev/null; '
        'echo @@ROUTES; redis-cli -n %d keys "ROUTE_TABLE:*" 2>/dev/null | grep -c .; '
        'echo @@KROUTES; ip route 2>/dev/null | grep -c .; '
        'echo @@VTUN; redis-cli -n %d keys "VXLAN_TUNNEL_TABLE|*" 2>/dev/null | grep -c .; '
        'echo @@EVPN; sudo vtysh -c "show bgp l2vpn evpn summary" 2>/dev/null; '
        'echo @@TEMP; redis-cli -n %d keys "TEMPERATURE_INFO|*" 2>/dev/null | grep -c .; '
        'echo @@FAN; redis-cli -n %d keys "FAN_INFO|*" 2>/dev/null | grep -c .; '
        'echo @@PSU; redis-cli -n %d keys "PSU_INFO|*" 2>/dev/null | grep -c .; '
        'echo @@END'
    ) % (cf, cf, st, port, st, port, st, port, S, S, S, S,
         cn, cn, cn, cf,
         cn, port, cn, cn, port, cn, cn, port, cn,
         ap, st, st, st, st)


def _split_markers(text):
    """Fold one combined stdout into {MARKER: 'section text'} by our @@ echoes."""
    out, cur = {}, None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("@@") and " " not in s:
            cur = s[2:]
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def _to_int(text, default=0):
    """First integer in a chunk of text (defensive: empty/None/garbage -> default)."""
    if text is None:
        return default
    m = re.search(r"-?\d+", str(text))
    return int(m.group(0)) if m else default


def _bgp_established(section):
    """(peers, established) from `show bgp summary json`. Defensive on any shape."""
    try:
        data = json.loads(section)
    except Exception:
        return 0, 0
    uni = data.get("ipv4Unicast") or data.get("default", {}).get("ipv4Unicast") or {}
    peers = uni.get("peers") or {}
    total = uni.get("peerCount", len(peers))
    estab = 0
    for p in peers.values():
        st = str(p.get("state", "")).lower()
        if st == "established" or (not st and p.get("peerState") == "OK"):
            estab += 1
    return int(total or 0), estab


def _evpn_established(section):
    """(peers, established) from `show bgp l2vpn evpn summary` FRR footer/rows."""
    peers = estab = 0
    for line in (section or "").splitlines():
        low = line.strip().lower()
        if low.startswith("total number of neighbors established"):
            estab = _to_int(low)
        elif low.startswith("total number of neighbors"):
            peers = _to_int(low)
    if not peers:                          # fall back: count neighbor rows w/ AS
        for line in (section or "").splitlines():
            toks = line.split()
            if toks and re.match(r"^Ethernet\d+$", toks[0]) and len(toks) > 2:
                peers += 1
                estab += 1                 # unnumbered rows shown here are up
    return peers, estab


# ── The CATALOG ─────────────────────────────────────────────────────────────
# Each field is metadata + a `rule(v)` that maps the parsed probe dict `v` to a
# (status, observed) pair. platform_expected: does a typical hardware platform populate it?
def _st(status, observed):
    return status, observed


def _cat_port():
    return {
        "name": "Port counters",
        "blurb": "Per-port bytes/packets moved — the base traffic signal.",
        "fields": [
            {"field": "rx_bytes", "source": "kernel /sys/class/net (SAI COUNTERS_DB db2 empty on vs)",
             "platform_expected": True,
             "platform_note": "real ASIC: SAI_PORT_STAT_IF_IN_OCTETS in COUNTERS_DB",
             "note": "Real bytes received, read from the kernel netdev counter.",
             "rule": lambda v: _st("POPULATED" if v["rxb"] > 0 else "NULL", "%d bytes" % v["rxb"])},
            {"field": "tx_bytes", "source": "kernel /sys/class/net (SAI COUNTERS_DB db2 empty on vs)",
             "platform_expected": True,
             "platform_note": "real ASIC: SAI_PORT_STAT_IF_OUT_OCTETS in COUNTERS_DB",
             "note": "Real bytes transmitted, read from the kernel netdev counter.",
             "rule": lambda v: _st("POPULATED" if v["txb"] > 0 else "NULL", "%d bytes" % v["txb"])},
            {"field": "rx_packets", "source": "kernel /sys/class/net (SAI COUNTERS_DB db2 empty on vs)",
             "platform_expected": True,
             "platform_note": "real ASIC: SAI_PORT_STAT_IF_IN_UCAST_PKTS in COUNTERS_DB",
             "note": "Real packets received, read from the kernel netdev counter.",
             "rule": lambda v: _st("POPULATED" if v["rxp"] > 0 else "NULL", "%d pkts" % v["rxp"])},
            {"field": "tx_packets", "source": "kernel /sys/class/net (SAI COUNTERS_DB db2 empty on vs)",
             "platform_expected": True,
             "platform_note": "real ASIC: SAI_PORT_STAT_IF_OUT_UCAST_PKTS in COUNTERS_DB",
             "note": "Real packets transmitted, read from the kernel netdev counter.",
             "rule": lambda v: _st("POPULATED" if v["txp"] > 0 else "NULL", "%d pkts" % v["txp"])},
        ],
    }


def _cat_ifstate():
    return {
        "name": "Interface state",
        "blurb": "Admin/oper state and speed of each port.",
        "fields": [
            {"field": "admin_status", "source": "redis STATE_DB db6 PORT_TABLE|%s" % _REF_PORT,
             "platform_expected": True, "platform_note": "same key on real HW",
             "note": "Configured up/down, from the switch's own STATE_DB.",
             "rule": lambda v: _st("POPULATED" if v["padmin"] else "ABSENT", v["padmin"] or "(none)")},
            {"field": "oper_status", "source": "redis STATE_DB db6 PORT_TABLE|%s" % _REF_PORT,
             "platform_expected": True, "platform_note": "same key on real HW",
             "note": "Live link state (netdev_oper_status), from STATE_DB.",
             "rule": lambda v: _st("POPULATED" if v["poper"] else "ABSENT", v["poper"] or "(none)")},
            {"field": "oper_speed", "source": "redis STATE_DB db6 PORT_TABLE|%s" % _REF_PORT,
             "platform_expected": True, "platform_note": "same key on real HW",
             "note": "Negotiated speed in Mb/s, from STATE_DB.",
             "rule": lambda v: _st("POPULATED" if v["pspeed"] else "NULL", (v["pspeed"] or "0") + " Mb/s")},
        ],
    }


def _cat_routing():
    return {
        "name": "Routing / BGP",
        "blurb": "Underlay control plane — eBGP sessions and installed routes.",
        "fields": [
            {"field": "bgp_peers_established", "source": "FRR vtysh CLI (show bgp summary json)",
             "platform_expected": True, "platform_note": "same FRR stack on real HW",
             "note": "eBGP neighbors in Established state, live from FRR.",
             "rule": lambda v: _st("POPULATED" if v["bgp_estab"] > 0 else "NULL",
                                   "%d/%d up" % (v["bgp_estab"], v["bgp_peers"]))},
            {"field": "fib_routes", "source": "kernel FIB / ip route (APPL_DB ROUTE_TABLE empty on vs)",
             "platform_expected": True,
             "platform_note": "real ASIC: routes in APPL_DB ROUTE_TABLE, programmed to SAI/hardware",
             "note": "Routes in the forwarding table — the kernel is the dataplane on a vs; "
                     "the SAI-backed APPL_DB ROUTE_TABLE path is empty here.",
             "rule": lambda v: _st("POPULATED" if v["kroutes"] > 0 else "NULL", "%d routes" % v["kroutes"])},
        ],
    }


def _cat_overlay():
    return {
        "name": "Overlay / EVPN",
        "blurb": "VXLAN tenant fabric and its BGP-EVPN control plane.",
        "fields": [
            {"field": "vxlan_tunnels", "source": "redis STATE_DB db6 VXLAN_TUNNEL_TABLE|*",
             "platform_expected": True, "platform_note": "same key on real HW",
             "note": "Live leaf-to-leaf VTEP tunnels, from STATE_DB.",
             "rule": lambda v: _st("POPULATED" if v["vtun"] > 0 else "NULL", "%d tunnels" % v["vtun"])},
            {"field": "evpn_peers_established", "source": "FRR vtysh CLI (show bgp l2vpn evpn summary)",
             "platform_expected": True, "platform_note": "same FRR stack on real HW",
             "note": "EVPN neighbors established, live from FRR.",
             "rule": lambda v: _st("POPULATED" if v["evpn_estab"] > 0 else "NULL",
                                   "%d/%d up" % (v["evpn_estab"], v["evpn_peers"]))},
        ],
    }


def _cat_temp():
    return {
        "name": "Temperature",
        "blurb": "Board / ASIC thermal sensors — PMON territory.",
        "fields": [
            {"field": "TEMPERATURE_INFO", "source": "redis STATE_DB db6 TEMPERATURE_INFO|* (PMON)",
             "platform_expected": True,
             "platform_note": "PMON thermalctld populates per-sensor temperature + high/crit thresholds",
             "note": "No PMON on a virtual switch, so no key exists at all.",
             "rule": lambda v: _st("POPULATED" if v["temp"] > 0 else "ABSENT",
                                   "%d sensors" % v["temp"])},
        ],
    }


def _cat_fan():
    return {
        "name": "Fan",
        "blurb": "Fan tach / presence — PMON territory.",
        "fields": [
            {"field": "FAN_INFO", "source": "redis STATE_DB db6 FAN_INFO|* (PMON)",
             "platform_expected": True,
             "platform_note": "PMON thermalctld populates fan RPM, presence, direction",
             "note": "No PMON on a virtual switch, so no key exists at all.",
             "rule": lambda v: _st("POPULATED" if v["fan"] > 0 else "ABSENT",
                                   "%d fans" % v["fan"])},
        ],
    }


def _cat_psu():
    return {
        "name": "PSU / Power",
        "blurb": "Power-supply input/output watts — the energy signal PMON reads.",
        "fields": [
            {"field": "PSU_INFO", "source": "redis STATE_DB db6 PSU_INFO|* (PMON)",
             "platform_expected": True,
             "platform_note": "PMON psud populates input/output power (W), voltage, current per PSU",
             "note": "No PMON on a virtual switch, so no key exists — power is MODELED separately, "
                     "not read from a sensor.",
             "rule": lambda v: _st("POPULATED" if v["psu"] > 0 else "ABSENT",
                                   "%d PSUs" % v["psu"])},
        ],
    }


def _cat_pfc():
    return {
        "name": "PFC",
        "blurb": "Priority-flow-control pause frames per class — congestion signal.",
        "fields": [
            {"field": "pfc_pause_rx", "source": "redis COUNTERS_DB db2 HGET COUNTERS:<oid> SAI_PORT_STAT_PFC_3_RX_PKTS",
             "platform_expected": True,
             "platform_note": "real ASIC: SAI_PORT_STAT_PFC_x_RX_PKTS per priority",
             "note": "CLI `show pfc counters` works but the SAI counter map is empty on a vs, so the "
                     "resolved SAI_PORT_STAT_PFC_3_RX_PKTS value is 0 — framework present, no data.",
             # Classify off the ACTUAL counter value: POPULATED only when >0; NULL when
             # present-but-0 OR the name map is empty (oid unresolvable → value 0).
             "rule": lambda v: _st("POPULATED" if v["pfc_val"] > 0 else "NULL",
                                   "SAI_PORT_STAT_PFC_3_RX_PKTS=%d (port map hlen=%d)"
                                   % (v["pfc_val"], v["cntmap"]))},
        ],
    }


def _cat_ecn():
    return {
        "name": "ECN",
        "blurb": "Explicit-congestion-notification marked packets — the WRED/ECN signal.",
        "fields": [
            {"field": "ecn_marked_packets", "source": "redis COUNTERS_DB db2 HGET COUNTERS:<oid> SAI_PORT_STAT_ECN_MARKED_PACKETS",
             "platform_expected": True,
             "platform_note": "real ASIC: SAI_PORT_STAT_ECN_MARKED_PACKETS / per-queue WRED-ECN drops",
             "note": "WRED/ECN config framework exists but the SAI ECN counter is empty on a vs, so the "
                     "resolved SAI_PORT_STAT_ECN_MARKED_PACKETS value is 0. (The storm scenario's REAL "
                     "marks come from kernel tc RED — a separate, non-ASIC path.)",
             # ECN's OWN probe: classify off the actual SAI_PORT_STAT_ECN_MARKED_PACKETS value.
             "rule": lambda v: _st("POPULATED" if v["ecn_val"] > 0 else "NULL",
                                   "SAI_PORT_STAT_ECN_MARKED_PACKETS=%d (WRED_PROFILE keys=%d)"
                                   % (v["ecn_val"], v["wred"]))},
        ],
    }


def _cat_queue():
    return {
        "name": "Queue / WRED",
        "blurb": "Per-queue depth and WRED tail-drops — buffer-pressure signal.",
        "fields": [
            {"field": "queue_wred_drops", "source": "redis COUNTERS_DB db2 HGET COUNTERS:<qoid> SAI_QUEUE_STAT_DROPPED_PACKETS",
             "platform_expected": True,
             "platform_note": "real ASIC: SAI_QUEUE_STAT_DROPPED_PACKETS / WRED_DROPPED_PACKETS per queue",
             "note": "CLI `show queue counters` works but the SAI queue map is empty on a vs, so the "
                     "resolved SAI_QUEUE_STAT_DROPPED_PACKETS value is 0.",
             # Classify off the ACTUAL per-queue drop value: POPULATED only when >0.
             "rule": lambda v: _st("POPULATED" if v["qdrop_val"] > 0 else "NULL",
                                   "SAI_QUEUE_STAT_DROPPED_PACKETS=%d (queue map hlen=%d)"
                                   % (v["qdrop_val"], v["qmap"]))},
        ],
    }


CATALOG = [
    _cat_port(), _cat_ifstate(), _cat_routing(), _cat_overlay(),
    _cat_temp(), _cat_fan(), _cat_psu(), _cat_pfc(), _cat_ecn(), _cat_queue(),
]


def _parse(out):
    """Parse the combined probe stdout into the flat value dict the rules consume."""
    s = _split_markers(out)
    bgp_peers, bgp_estab = _bgp_established(s.get("BGP", ""))
    evpn_peers, evpn_estab = _evpn_established(s.get("EVPN", ""))
    return {
        "asic": s.get("ASIC", "").strip(),
        "hwsku": s.get("HWSKU", "").strip(),
        "padmin": s.get("PADMIN", "").strip(),
        "poper": s.get("POPER", "").strip(),
        "pspeed": s.get("PSPEED", "").strip(),
        "rxb": _to_int(s.get("RXB")), "txb": _to_int(s.get("TXB")),
        "rxp": _to_int(s.get("RXP")), "txp": _to_int(s.get("TXP")),
        "cntmap": _to_int(s.get("CNTMAP")), "qmap": _to_int(s.get("QMAP")),
        "cntsize": _to_int(s.get("CNTSIZE")), "wred": _to_int(s.get("WRED")),
        # ACTUAL SAI counter values (0 when the name map is empty / value pinned 0)
        "ecn_val": _to_int(s.get("ECNVAL")), "pfc_val": _to_int(s.get("PFCVAL")),
        "qdrop_val": _to_int(s.get("QDROPVAL")),
        "bgp_peers": bgp_peers, "bgp_estab": bgp_estab,
        "routes": _to_int(s.get("ROUTES")), "kroutes": _to_int(s.get("KROUTES")),
        "vtun": _to_int(s.get("VTUN")),
        "evpn_peers": evpn_peers, "evpn_estab": evpn_estab,
        "temp": _to_int(s.get("TEMP")), "fan": _to_int(s.get("FAN")),
        "psu": _to_int(s.get("PSU")),
    }


def build_audit(target, probe_fn, port=_REF_PORT):
    """Run the coverage audit against `target` via probe_fn(remote)->(rc,out,err).

    Never raises. On SSH/parse failure every field falls back to a labeled
    UNKNOWN so the matrix still renders and the failure is visible, not hidden.
    Returns the JSON-able audit dict described in this module's docstring.
    """
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ok, err = False, None
    v = None
    try:
        rc, out, e = probe_fn(_remote_cmd(port))
        if rc == 0 and out:
            v = _parse(out)
            ok = True
        else:
            err = (e or "probe returned rc=%s" % rc).strip()[:200]
    except Exception as ex:                       # belt-and-braces; must not raise
        err = str(ex)[:200]

    pop = nul = ab = 0
    categories = []
    for cat in CATALOG:
        fields = []
        for f in cat["fields"]:
            status, observed = "UNKNOWN", (err or "probe unavailable")
            if ok:
                try:
                    status, observed = f["rule"](v)
                except Exception as ex:           # per-probe guard
                    status, observed = "UNKNOWN", "parse error: %s" % str(ex)[:80]
            if status == "POPULATED":
                pop += 1
            elif status == "NULL":
                nul += 1
            elif status == "ABSENT":
                ab += 1
            fields.append({
                "field": f["field"],
                "live_status": status,
                "observed": observed,
                "source": f["source"],
                "platform_expected": bool(f["platform_expected"]),
                "platform_note": f["platform_note"],
                "note": f["note"],
            })
        categories.append({"name": cat["name"], "blurb": cat.get("blurb", ""), "fields": fields})

    total = pop + nul + ab
    coverage = round(100.0 * pop / total, 1) if total else 0.0
    fixture_label = (
        "REPRESENTATIVE PLATFORM FIXTURE (labeled, NOT probed): merchant-silicon "
        "reference %s. The 'platform_expected' column is the labeled expectation "
        "for that box; only the 'live_status' column is observed live from the "
        "target below." % PLATFORM_REF
    )
    note = (
        "Observed live (over SSH or docker exec) from the switch's own redis (STATE_DB db6 / "
        "COUNTERS_DB db2 / CONFIG_DB db4 / APPL_DB db0), kernel /sys/class/net, "
        "and FRR vtysh — NOT precomputed. Grey (NULL) rows have the framework but "
        "no data on a virtual switch (asic_type=vs, no SAI counters); red (ABSENT) "
        "rows have no key at all because there is no PMON. A real platform (%s) "
        "fills every row — that gap is the point." % PLATFORM_REF
    )
    if not ok:
        note = "PROBE FAILED (%s) — matrix shows the catalog with UNKNOWN status. " % err + note

    return {
        "target": target,
        "port_probed": port,
        "generated": generated,
        "live_reachable": ok,
        "hwsku": (v or {}).get("hwsku", "") if ok else "",
        "asic_type": (v or {}).get("asic", "") if ok else "",
        "categories": categories,
        "summary": {"populated": pop, "null": nul, "absent": ab, "coverage_pct": coverage},
        "fixture_label": fixture_label,
        "note": note,
    }
