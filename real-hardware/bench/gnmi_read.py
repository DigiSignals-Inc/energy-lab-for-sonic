#!/usr/bin/env python3
"""
Read the switches over gNMI instead of SSH.

The dashboard has always read its numbers by SSHing to a switch and running
sonic-db-cli. That works, but the point is reading power and thermals over gNMI, so the
dashboard should actually use gNMI. Both paths read the SAME Redis databases — gNMI is a
different front door, not different data — so this is a transport swap, nothing
about the numbers changes.

WHAT IS PROVEN HERE (31 Aug 2026, against the live switches):
  - gNMI runs on both boxes on :8080 with --noTLS --allow_no_client_auth, on
    built-in defaults. There is no TELEMETRY or GNMI table in CONFIG_DB at all.
  - A Get returns PSU power, every temperature sensor, every fan, and the port
    and queue counters.
  - The DB is selected with pygnmi's `target=` argument, NOT by putting the DB
    name in the path. "STATE_DB/PSU_INFO" fails with "Node STATE_DB not found";
    path=["PSU_INFO"], target="STATE_DB" works.
  - gNMI Subscribe (streaming) is NOT proven. It was seen once, from a single
    gnmi-probe-hw.py run on 2026-08-20 (the fixtures record it), and not repeated.
    gnmi_cli on the box always does
    TLS against a noTLS server, and testing from outside the lab LAN failed for
    transport reasons that also broke Get, so those runs proved nothing either
    way. Do not claim streaming.

SAFETY: every read falls back to the SSH path on any failure, so a gNMI problem
can never take the dashboard down. The dashboard reports how many reads came from
which path, so the claim is visible rather than asserted.
"""
import os
import time

try:
    from pygnmi.client import gNMIclient
    HAVE_GNMI = True
except Exception:                                    # library absent -> pure SSH
    HAVE_GNMI = False

# gNMI target per switch: the host part of BENCH_SWA / BENCH_SWB (the same settings
# the dashboard SSHes to; see .env.example) on BENCH_GNMI_PORT. Unset means the alias
# name itself (swa/swb), which only works if it resolves; otherwise reads fall back to SSH.
GNMI_PORT = int(os.environ.get("BENCH_GNMI_PORT") or "8080")
GNMI_HOSTS = {h: ((os.environ.get("BENCH_" + h.upper()) or h).rsplit("@", 1)[-1], GNMI_PORT)
              for h in ("swa", "swb")}
TIMEOUT = 8

# Counted per poll so the UI can show "14 over gNMI, 0 fell back" rather than
# just asserting the transport.
stats = {"gnmi": 0, "fallback": 0, "last_error": None}


def reset_stats():
    stats["gnmi"] = 0
    stats["fallback"] = 0


def available():
    return HAVE_GNMI


def _get(host, path, target):
    """One gNMI Get. Returns the decoded value, or None on any failure."""
    if not HAVE_GNMI or host not in GNMI_HOSTS:
        return None
    try:
        with gNMIclient(target=GNMI_HOSTS[host], insecure=True, timeout=TIMEOUT) as c:
            r = c.get(path=[path], target=target, encoding="json_ietf")
        return r["notification"][0]["update"][0]["val"]
    except Exception as e:
        stats["last_error"] = f"{host}/{path}: {type(e).__name__}"
        return None


def platform(host):
    """PSU watts, all temperatures (with thresholds) and all fans, over gNMI.

    Returns the same shape as the SSH poller so it is a drop-in, or None if any
    part fails — partial data would be worse than falling back cleanly.
    """
    psu_raw = _get(host, "PSU_INFO", "STATE_DB")
    tmp_raw = _get(host, "TEMPERATURE_INFO", "STATE_DB")
    fan_raw = _get(host, "FAN_INFO", "STATE_DB")
    if not isinstance(psu_raw, dict) or not isinstance(tmp_raw, dict) or not isinstance(fan_raw, dict):
        stats["fallback"] += 1
        return None

    def f(v):
        try:
            return float(v)
        except Exception:
            return None

    psu = {k: f(v.get("power")) for k, v in psu_raw.items() if isinstance(v, dict)}
    temp = {k: f(v.get("temperature")) for k, v in tmp_raw.items() if isinstance(v, dict)}
    thr = {k: [v.get("high_threshold", ""), v.get("critical_high_threshold", "")]
           for k, v in tmp_raw.items() if isinstance(v, dict)}
    fan = {k: (f(v.get("speed")), v.get("status")) for k, v in fan_raw.items() if isinstance(v, dict)}
    vals = [v[0] for v in fan.values() if v[0] is not None]
    # On this platform every healthy fan reports status False, so count them
    # rather than treating the field as meaningful.
    bad = sum(1 for v in fan.values() if str(v[1]).lower() != "true")

    stats["gnmi"] += 1
    return {
        "psu": psu,
        "psu_total": round(sum(v for v in psu.values() if v is not None), 1),
        "temp": temp,
        "thr": thr,
        "fan": {k: v[0] for k, v in fan.items()},
        "fan_min": min(vals, default=None),
        "fan_max": max(vals, default=None),
        "fan_n": len(fan),
        "fan_bad": bad,
        "via": "gNMI",
    }


# --- STAGE 2: the congestion counters -------------------------------------
# These drive the live charts, so they are read the same way but with the same
# hard rule: any failure returns None and the caller falls back to SSH.
#
# The oid map is fetched once and cached — port oids do not change while the
# switch is up, and re-fetching it every 2 s would double the work for nothing.
_oid_cache = {}


def _oids(host):
    if host in _oid_cache:
        return _oid_cache[host]
    ports = _get(host, "COUNTERS_PORT_NAME_MAP", "COUNTERS_DB")
    queues = _get(host, "COUNTERS_QUEUE_NAME_MAP", "COUNTERS_DB")
    if not isinstance(ports, dict):
        return None
    _oid_cache[host] = {"port": ports, "queue": queues if isinstance(queues, dict) else {}}
    return _oid_cache[host]


def counters(host, egress, ingress, queue=3):
    """Port and queue counters for one switch, over gNMI.

    Mirrors what SW_POLL returns so the poll loop can use either. Returns None
    if anything is missing rather than a half-filled dict.
    """
    m = _oids(host)
    if not m:
        stats["fallback"] += 1
        return None
    eg, ing = m["port"].get(egress), m["port"].get(ingress)
    q = m["queue"].get(f"{egress}:{queue}")
    if not eg or not ing:
        stats["fallback"] += 1
        return None

    # Port oper_status lives in APPL_DB, not COUNTERS_DB. The UI's "switch up/down"
    # badge reads this field; when the dashboard moved from SSH to gNMI the field
    # was simply not carried over, so the badge read "down" on a switch that was
    # forwarding 20 Gb/s. Absence must not look like failure.
    # NOTE the separator: gNMI addresses this as PORT_TABLE/<port>, not with the
    # Redis key syntax PORT_TABLE:<port>. The colon form returns an empty dict
    # rather than an error, so it fails silently and oper reads as absent.
    port = _get(host, f"PORT_TABLE/{egress}", "APPL_DB")
    oper = port.get("oper_status") if isinstance(port, dict) else None

    ce = _get(host, f"COUNTERS/{eg}", "COUNTERS_DB")
    ci = _get(host, f"COUNTERS/{ing}", "COUNTERS_DB")
    cq = _get(host, f"COUNTERS/{q}", "COUNTERS_DB") if q else {}
    if not isinstance(ce, dict) or not isinstance(ci, dict):
        stats["fallback"] += 1
        return None
    cq = cq if isinstance(cq, dict) else {}

    def i(d, k):
        try:
            return int(d.get(k, 0))
        except Exception:
            return 0

    stats["gnmi"] += 1
    return {
        "out_oct":  i(ce, "SAI_PORT_STAT_IF_OUT_OCTETS"),
        # The SAME two counter hashes also hold the OPPOSITE directions, so the
        # reverse-path mode (pc1 -> pc2, which enters on the egress port and
        # leaves on the ingress port) costs no extra reads. Without these the
        # reverse flow reads as zero traffic while it is plainly running.
        "eg_in_oct":   i(ce, "SAI_PORT_STAT_IF_IN_OCTETS"),
        "ing_out_oct": i(ci, "SAI_PORT_STAT_IF_OUT_OCTETS"),
        "out_disc": i(ce, "SAI_PORT_STAT_IF_OUT_DISCARDS"),
        "in_oct":   i(ci, "SAI_PORT_STAT_IF_IN_OCTETS"),
        "pfc3_tx":  i(ci, "SAI_PORT_STAT_PFC_3_TX_PKTS"),
        "pfc3_rx":  i(ci, "SAI_PORT_STAT_PFC_3_RX_PKTS"),
        "q3_pkts":  i(cq, "SAI_QUEUE_STAT_PACKETS"),
        "q3_drop":  i(cq, "SAI_QUEUE_STAT_DROPPED_PACKETS"),
        "q3_wm":    i(cq, "SAI_QUEUE_STAT_SHARED_WATERMARK_BYTES"),
        # Now that the WRED_ECN_QUEUE_STAT group is enabled this is the standard
        # SAI counter, no longer the Broadcom SDK back door.
        "q3_ecn":   i(cq, "SAI_QUEUE_STAT_WRED_ECN_MARKED_PACKETS"),
        # None (not "down") when the read did not return it — the UI treats a
        # missing value as unknown rather than as a failed link.
        "oper": oper,
        "via": "gNMI",
    }


def selftest():
    """Print what each switch answers. Run this on the dashboard host."""
    if not HAVE_GNMI:
        print("  pygnmi not installed"); return
    for h in GNMI_HOSTS:
        t0 = time.time()
        p = platform(h)
        dt = (time.time() - t0) * 1000
        if p:
            print(f"  {h}: platform OK in {dt:.0f} ms — PSU {p['psu_total']} W, "
                  f"{len(p['temp'])} temps, {p['fan_n']} fans ({p['fan_bad']} 'Not OK')")
        else:
            print(f"  {h}: platform FAILED after {dt:.0f} ms — {stats['last_error']}")
    t0 = time.time()
    c = counters("swb", "Ethernet8", "Ethernet0")
    dt = (time.time() - t0) * 1000
    if c:
        print(f"  swb: counters OK in {dt:.0f} ms — in {c['in_oct']}, out {c['out_oct']}, "
              f"pfc3_tx {c['pfc3_tx']}, ecn {c['q3_ecn']}, wm {c['q3_wm']}")
    else:
        print(f"  swb: counters FAILED after {dt:.0f} ms — {stats['last_error']}")


if __name__ == "__main__":
    selftest()
