#!/usr/bin/env python3
"""Energy Lab Command Center — dashboard, JSON API and assistant (stdlib only).

Serves :9300. Reads the live fabric picture from Prometheus (:9090) and lets an
engineer ask "what's happening / why is it hot / is this normal / what would you
do". Answers come from a pluggable BRAIN (COPILOT_BRAIN env): 'offline' (default,
rules-based, no network), 'openai' (Groq/OpenAI-compatible), or 'azure-anthropic'.
ANY brain error falls back to the offline responder so a live demo never hard-fails.

Topology is NOT hardcoded: it is loaded from fabric.json — the single source of
truth emitted by fabgen.py. Point FABRIC_JSON at it (default: ../build/fabric.json
next to this file). Exec mode (docker vs limactl) and the overload demo target
come from the same file, so the app works unmodified on Linux, in a Lima VM on
macOS, or in WSL2.

Heat / power / congestion shown here are MODELED by the exporter from REAL measured
traffic on a virtual switch (docker-sonic-vs has no physical sensors) — so flooding
the fabric genuinely pushes modeled ASIC temp up. ASIC thresholds: warn 62C, crit 72C.

Design: dark theme. Other sites can't frame the page (X-Frame-Options: SAMEORIGIN).
Security: it listens on localhost only unless COPILOT_BIND and COPILOT_TOKEN say otherwise
(see the config section), because anyone who can reach it can drive the lab.

Stdlib only: http.server, socketserver, urllib, json, os, math, threading.
"""
import concurrent.futures
import hmac
import http.cookies
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from http.server import HTTPServer

# The live coverage-audit core lives beside this file. Import defensively so a
# missing/broken module can NEVER stop the app from serving — /api/audit just
# reports it's unavailable in that case.
try:
    import coverage_audit as _covaudit
except Exception as _e:  # pragma: no cover
    _covaudit = None
    print("coverage_audit import failed (%s) — /api/audit disabled" % _e, file=sys.stderr)

# The node-binding / capability manifest, shared verbatim with the exporter so the
# two processes can never disagree about what a node can measure. Imported
# defensively: without it every node resolves to today's emulated behaviour.
try:
    import nodecaps as _nc
except Exception as _e:  # pragma: no cover
    _nc = None
    print("nodecaps import failed (%s) — every node treated as emulated" % _e, file=sys.stderr)

# ─── config ──────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("COPILOT_PORT", "9300"))
# Where the Command Center listens. Anyone who can reach it can rebuild the lab, kill
# switches and start traffic floods, so the default is this machine only. Lima (macOS)
# and WSL2 (Windows) forward the Linux VM's localhost to yours, so your browser still
# reaches it. To open it to a network, set COPILOT_BIND=0.0.0.0 and COPILOT_TOKEN to a
# long random string, then open http://<host>:<port>/?token=<that string> once.
BIND = os.environ.get("COPILOT_BIND", "127.0.0.1").strip() or "127.0.0.1"
TOKEN = os.environ.get("COPILOT_TOKEN", "").strip()
LOCAL_NAMES = ("localhost", "127.0.0.1", "::1")
MAX_BODY = 64 * 1024      # bytes; every POST here is a small JSON object
PROM_URL = os.environ.get("PROM_URL", "http://127.0.0.1:9090")
BRAIN = os.environ.get("COPILOT_BRAIN", "offline").strip().lower()
FLOW_THRESHOLD_BPS = float(os.environ.get("FLOW_THRESHOLD_BPS", "5000"))
# GPU servers (kind=="gpu") render on the Live map as green GPU racks under their rail
# leaf (ui.html knows the kind), AND in the GPU Pods tab. Env override GPU_ON_MAP=0 hides
# them from the map (kept for a clean fallback / older ui.html).
GPU_ON_MAP = os.environ.get("GPU_ON_MAP", "1").strip().lower() in ("1", "true", "yes")

# ── in-UI Fabric Architect (live rebuild) config ─────────────────────────────
# hard ranges fabgen itself enforces (kept in sync with fabgen's MAX_* limits)
FAB_MIN = {"spines": 1, "leaves": 1, "hosts_per_leaf": 1}
FAB_MAX = {"spines": 8, "leaves": 16, "hosts_per_leaf": 4}
# how many switches (spines+leaves) THIS host can actually deploy — bigger fabrics
# preview fine but need a bigger machine. Below the cap, POST /api/fabric/apply is allowed.
DEPLOY_MAX_SWITCHES = int(os.environ.get("DEPLOY_MAX_SWITCHES", "8"))
# repo root = parent of this app/ dir; fabgen.py + rebuild-fabric.sh live in virtual-fabric/
_DS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FABGEN = os.environ.get("FABGEN", os.path.join(_DS_DIR, "virtual-fabric", "fabgen.py"))
REBUILD_CMD = os.environ.get("REBUILD_CMD", os.path.join(_DS_DIR, "virtual-fabric", "rebuild-fabric.sh"))

# ASIC temperature thresholds for the modeled thermal view
ASIC_WARN = 62.0
ASIC_CRIT = 72.0

# ── modeled hardware catalog (verbatim mirror of fabgen.HARDWARE) ─────────────
# HONESTY: the emulated switch is ALWAYS docker-sonic-vs. A "hardware" pick is a
# MODELED label + capacity input (radix / port speed / ASIC), NOT a different
# container — nothing about the deployment changes. This is the authoritative
# copy the /api/fabric/hardware endpoint serves and the capacity math uses.
HARDWARE = [
    {"id": "th3-32x400", "vendor": "Edgecore", "product": "AS9716-32D", "asic": "Broadcom Tomahawk 3", "bw_tbps": 12.8, "radix": 32, "port_speed_gbps": 400, "buffer": "shallow", "role": "any"},
    {"id": "th4-64x400", "vendor": "Edgecore", "product": "AS9736-64D", "asic": "Broadcom Tomahawk 4", "bw_tbps": 25.6, "radix": 64, "port_speed_gbps": 400, "buffer": "shallow", "role": "any"},
    {"id": "th5-64x800", "vendor": "Celestica", "product": "DS5000", "asic": "Broadcom Tomahawk 5", "bw_tbps": 51.2, "radix": 64, "port_speed_gbps": 800, "buffer": "shallow", "role": "any"},
    {"id": "th6-64x1600", "vendor": "(ref)", "product": "Tomahawk 6 platform", "asic": "Broadcom Tomahawk 6", "bw_tbps": 102.4, "radix": 64, "port_speed_gbps": 1600, "buffer": "shallow", "role": "any"},
    {"id": "spectrum4-sn5600", "vendor": "NVIDIA", "product": "SN5600", "asic": "Spectrum-4", "bw_tbps": 51.2, "radix": 64, "port_speed_gbps": 800, "buffer": "shallow", "role": "any"},
    {"id": "jericho2c-deepbuf", "vendor": "Arista", "product": "7280R3", "asic": "Broadcom Jericho2c+", "bw_tbps": 9.6, "radix": 24, "port_speed_gbps": 400, "buffer": "deep", "role": "edge"},
]
HW_BY_ID = {m["id"]: m for m in HARDWARE}
DEFAULT_LEAF_MODEL = "th4-64x400"
DEFAULT_SPINE_MODEL = "th5-64x800"
DEFAULT_BREAKOUT = 1
DEFAULT_RADIX = 64


def hw_label(m):
    return "%s %s · %d×%dG · %s" % (m["vendor"], m["product"], m["radix"],
                                    m["port_speed_gbps"], m["asic"])


def hw_model(model_id):
    """A COPY of the catalog model dict (+ derived 'label'), or None if unknown."""
    m = HW_BY_ID.get(model_id)
    if not m:
        return None
    d = dict(m)
    d["label"] = hw_label(m)
    return d


def compute_capacity(S, L, H, Rl, Rs, leaf_speed, spine_speed,
                     breakout=1, mode="ai"):
    """AUTHORITATIVE capacity math — the exact model the UI recomputes client-side.
    Returns every headline figure + the 4 verdict chips (LEAF/SPINE/SCALE/REDUND)."""
    S, L, H = int(S), int(L), int(H)
    Rl, Rs = int(Rl), int(Rs)
    breakout = int(breakout) or 1
    leaf_speed, spine_speed = float(leaf_speed), float(spine_speed)

    uplinks_per_leaf = S
    downlinks_per_leaf = Rl - S
    max_hosts_per_leaf = downlinks_per_leaf * breakout
    total_gpus = L * H
    max_gpus_2tier = L * max_hosts_per_leaf
    spare_leaf_slots = Rs - L
    denom = S * spine_speed
    oversub_ratio = (H * leaf_speed) / denom if denom else float("inf")
    non_blocking = oversub_ratio <= 1.0
    ecmp_width = S
    lose_one_spine_pct = 100.0 / S if S else 0.0
    two_tier_ceiling = (min(Rl, Rs) ** 2) // 2
    target_oversub = 1.0 if mode == "ai" else 3.0

    def _r(x):
        return round(x, 2)

    verdicts = {}
    if H >= max_hosts_per_leaf:
        verdicts["leaf"] = {"level": "crit", "text": "leaf downlinks full — add a leaf"}
    elif H >= 0.8 * max_hosts_per_leaf:
        verdicts["leaf"] = {"level": "warn", "text": "leaf ~full"}
    else:
        verdicts["leaf"] = {"level": "ok", "text": "leaf downlinks have headroom"}

    if oversub_ratio > target_oversub:
        lvl = "crit" if oversub_ratio > 2 * target_oversub else "warn"
        verdicts["spine"] = {"level": lvl,
                             "text": "oversubscribed %g:1 vs target %g:1 — add a spine"
                                     % (_r(oversub_ratio), target_oversub)}
    else:
        verdicts["spine"] = {"level": "ok", "text": "non-blocking / within target"}

    if L >= Rs:
        verdicts["scale"] = {"level": "crit",
                             "text": "at spine radix (%d) — go 3-tier (pods+super-spine)" % Rs}
    elif L >= 0.8 * Rs:
        verdicts["scale"] = {"level": "warn", "text": "approaching spine radix — plan a 3rd tier"}
    else:
        verdicts["scale"] = {"level": "ok", "text": "within spine radix"}

    if S < 2:
        verdicts["redund"] = {"level": "crit", "text": "single spine = no redundancy"}
    else:
        verdicts["redund"] = {"level": "ok",
                              "text": "lose-1-spine = %g%% capacity" % _r(lose_one_spine_pct)}

    return {
        "uplinks_per_leaf": uplinks_per_leaf,
        "downlinks_per_leaf": downlinks_per_leaf,
        "max_hosts_per_leaf": max_hosts_per_leaf,
        "total_gpus": total_gpus,
        "max_gpus_2tier": max_gpus_2tier,
        "spare_leaf_slots": spare_leaf_slots,
        "oversub_ratio": _r(oversub_ratio),
        "non_blocking": non_blocking,
        "ecmp_width": ecmp_width,
        "lose_one_spine_pct": _r(lose_one_spine_pct),
        "two_tier_ceiling": two_tier_ceiling,
        "target_oversub": target_oversub,
        "mode": mode,
        "inputs": {"S": S, "L": L, "H": H, "leaf_radix": Rl, "spine_radix": Rs,
                   "leaf_speed": leaf_speed, "spine_speed": spine_speed,
                   "breakout": breakout},
        "verdicts": verdicts,
    }


# ─── fabric.json — the single source of truth ────────────────────────────────
def _fabric_path():
    p = os.environ.get("FABRIC_JSON", "").strip()
    if p:
        return p
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "build", "fabric.json"))


def load_fabric():
    """Load + sanity-check fabric.json. Exits with a CLEAR message (no traceback)
    when it's missing/broken — the most common way to mislaunch this app."""
    path = _fabric_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            fab = json.load(f)
    except OSError as e:
        sys.exit("copilot: cannot read fabric.json at %s (%s)\n"
                 "  → generate a build with fabgen.py, or set FABRIC_JSON=/path/to/fabric.json" % (path, e))
    except ValueError as e:
        sys.exit("copilot: %s is not valid JSON (%s)" % (path, e))
    for key in ("name", "nodes", "links"):
        if key not in fab:
            sys.exit("copilot: %s is missing required key '%s' — regenerate with fabgen.py" % (path, key))
    return fab, path


# All fabric-derived module globals are (re)computed by reload_fabric() below.
# They are declared here (placeholders) so static readers know the surface; the
# real values — and every value after a live rebuild — come from reload_fabric().
# A single RLock guards the whole re-derivation so a mid-reload never yields a
# half-updated fabric to build_topology / scenarios / the agent.
_fab_lock = threading.RLock()

FABRIC = {}
FABRIC_PATH = _fabric_path()
FAB_MTIME = 0.0
FABRIC_NAME = ""
PARAMS = {}
TOPOLOGY_NODES = []
TOPOLOGY_LINKS = []
KIND = {}
SPINES = []
LEAVES = []
UPLINKS = {}
HOST_PORTS = {}
ASN = {}
HOSTS = []
HOST_IP = {}
HOSTS_OF_LEAF = {}
EXEC = {}
CONTAINER_PREFIX = ""
FAB_HW = {}
SYSTEM_PROMPT = ""
OVERLOAD_CFG = {}
COLLECTIVE_CFG = {}
FLOOD = None
CONFIGURE_SH = ""
# HARDWARE-READY NODE BINDING (see nodecaps.py): {node_id: binding}. Re-derived by
# reload_fabric() alongside every other fabric global, so a one-click swap that
# rewrites fabric.json flips provenance live without a restart.
BINDINGS = {}
SWITCH_KINDS = ("spine", "leaf", "access")


def node_binding(nid):
    """Binding for one node id; a permissive emulated default when unresolved, so
    every caller can read it unconditionally."""
    b = BINDINGS.get(nid)
    if b:
        return b
    return {"id": nid, "kind": KIND.get(nid, ""), "site": "default", "platform": "",
            "nos": "virtual", "transport": "exec", "physical": False,
            "counter_source": "netdev", "caps": {}, "driver": "sonic-virtual",
            "endpoints": {}, "modeled_energy": True}


def declared_absent(nid):
    """{metric: reason} this node's platform cannot produce. {} for every emulated
    node, which is the whole fleet today."""
    if _nc is None:
        return {}
    try:
        return _nc.absent_metrics(node_binding(nid))
    except Exception:
        return {}


def node_actions(nid):
    """{action_id: {supported, reason, ...}} for one node."""
    if _nc is None:
        return {}
    try:
        return _nc.action_support(node_binding(nid))
    except Exception:
        return {}


def reload_fabric(reset_state=True):
    """(Re)derive EVERY fabric-dependent global from fabric.json on disk.

    Called once at startup and again whenever fabric.json changes (a live
    rebuild, see maybe_reload()). Holds _fab_lock for the whole re-derivation so
    readers that snapshot under the same lock always see a consistent fabric.
    When reset_state is True (any reload AFTER the first) the scenario state and
    the agent's drained-set are cleared — the old node names may be gone."""
    global FABRIC, FABRIC_PATH, FAB_MTIME, FABRIC_NAME, PARAMS
    global TOPOLOGY_NODES, TOPOLOGY_LINKS, KIND, SPINES, LEAVES
    global UPLINKS, HOST_PORTS, ASN, HOSTS, HOST_IP, HOSTS_OF_LEAF
    global EXEC, CONTAINER_PREFIX, FAB_HW, SYSTEM_PROMPT, OVERLOAD_CFG, COLLECTIVE_CFG, FLOOD, CONFIGURE_SH
    global BINDINGS
    with _fab_lock:
        fab, path = load_fabric()
        FABRIC, FABRIC_PATH = fab, path
        try:
            FAB_MTIME = os.path.getmtime(path)
        except OSError:
            FAB_MTIME = 0.0
        FABRIC_NAME = FABRIC["name"]
        PARAMS = FABRIC.get("params", {})
        TOPOLOGY_NODES = FABRIC["nodes"]
        TOPOLOGY_LINKS = FABRIC["links"]
        KIND = {n["id"]: n.get("kind", "") for n in TOPOLOGY_NODES}
        SPINES = [n["id"] for n in TOPOLOGY_NODES if n.get("kind") == "spine"]
        LEAVES = [n["id"] for n in TOPOLOGY_NODES if n.get("kind") == "leaf"]

        # derived wiring tables (from links, NOT positional guesses):
        #   UPLINKS[leaf]  = [(port, spine), ...]   leaf-side uplink ports
        #   HOST_PORTS[leaf] = [port, ...]          leaf-side host-facing ports
        UPLINKS = {lf: [] for lf in LEAVES}
        HOST_PORTS = {lf: [] for lf in LEAVES}
        for _L in TOPOLOGY_LINKS:
            ka, kb = KIND.get(_L["a"]), KIND.get(_L["b"])
            if ka == "leaf" and kb == "spine":
                UPLINKS[_L["a"]].append((_L["ap"], _L["b"]))
            elif ka == "spine" and kb == "leaf":
                UPLINKS[_L["b"]].append((_L["bp"], _L["a"]))
            elif ka == "host" and kb == "leaf":
                HOST_PORTS[_L["b"]].append(_L["bp"])
            elif ka == "leaf" and kb == "host":
                HOST_PORTS[_L["a"]].append(_L["ap"])

        # more derived tables (all from fabric.json, never positional):
        ASN = {n["id"]: n.get("asn") for n in TOPOLOGY_NODES}
        HOSTS = [n["id"] for n in TOPOLOGY_NODES if n.get("kind") == "host"]
        HOST_IP = {n["id"]: n.get("ip") for n in TOPOLOGY_NODES if n.get("kind") == "host"}
        HOSTS_OF_LEAF = {lf: [] for lf in LEAVES}
        for _L in TOPOLOGY_LINKS:
            ka, kb = KIND.get(_L["a"]), KIND.get(_L["b"])
            if ka == "host" and kb == "leaf":
                HOSTS_OF_LEAF.setdefault(_L["b"], []).append(_L["a"])
            elif ka == "leaf" and kb == "host":
                HOSTS_OF_LEAF.setdefault(_L["a"], []).append(_L["b"])

        # exec mode: how to reach the fabric containers from where this app runs
        EXEC = FABRIC.get("exec", {})
        CONTAINER_PREFIX = EXEC.get("container_prefix", "clab-%s-" % FABRIC_NAME)

        # modeled-hardware block (leaf/spine model dicts + breakout); {} for a
        # pre-hardware fabric.json so old builds still load cleanly.
        FAB_HW = FABRIC.get("hardware") or {}

        # per-node hardware binding + capability manifest. Every node in today's
        # fabric.json resolves to the emulated defaults (all sensors present,
        # counter_source=netdev), so this is a pure addition until a node declares
        # "physical": true.
        if _nc is not None:
            try:
                BINDINGS = _nc.bindings_for(TOPOLOGY_NODES, EXEC.get("mode", "docker"))
            except Exception as _be:
                BINDINGS = {}
                print("nodecaps: binding derivation failed (%s)" % _be, file=sys.stderr)
        else:
            BINDINGS = {}

        # things that depend on the freshly-set globals above
        def _tier_sentence():
            """Describe the fabric from the ACTUAL node kinds, in tier order.

            This used to be hardcoded as "N spines and M leaves", which silently
            dropped the ToR access switches of a 3-tier fabric — so the
            copilot confidently described a 3-tier fabric as 2-tier.
            """
            order = ["spine", "leaf", "access", "tor", "host", "gpu"]
            label = {"spine": "spine", "leaf": "leaf", "access": "ToR access switch",
                     "tor": "ToR switch", "gpu": "GPU server"}
            byk = {}
            for _n in TOPOLOGY_NODES:
                byk.setdefault(_n.get("kind", "other"), []).append(_n["id"])
            parts = []
            for k in order + sorted(set(byk) - set(order)):
                if k in ("host", "gpu") or k not in byk:
                    continue
                ids = byk[k]
                parts.append("%d %s%s (%s)" % (len(ids), label.get(k, k),
                                               "" if len(ids) == 1 else "s", ", ".join(ids)))
            if not parts:
                return "an empty fabric"
            return ("a %d-tier fabric: " % len(parts)) + "; ".join(parts) + \
                   ", wired as eBGP ECMP between the spine and leaf tiers"

        _n_gpu = sum(1 for n in TOPOLOGY_NODES if n.get("kind") == "gpu")
        _gpu_note = ""
        if _n_gpu:
            _gpu_note = (
                " It also hosts an AI-training GPU pod: %d GPU server(s) with the leaves as "
                "rails, running RoCE collectives (ring all-reduce, all-to-all). GPU util, "
                "power, HBM, junction temperature and the liquid-cooling loop are MODELED from "
                "the collective plus a training-step clock; during training the GPU rack power "
                "SAWTOOTHS in ANTI-PHASE with the fabric — power peaks in the compute window, "
                "dips during the all-reduce comms burst while ECN/PFC spike. GPU scenarios: "
                "training, alltoall, straggler, sustained (thermal throttle), coolant (liquid "
                "loss). The point of this view: correlating PFC/ECN with power and thermals "
                "over gNMI, made vivid on a GPU fabric." % _n_gpu)
        SYSTEM_PROMPT = (
            "You are the Energy Lab assistant, embedded in a live network telemetry "
            "dashboard. Explain what is happening in plain, technically-accurate terms "
            "for engineers. Tie heat to traffic (more load -> hotter). Heat/power are "
            "MODELED from real traffic on a virtual switch (no physical sensors) — say "
            "so honestly if asked. ASIC thresholds: warn 62C, critical 72C. Be concise "
            "(2-5 sentences) unless asked for detail. "
            "Format answers in clean markdown so they render well: short paragraphs, "
            "**bold** for key values/terms, and bullet lists ('- ') when listing multiple "
            "items, steps, or options; use a brief '### ' heading only for longer multi-part "
            "answers. Do not wrap the whole reply in a code block. "
            # ── Grounding rules. Without these it answered a topology question by
            #    reciting spines and leaves and never mentioned the access tier,
            #    and answered "PFC frames in the last hour" with a confident 0 for
            #    a counter this fabric does not have. ──
            "\n\nRULES — these override fluency:\n"
            "1. The context is ONE INSTANTANEOUS SAMPLE. There is no history. If asked "
            "about the last hour, a trend, a peak, or 'earlier', say the copilot only "
            "sees the current sample and give the current value instead. Never invent a "
            "time series.\n"
            "2. A field set to null, or listed under 'absent', is NOT ZERO — it means "
            "this node cannot report it. Say 'not available on this node'. Only report a "
            "0 when the number really is 0.\n"
            "3. If something is not in the context at all, say the fabric does not "
            "expose it. Do not reason it out from what a switch usually has. The list "
            "under 'not_measured' is what this fabric genuinely cannot tell you.\n"
            "4. Refer to nodes by their exact id and say which tier they are in. Every "
            "switch in the context carries a 'kind'; use it.\n"
            "5. Heat and power on the virtual nodes are MODELED from real measured "
            "traffic, not read from sensors. Say so whenever you quote one.\n"
            "6. When asked where traffic is coming from, read 'activity' and answer from "
            "it. Do not speculate, and never say 'likely' or 'probably' about something "
            "the context states outright. Host links reading 0 while the fabric is busy "
            "is the NORMAL state here, and 'activity' explains exactly why.\n"
            "7. The user is looking at a live map. Arrows and highlighted routes on it "
            "can be configuration rather than live traffic — check the matching "
            "'running' flag in 'activity' before saying anything is or is not flowing.\n"
            "8. Answer about THIS fabric, not about data centres in general. Before you "
            "repeat a general networking principle, CHECK IT against the numbers in the "
            "context — port_count, kind, the link list, how many hosts hang off a node. "
            "If the numbers do not support the principle here, lead with what is "
            "actually true of this fabric and only then say what the principle would be "
            "at production scale. A design question is not permission to stop reading "
            "the data. Example of the mistake to avoid: saying an access switch is there "
            "for port density without noticing that in this fabric it has two ports and "
            "one host, so density is plainly not the reason.\n\n"
            "The fabric you are watching is '%s'. It is %s. Hosts: %s.%s" % (
                FABRIC_NAME, _tier_sentence(),
                ", ".join(n["id"] for n in TOPOLOGY_NODES if n.get("kind") == "host") or "none",
                _gpu_note))
        OVERLOAD_CFG = FABRIC.get("overload") or {}
        COLLECTIVE_CFG = FABRIC.get("collective") or {}
        FLOOD = _flood_cmds()
        CONFIGURE_SH = _configure_sh_path()

    if not reset_state:
        return
    # a rebuild changed the fabric under us — the old node names may be gone, so
    # drop all scenario + agent runtime state and note it in the agent log.
    try:
        with _scen_lock:
            _scen_state.clear()
            _active.clear()
    except Exception:
        pass
    try:
        with _agent_lock:
            _agent["drained"].clear()
            _agent["cool_since"].clear()
    except Exception:
        pass
    try:
        _overload["running"] = False
        _overload["checked"] = 0.0
    except Exception:
        pass
    try:
        _agent_log("act", "fabric rebuilt to %dx%d — state reset" % (len(SPINES), len(LEAVES)))
    except Exception:
        pass


def maybe_reload():
    """Cheap stat of fabric.json; re-derive everything if its mtime changed.
    Guarded so a transient half-written / broken file NEVER crashes the app —
    we keep the last-good fabric and retry on the next poll."""
    try:
        m = os.path.getmtime(FABRIC_PATH)
    except OSError:
        return False
    if m == FAB_MTIME:
        return False
    try:
        reload_fabric(reset_state=True)
        return True
    except SystemExit:
        # load_fabric() sys.exit()s on a missing/broken json (mid-write) — swallow
        # it, keep the last good fabric, retry next tick.
        return False
    except Exception as e:
        print("maybe_reload: keeping last-good fabric (%s)" % e, file=sys.stderr)
        return False


def _exec_prefix():
    """Command prefix to 'docker exec' into a fabric container."""
    if EXEC.get("mode") == "limactl":
        vm = EXEC.get("vm", "sonic")
        return ["limactl", "shell", vm, "--", "sudo", "docker", "exec"]
    return ["docker", "exec"]


def _docker_base():
    """Command prefix for a raw 'docker' subcommand (stop/start/kill), same
    docker daemon _exec_prefix() reaches — used to fail/recover a whole switch."""
    if EXEC.get("mode") == "limactl":
        vm = EXEC.get("vm", "sonic")
        return ["limactl", "shell", vm, "--", "sudo", "docker"]
    return ["docker"]


def _mgmt_ip(node):
    """OOB management IP for a fabric node from fabric.json (ssh mode). None if unknown."""
    for n in TOPOLOGY_NODES:
        if n.get("id") == node:
            return n.get("mgmt_ip")
    return None


def _ssh_argv(ip, remote):
    """argv that runs a single remote shell string on a switch over SSH (admin@ip).
    The caller owns sudo/quoting inside `remote` — this only wraps the transport."""
    pw = os.environ.get("SW_PW", "YourPaSsWoRd")
    return ["sshpass", "-p", pw, "ssh", "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8",
            "-o", "LogLevel=ERROR", "admin@" + ip, remote]


def _node_ssh(node, remote):
    """_ssh_argv to <node>'s mgmt IP; ['false'] (clean rc!=0) if the node has none."""
    ip = _mgmt_ip(node)
    return _ssh_argv(ip, remote) if ip else ["false"]


# ssh mode (fabric.json exec.mode == "ssh") drives switches over SSH instead of
# `docker exec`: the path for real switches, described by a hand-written fabric.json.
# fabgen.py only emits docker/limactl. Several code paths share it.
def _cnode(node, *args):
    """Command line to run <args> on <node>.
    ssh mode (real switches): one sudo'd remote string over SSH to the switch's
    mgmt IP, so vtysh multi-'-c', config and tc all run in a single session.
    otherwise: 'docker exec' into the node's container (containerlab / lima)."""
    if EXEC.get("mode") == "ssh":
        ip = _mgmt_ip(node)
        if not ip:
            return ["false"]        # unknown node → command fails cleanly (rc 1)
        remote = "sudo " + " ".join(shlex.quote(a) for a in args)
        return _ssh_argv(ip, remote)
    return _exec_prefix() + [CONTAINER_PREFIX + node] + list(args)


def _leaf_of_host(host):
    """The leaf a host attaches to — directly, or via an access switch. None if unknown."""
    if not host:
        return None
    def nbrs(x):
        out = []
        for L in TOPOLOGY_LINKS:
            if L.get("a") == x:
                out.append(L.get("b"))
            elif L.get("b") == x:
                out.append(L.get("a"))
        return out
    for nb in nbrs(host):
        if KIND.get(nb) == "leaf":
            return nb
    for nb in nbrs(host):                       # host → access → leaf
        if KIND.get(nb) == "access":
            for nb2 in nbrs(nb):
                if KIND.get(nb2) == "leaf":
                    return nb2
    return None


def _spine_fabric_ports(spine):
    """Spine-side ports facing leaves (the links that carry the fabric underlay)."""
    ports = []
    for L in TOPOLOGY_LINKS:
        a, b = L.get("a"), L.get("b")
        if a == spine and KIND.get(b) == "leaf":
            ports.append(L.get("ap"))
        elif b == spine and KIND.get(a) == "leaf":
            ports.append(L.get("bp"))
    return [p for p in ports if p]


def _run(cmd, timeout=20):
    """Run a subprocess, NEVER raise. Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or ""), (r.stderr or "")
    except Exception as e:  # timeout, missing binary, etc.
        return 255, "", str(e)


# ─── Prometheus access ─────────────────────────────────────────────────────────
def _prom_query(expr):
    """Run one instant query. Returns list of {'metric':{...},'value':[ts,'v']}."""
    # PROM_URL="" means no Prometheus (run.sh preview). Fail at once: on Windows a refused
    # connection takes 2-4 s, which made every map refresh in preview take ~24 s.
    if not PROM_URL:
        raise RuntimeError("no Prometheus configured (preview mode)")
    url = PROM_URL.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(expr)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("status") != "success":
        raise RuntimeError("prom status " + str(data.get("status")))
    return data.get("data", {}).get("result", [])


_PROM_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="prom")


def _prom_batch(queries):
    """Run several instant queries CONCURRENTLY (independent localhost round-trips),
    returning {query: result_vector}. A failed query maps to [] so callers degrade
    gracefully. Collapses build_context's sequential Prom reads into ~1 round-trip."""
    def _one(q):
        try:
            return _prom_query(q)
        except Exception:
            return []
    futs = {q: _PROM_POOL.submit(_one, q) for q in queries}
    return {q: f.result() for q, f in futs.items()}


def _as_map(results, key_label):
    """Fold a prometheus result vector into {label_value: float}."""
    out = {}
    for row in results:
        lbl = row.get("metric", {}).get(key_label)
        if lbl is None:
            continue
        try:
            out[lbl] = float(row.get("value", [None, "nan"])[1])
        except (TypeError, ValueError):
            continue
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  POWER-EFFICIENCY ANOMALY DETECTION (P4 roadmap step, real).
#  Per switch: efficiency = watts / max(load_gbps, eps) = watts-per-Gbps. We keep
#  an EWMA baseline (mean + EW variance) per switch and flag a HIGH-side anomaly
#  when the live value deviates > K·σ above baseline (or, when σ is ~0, above a
#  fixed multiple of baseline). Two REAL shapes it catches:
#    * a spine that's been DRAINED — idle but still drawing its power floor, so its
#      watts-per-Gbps rockets vs when it carried load;
#    * a leaf under the congestion storm — RED-ecn throttles its useful tx while
#      modeled power climbs with intensity, so watts-per-useful-Gbps spikes.
#  The suggested fix ties to the EXISTING real 'drain' scenario (graceful-shutdown
#  a spine to rebalance ECMP) — never an invented action.
# ═══════════════════════════════════════════════════════════════════════════════
ANOM_ALPHA = float(os.environ.get("ANOM_ALPHA", "0.2"))            # EWMA smoothing
ANOM_K = float(os.environ.get("ANOM_K", "3.0"))                   # σ band width
ANOM_WARMUP = int(os.environ.get("ANOM_WARMUP", "8"))            # samples before flagging
ANOM_MIN_RATIO = float(os.environ.get("ANOM_MIN_RATIO", "1.6"))   # fixed-band fallback (σ≈0)
ANOM_EPS_GBPS = float(os.environ.get("ANOM_EPS_GBPS", "1e-4"))    # load floor, Gbps (no div/0)
# cadence of the server-side snapshot sampler (see "ONE AUTHORITATIVE SNAPSHOT")
SNAPSHOT_PERIOD = float(os.environ.get("SNAPSHOT_PERIOD", "2.0"))
# hard ceiling on ONE liveness fan-out (overload / ambient). Without it an
# unreachable fabric blocks a poll for len(sources) x 15 s — up to two minutes.
PROBE_BUDGET_S = float(os.environ.get("PROBE_BUDGET_S", "16.0"))
_anom = {"state": {}, "last": {}, "ts": 0.0, "skipped": {}}
_anom_lock = threading.Lock()


def _efficiency_wpg(s):
    """(watts-per-Gbps, load_gbps) for one build_context switch dict, or (None, None)
    when either input is ABSENT — an absent watt-reading must never be folded into
    a baseline as a zero."""
    tx, pw = s.get("tx_bps"), s.get("power_w")
    if tx is None or pw is None:
        return None, None
    load_gbps = max(float(tx) / 1e9, ANOM_EPS_GBPS)
    return float(pw) / load_gbps, load_gbps


def _anom_action():
    """The recommended fix — the EXISTING graceful-drain scenario, not an invented one."""
    return {"scenario": "drain", "label": "Graceful-drain a spine to rebalance ECMP"}


def _anom_reason(name, ratio, s):
    """Plain-English one-liner for an active anomaly."""
    kind = KIND.get(name, "switch")
    load_mbps = float(s.get("tx_bps") or 0.0) / 1e6
    drained = (name in _agent_drained()
               or (_scen_state.get("drain") or {}).get("spine") == name)
    if kind == "spine" and (drained or load_mbps < 0.05):
        return ("%s is drawing ~%.0fW while carrying almost no traffic — its "
                "power-per-Gbps is %.1fx its own baseline. It's parked but still "
                "burning energy." % (name, float(s.get("power_w") or 0.0), ratio))
    return ("%s power-per-Gbps is %.1fx its baseline — congestion is inflating its "
            "energy cost (useful throughput is throttled while power stays high)."
            % (name, ratio))


def _update_anomalies(switches):
    """Fold the current sample into each switch's EWMA baseline and (re)classify.
    Called ONCE per /api/topology poll (steady ~3s cadence) so sampling is even.
    Returns {switch: anomaly|None}. While a switch is flagged the baseline update
    is slowed 10x so a live anomaly stays visible instead of being absorbed."""
    out = {}
    now = time.time()
    with _anom_lock:
        for name, s in switches.items():
            wpg, load_gbps = _efficiency_wpg(s)
            if wpg is None:
                # ABSENT power or throughput — do NOT fold a zero into the EWMA
                # baseline and do not classify. Record it so the UI can say why.
                out[name] = None
                _anom["skipped"][name] = sorted(s.get("absent") or ["power_w"])
                continue
            _anom["skipped"].pop(name, None)
            st = _anom["state"].get(name)
            if st is None:                       # first sight of this switch — seed baseline
                _anom["state"][name] = {"mean": wpg, "var": 0.0, "n": 1}
                out[name] = None
                continue
            mean, var, n = st["mean"], st["var"], st["n"]
            std = math.sqrt(max(var, 0.0))
            anomaly = None
            # classify against the ESTABLISHED baseline, before folding this sample in
            if n >= ANOM_WARMUP and wpg > mean and mean > 0:
                ratio = wpg / mean
                over_sigma = std > 0 and wpg > mean + ANOM_K * std
                over_band = std <= 1e-9 and ratio >= ANOM_MIN_RATIO
                if over_sigma or over_band:
                    crit = ratio >= 2.5 or (std > 0 and wpg > mean + 2 * ANOM_K * std)
                    anomaly = {
                        "switch": name, "kind": KIND.get(name, ""),
                        "level": "crit" if crit else "warn",
                        "ratio": round(ratio, 2),
                        "wpg": round(wpg, 1), "baseline_wpg": round(mean, 1),
                        "load_gbps": round(load_gbps, 6),
                        "reason": _anom_reason(name, ratio, s),
                        "action": _anom_action(),
                    }
            a = ANOM_ALPHA * (0.1 if anomaly else 1.0)   # freeze-slow while flagged
            d = wpg - mean
            st["mean"] = mean + a * d
            st["var"] = (1 - a) * (var + a * d * d)
            st["n"] = n + 1
            out[name] = anomaly
        _anom["last"] = {k: v for k, v in out.items() if v}   # active-only snapshot
        _anom["ts"] = now
    return out


def _anomalies_for_nodes(switches):
    """{switch: anomaly|None} for rendering. The baseline update lives on the
    server-side sampler thread (so closing the browser tab no longer stops the
    baselines learning); if that thread is missing or has stalled we fold the
    sample in here instead, so the detector is never silently dead."""
    now = time.time()
    with _anom_lock:
        ts = _anom["ts"]
        last = dict(_anom["last"])
    if now - ts > max(6.0, SNAPSHOT_PERIOD * 3):
        return _update_anomalies(switches)
    return last


def _anom_active_list():
    """Read-only snapshot of active anomalies, most severe first. Safe from any
    request path — never mutates the EWMA baselines (only _update_anomalies does)."""
    with _anom_lock:
        items = list(_anom["last"].values())
    order = {"crit": 0, "warn": 1}
    items.sort(key=lambda a: (order.get(a.get("level"), 9), -a.get("ratio", 0)))
    return items


def anomalies_payload():
    """GET /api/anomalies — the active power-efficiency anomalies + a plain summary."""
    active = _anom_active_list()
    if active:
        summary = "%d power-efficiency %s active: %s" % (
            len(active), "anomaly" if len(active) == 1 else "anomalies",
            "; ".join("%s %.1fx" % (a["switch"], a["ratio"]) for a in active))
    else:
        summary = "no power-efficiency anomalies — every switch within its watts-per-Gbps baseline"
    return {"anomalies": active, "count": len(active), "summary": summary,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def build_context():
    """Assemble the compact live picture. Never raises: returns zeros + note on error."""
    switches = {}
    note = None
    try:
        # Run all instant queries CONCURRENTLY (was 9 sequential Prom round-trips ≈0.4s).
        _Q_ASIC, _Q_PWR = 'sonic_temperature_celsius{sensor="ASIC"}', "sum by(instance)(sonic_psu_output_power_watts)"
        _Q_TX, _Q_DROP = "sum by(switch)(rate(fabric_port_tx_bytes[1m])*8)", "sum by(switch)(rate(fabric_port_tx_dropped[1m]))"
        _Q_BGP, _Q_VIAS, _Q_INTEN = "fabric_bgp_established", "fabric_fib_vias", "fabric_switch_intensity"
        _Q_ECN = "sum by(instance)(rate(sonic_ecn_marked_packets_total[1m]))"
        _Q_ECNMK = "fabric_ecn_marks_total"   # REAL kernel-tc RED CE marks (0 unless a storm armed it)
        # ── Phase A2 cheap per-node additions — ride the SAME concurrent fan-out
        #    (no extra round-trip). cpu_c/fan_rpm/psu_in_w are MODELED-from-load;
        #    load_bps is the honest measured aggregate (REAL); gnmi_live is REAL. ──
        _Q_CPU = 'sonic_temperature_celsius{sensor="CPU"}'
        _Q_FAN = "max by(instance)(sonic_fan_rpm)"
        _Q_PSUIN = "sum by(instance)(sonic_psu_input_power_watts)"
        _Q_LOAD = "fabric_switch_load_bps"
        _Q_GNMIUP = "fabric_gnmi_up"
        # SILENT-ZERO GUARD: the exporter publishes ABSENCE positively (a metric a
        # node cannot produce gets fabric_metric_absent=1 and NO value series), so
        # a missing sensor can never be read as a confident 0. Rides the same fan-out.
        _Q_ABSENT = "fabric_metric_absent"
        _R = _prom_batch([_Q_ASIC, _Q_PWR, _Q_TX, _Q_DROP, _Q_BGP, _Q_VIAS, _Q_INTEN,
                          _Q_ECN, _Q_ECNMK, _Q_CPU, _Q_FAN, _Q_PSUIN, _Q_LOAD, _Q_GNMIUP,
                          _Q_ABSENT])
        asic = _as_map(_R[_Q_ASIC], "instance")
        power = _as_map(_R[_Q_PWR], "instance")
        txbps = _as_map(_R[_Q_TX], "switch")
        drops = _as_map(_R[_Q_DROP], "switch")
        bgp = _as_map(_R[_Q_BGP], "switch")
        vias = _as_map(_R[_Q_VIAS], "switch")
        inten = _as_map(_R[_Q_INTEN], "switch")
        ecn = _as_map(_R[_Q_ECN], "instance")
        ecnmk = _as_map(_R[_Q_ECNMK], "switch")
        cpu_c = _as_map(_R[_Q_CPU], "instance")
        fan_rpm = _as_map(_R[_Q_FAN], "instance")
        psu_in = _as_map(_R[_Q_PSUIN], "instance")
        load = _as_map(_R[_Q_LOAD], "switch")
        gnmiup = _as_map(_R[_Q_GNMIUP], "switch")
        reported_absent = {}                     # {switch: {metric: True}}
        for _row in _R.get(_Q_ABSENT, []):
            _m = _row.get("metric", {})
            _sw, _met = _m.get("switch"), _m.get("metric")
            try:
                _on = float(_row.get("value", [None, "0"])[1]) >= 0.5
            except (TypeError, ValueError):
                _on = False
            if _sw and _met and _on:
                reported_absent.setdefault(_sw, {})[_met] = True

        names = set()
        for d in (asic, power, txbps, drops, bgp, vias, inten, load):
            names.update(d.keys())
        names = {n for n in names if ":" not in n}  # drop scrape-target artifacts (e.g. 'localhost:9200')
        for n in sorted(names):
            # ── ABSENT vs ZERO ────────────────────────────────────────────────
            # A field is ABSENT when (a) this node's platform manifest says it
            # cannot produce it, or (b) the exporter published absence for it.
            # ABSENT fields carry None (never 0.0) plus a machine-readable signal
            # and a human reason, so the UI badges ABSENT and the agents SKIP.
            # For every emulated node both sources are empty, so this block is a
            # no-op and the payload is identical to before.
            gone = dict(declared_absent(n))
            for _met in reported_absent.get(n, {}):
                gone.setdefault(_met, "reported absent by the collector on this node")

            def _v(metric, m, dflt, nd=None):
                """Value for `metric` out of prom map `m`: None when ABSENT."""
                if metric in gone:
                    return None
                val = m.get(n, dflt)
                return round(val, nd) if nd is not None else val

            rec = {
                "asic_c": _v("asic_c", asic, 0.0, 1),
                "power_w": _v("power_w", power, 0.0, 1),
                "tx_bps": _v("tx_bps", txbps, 0.0, 1),
                "drops_ps": _v("drops_ps", drops, 0.0, 3),
                "bgp_peers": int(round(bgp.get(n, 0.0))),
                "fib_vias": int(round(vias.get(n, 0.0))),
                "intensity": _v("intensity", inten, 0.0, 3),
                "ecn_ps": _v("ecn_ps", ecn, 0.0, 1),
                # REAL tc RED CE marks
                "ecn_marks": None if "ecn_marks" in gone else int(round(ecnmk.get(n, 0.0))),
                # ── cheap per-node additions (Phase A2) ──
                "cpu_c": _v("cpu_c", cpu_c, 0.0, 1),           # MODELED (from load)
                "fan_rpm": None if "fan_rpm" in gone else int(round(fan_rpm.get(n, 0.0))),
                "psu_in_w": _v("psu_in_w", psu_in, 0.0, 1),    # MODELED (from load)
                "load_bps": _v("load_bps", load, 0.0, 1),      # REAL measured aggregate
                "gnmi_live": gnmiup.get(n, 0.0) >= 0.5,        # REAL gNMI (per node)
            }
            if gone:
                rec["absent"] = sorted(gone)
                rec["absent_why"] = gone
                rec["signal"] = {k: "absent" for k in sorted(gone)}
            # Which tier this node is in. Without it the model had to guess from the
            # name, and guessed wrong — it read SWITCH-1..4 as duplicates of the
            # leaves instead of the access tier they actually are.
            rec["kind"] = KIND.get(n) or "unknown"
            _nd = next((x for x in TOPOLOGY_NODES if x.get("id") == n), {})
            if _nd.get("role"):
                rec["role"] = _nd["role"]
            if _nd.get("mgmt_ip"):
                rec["mgmt_ip"] = _nd["mgmt_ip"]
            if _nd.get("asn"):
                rec["asn"] = _nd["asn"]
            # Port list, so the model can check a general claim against this box.
            # Without it, asked why the access tier exists, it recited the textbook
            # "access switches give you port density" — while every access switch
            # here has exactly two ports and serves exactly one host.
            if _nd.get("ports"):
                rec["ports"] = _nd["ports"]
                rec["port_count"] = len(_nd["ports"])
            # fabric_bgp_established counts ESTABLISHED sessions, not configured
            # neighbours. The old key name read as the latter.
            rec["bgp_established"] = rec["bgp_peers"]
            b = node_binding(n)
            if b.get("physical"):
                rec["physical"] = True
                rec["platform"] = b.get("platform")
                rec["nos"] = b.get("nos")
                rec["counter_source"] = b.get("counter_source")
            switches[n] = rec
        if not switches:
            note = "waiting for data — no metrics from Prometheus yet"
    except Exception as e:  # unreachable Prom, timeout, bad JSON — degrade gracefully
        note = "waiting for data — Prometheus unreachable (%s)" % (type(e).__name__)

    # ── REAL gNMI series — the ONE genuinely gNMI-sourced signal (everything else
    # here is SSH/kernel or modeled). fabric_gnmi_out_octets{switch,port} is read (polled)
    # over gNMI Get by the exporter from GNMI_LEAF's :8080 telemetry server; fabric_gnmi_up is
    # 1 while that read is succeeding. Isolated in its own try so a gNMI hiccup can
    # NEVER blank the rest of the fabric picture.
    gnmi = None
    try:
        _RG = _prom_batch(["fabric_gnmi_out_octets", "fabric_gnmi_up"])
        goct = _as_pair_map(_RG["fabric_gnmi_out_octets"])   # (switch,port)->octets
        gup = _as_map(_RG["fabric_gnmi_up"], "switch")
        per_sw = {}
        for (sw, p), v in goct.items():
            per_sw.setdefault(sw, {})[p] = v
        if per_sw:
            gsw = sorted(per_sw)[0]
            ports = per_sw[gsw]
            total = int(sum(ports.values()))
            live = gup.get(gsw, 0.0) >= 0.5
            gnmi = {"switch": gsw, "port": sorted(ports)[0],
                    "ports": {p: int(v) for p, v in ports.items()},
                    "out_octets": total, "live": live}
            if gsw in switches:                 # let the node tile inherit the badge
                switches[gsw]["gnmi_out_octets"] = total
                switches[gsw]["gnmi_live"] = live
    except Exception:
        gnmi = None

    # OFFERED LOAD = the real work the fabric is asked to do = sum over leaves of
    # host-edge INGRESS (rate of bytes RECEIVED on host-facing ports). This is
    # ONE-DIRECTIONAL and does NOT double-count per hop, so it is the honest
    # human-facing "fabric throughput" — unlike sum(per-switch tx) which counts
    # every packet once per switch it traverses (≈2× inflated on a leaf-spine).
    offered = 0.0
    try:
        hostrx = _as_pair_map(_prom_query("rate(fabric_port_rx_bytes[1m])*8"))
        for lf in LEAVES:
            for p in HOST_PORTS.get(lf, []):
                offered += hostrx.get((lf, p), 0.0)
    except Exception:
        offered = 0.0

    # Aggregates NEVER fold an ABSENT reading in as a zero — a node with no ASIC
    # sensor must not be able to win (or lose) "hottest", and must not silently
    # drag the fabric total down.
    hottest = {"name": None, "asic_c": 0.0}
    total_tx = 0.0
    notable = []
    blind = []
    for n, s in switches.items():
        txv = s.get("tx_bps")
        if txv is not None:
            total_tx += txv
        ac = s.get("asic_c")
        if ac is None:
            blind.append(n)
        else:
            if hottest["name"] is None or ac > hottest["asic_c"]:
                hottest = {"name": n, "asic_c": ac}
            if ac >= ASIC_CRIT:
                notable.append("%s ASIC %.0fC (CRITICAL >%.0f)" % (n, ac, ASIC_CRIT))
            elif ac >= ASIC_WARN:
                notable.append("%s ASIC %.0fC (warn >%.0f)" % (n, ac, ASIC_WARN))
        dp = s.get("drops_ps")
        if dp is not None and dp > 0.5:
            notable.append("%s dropping ~%.1f pkt/s (tx)" % (n, dp))
    for n in blind:
        why = (switches[n].get("absent_why") or {}).get("asic_c", "no sensor")
        notable.append("%s ASIC temperature ABSENT — %s" % (n, why))

    ctx = {
        "fabric": {"name": FABRIC_NAME, "params": PARAMS},
        "switches": switches,
        # nodes whose ASIC temperature is ABSENT (not zero) — the thermal agent's
        # required input. Empty on every emulated fabric.
        "blind_thermal": sorted(blind),
        "hottest": hottest,
        "total_tx_bps": round(total_tx, 1),
        "offered_bps": round(offered, 1),
        "notable": notable,
        "units": {
            "asic_c": "degrees Celsius (warn 62, critical 72)",
            "power_w": "watts",
            "tx_bps": "bits per second, RAW per-switch tx (e.g. 210 = 210 bit/s, NOT Mbps; 4000000 = 4 Mbps)",
            "offered_bps": "bits per second, ONE-DIRECTIONAL offered load = host-edge ingress summed over leaves; the honest fabric throughput",
            "drops_ps": "packets per second",
            "intensity": "0..1 load index",
            "bgp_established": "count of BGP sessions in the Established state",
        },
    }

    # ── What this fabric CANNOT tell you. ───────────────────────────────────────
    # Stating absence positively is the only thing that stops a fluent model from
    # answering "PFC pause frames in the last hour?" with a confident 0. These are
    # real gaps in the virtual fabric, not bugs.
    ctx["not_measured"] = [
        "PFC / pause frames — the virtual switches expose no PFC counters. Only the "
        "two physical Z9100-ON switches on the bench do.",
        "per-queue depth, watermarks and WRED thresholds",
        "any history — this payload is one instantaneous sample, so no trends, peaks, "
        "averages or 'last hour' answers are possible from it",
        "real power and temperature sensors on the virtual nodes: asic_c, cpu_c, "
        "power_w, psu_in_w and fan_rpm are MODELED from measured traffic",
    ]
    ctx["sampled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # Hosts and what they hang off. Static wiring, safe to read here; the LIVE
    # per-link rates are added by with_wiring() at the chat boundary, because
    # they come from build_topology() and that calls back into this function.
    try:
        ctx["hosts"] = {n["id"]: {"ip": n.get("ip"), "attached_to": next(
            (l["b"] if l["a"] == n["id"] else l["a"]) for l in TOPOLOGY_LINKS
            if n["id"] in (l["a"], l["b"]))}
            for n in TOPOLOGY_NODES if n.get("kind") == "host"}
    except Exception:
        pass

    if gnmi:
        ctx["gnmi"] = gnmi
        ctx["units"]["gnmi_out_octets"] = (
            "cumulative bytes transmitted, read live over gNMI/OpenConfig from %s's "
            ":8080 telemetry server" % gnmi["switch"])
    # power-efficiency anomalies (read-only snapshot; the EWMA baseline is updated
    # only on the /api/topology poll, never here) — so the copilot brain is AWARE.
    active = _anom_active_list()
    if active:
        ctx["anomalies"] = active
    if BRAIN == "openai":
        ctx["brain"] = "LLM · " + os.environ.get("OPENAI_MODEL", "?").split("/")[-1].replace(":free", "")
    elif BRAIN == "azure-anthropic":
        ctx["brain"] = "LLM · " + os.environ.get("AZURE_ANTHROPIC_DEPLOYMENT", "claude")
    else:
        ctx["brain"] = "offline (rules)"
    if note:
        ctx["note"] = note
    return ctx


# ─── overload control (demo flood, target from fabric.json) ───────────────────
# demo flood = src_host pings dst_ip fast: 1400-byte packet every 10ms ≈ 100 pkt/s ≈ 1.1 Mbps
# OVERLOAD_CFG is (re)set by reload_fabric().


# Virtual-lab (docker/limactl) flood signatures. pkill/pgrep -f run through
# docker exec with no shell around them, so they cannot match their own command
# line and need no bracket.
VIRTUAL_SURGE_SIG = "ping -i 0.01 -s 1400"       # RESERVED for the virtual surge
VIRTUAL_ELEPHANT_SIG = "ping -i 0.01 -s 1200"    # RESERVED for the virtual elephant


def _flood_cmds():
    """One stream dict per source host flooding OVERLOAD_CFG's dst_ip — every host in
    the fabric except the destination itself, so the always-on demo route is an N-to-1
    incast (not a single host-to-host ping). Sources are derived fresh from HOSTS/HOST_IP
    each call (set by reload_fabric()), never hardcoded, so this self-adjusts if the
    fabric is regenerated with a different host count."""
    dst = OVERLOAD_CFG.get("dst_ip")
    if not dst:
        return []
    sources = [h for h in HOSTS if HOST_IP.get(h) != dst]
    if not sources:
        return []
    dst_host = next((n["id"] for n in TOPOLOGY_NODES if n.get("ip") == dst), None)
    dst_leaf = _leaf_of_host(dst_host)
    if not dst_leaf:
        return []
    # ssh mode (real switches; see the note above _cnode()): there are no clab-* containers. Flood on the REAL switches:
    # from each source host's LEAF, ping the destination host's LEAF loopback, so the traffic
    # traverses leaf -> spine -> leaf — the incast path to the overload destination host lights up.
    if EXEC.get("mode") == "ssh":
        dst_lo = next((n.get("loopback") for n in TOPOLOGY_NODES if n.get("id") == dst_leaf), None)
        if not dst_lo:
            return []
        pat = "ping -i 0.003 -s 1400 %s" % dst_lo    # unique signature for pgrep/pkill
        streams = []
        for src in sources:
            src_leaf = _leaf_of_host(src)
            if not src_leaf or src_leaf == dst_leaf:  # same-leaf flood is pointless (no leaf->spine->leaf hop)
                continue
            streams.append({
                "src": src, "dst": dst_lo,
                "start": _node_ssh(src_leaf, "sudo nohup %s >/dev/null 2>&1 </dev/null & echo ok" % pat),
                # bracketed so the pattern cannot match this command's own line
                "stop":  _node_ssh(src_leaf, 'sudo pkill -f "%s"' % _brk(pat)),
                "check": _node_ssh(src_leaf, 'pgrep -f "%s"' % _brk(pat)),
            })
        return streams
    # Virtual lab: the same idea as ssh mode, one signature per scenario, so the
    # surge's check and stop match only the surge's own pings (the elephant uses
    # -s 1200, ambient -i 0.05). A bare `pgrep ping` counted any ping as a surge,
    # and `pkill ping` stopped the elephant flows too.
    sig = VIRTUAL_SURGE_SIG + " " + dst
    ex = _exec_prefix()
    streams = []
    for src in sources:
        cont = CONTAINER_PREFIX + src
        streams.append({
            "src": src, "dst": dst,
            "start": ex + ["-d", cont] + sig.split(),
            "stop": ex + [cont, "pkill", "-f", sig],
            "check": ex + [cont, "pgrep", "-f", sig],
        })
    return streams


# FLOOD is (re)set by reload_fabric() from OVERLOAD_CFG.
_overload = {"running": False, "checked": 0.0}


def _overload_running(force=False):
    """Is ANY flood stream alive in any source host? Cached ~10s so polls stay cheap."""
    if not FLOOD:
        return False
    now = time.time()
    if force or now - _overload["checked"] > 10:
        try:
            alive = False
            deadline = now + PROBE_BUDGET_S      # hard cap for the WHOLE fan-out
            for s in FLOOD:
                left = deadline - time.time()
                if left <= 0:                     # budget spent — keep last-known
                    break
                r = subprocess.run(s["check"], capture_output=True, text=True,
                                   timeout=min(15, max(1.0, left)))
                if r.returncode == 0:
                    alive = True
                    break
            _overload["running"] = alive
            _overload["checked"] = now
        except Exception:
            pass  # keep last known state
    return _overload["running"]


def overload_control(action):
    """Start/stop every incast flood stream. Returns a dict the UI can show verbatim."""
    if not FLOOD:
        return {"ok": False, "running": False,
                "msg": "no overload demo configured (fabric.json has no overload block)"}
    try:
        if action == "start":
            if _overload_running(force=True):
                return {"ok": True, "running": True, "msg": "overload already running"}
            for s in FLOOD:
                subprocess.run(s["start"], capture_output=True, timeout=20)
            _overload["running"], _overload["checked"] = True, time.time()
            srcs = ", ".join(s["src"] for s in FLOOD)
            return {"ok": True, "running": True,
                    "msg": "flood started: %d source(s) [%s] → %s, ~100 pkt/s each (watch where the congestion reaches)"
                           % (len(FLOOD), srcs, FLOOD[0]["dst"])}
        if action == "stop":
            for s in FLOOD:
                subprocess.run(s["stop"], capture_output=True, timeout=20)  # rc=1 if none; fine
            _overload["running"], _overload["checked"] = False, time.time()
            return {"ok": True, "running": False, "msg": "stopped — the fabric drains back to its baseline"}
        return {"ok": False, "running": _overload_running(), "msg": "unknown action"}
    except Exception as e:
        return {"ok": False, "running": _overload_running(), "msg": "error: %s" % e}


# ═══════════════════════════════════════════════════════════════════════════════
#  AMBIENT BASELINE — always-on low-rate REAL pings so the map hums and no metric
#  reads dead-zero. Modeled exactly on the SSH flood plumbing (honest: real pings on
#  real switches). RESERVED SIGNATURE: "ping -i 0.05". Every scenario flood uses
#  -i 0.003 (surge -s1400, elephant -s1200, storm/incast -s1250); ambient's -i 0.05
#  is unique on the interval axis, so pkill -f "ping -i 0.05" targets ONLY ambient
#  and no scenario's pkill (all -i 0.003 / -s NNNN) can ever kill it, and ambient's
#  own pkill can never kill a scenario. DO NOT reuse -i 0.05 for any other flood.
#  ~20 pkt/s * ~142 B on-wire ≈ 23 kbps one-directional — above FLOW_THRESHOLD_BPS
#  (surfaces as a gentle flow) but ~50x below a surge (reads as calm, not congestion).
# ═══════════════════════════════════════════════════════════════════════════════
AMBIENT_SIG = "ping -i 0.05"                 # RESERVED for ambient — never reuse
_ambient = {"running": False, "checked": 0.0}
_ambient_lazy = {"last_kick": 0.0}


def _ambient_cmds():
    """Per-leaf ambient ping streams. Pairs derive generically from LEAVES + each
    node's loopback: leaves[0]<->leaves[-1] and leaves[1]<->leaves[2] (non-adjacent
    so their paths cross and hash onto different spines). Each direction is its own
    ping process, one per source leaf (at most 4), so both leaf-spine
    segments of every pair light. Skips pairs whose loopbacks are missing."""
    with _fab_lock:
        leaves = list(LEAVES)
        nodes = {n.get("id"): n for n in TOPOLOGY_NODES}
    if len(leaves) < 2:
        return []
    pairs = [(leaves[0], leaves[-1])]
    if len(leaves) >= 3:
        pairs.append((leaves[1], leaves[2 if len(leaves) >= 4 else -1]))
    ssh = EXEC.get("mode") == "ssh"
    ex = _exec_prefix()
    streams, seen = [], set()
    for a, b in pairs:
        for src, dst in ((a, b), (b, a)):
            if src in seen:
                continue
            lo = (nodes.get(dst) or {}).get("loopback")
            if not lo:
                continue
            seen.add(src)
            pat = "ping -i 0.05 -s 100 %s" % lo
            if ssh:
                start = _node_ssh(src, "sudo nohup %s >/dev/null 2>&1 </dev/null & echo ok" % pat)
                # bracketed: an unbracketed pkill -f kills its own SSH shell first
                stop = _node_ssh(src, 'sudo pkill -f "%s"' % _brk(AMBIENT_SIG))
                check = _node_ssh(src, 'pgrep -f "%s"' % _brk(AMBIENT_SIG))
            else:
                cont = CONTAINER_PREFIX + src
                start = ex + ["-d", cont, "ping", "-i", "0.05", "-s", "100", lo]
                stop = ex + [cont, "pkill", "-f", AMBIENT_SIG]
                check = ex + [cont, "pgrep", "-f", AMBIENT_SIG]
            streams.append({"leaf": src, "dst_lo": lo,
                            "start": start, "stop": stop, "check": check})
    return streams


def _ambient_running(force=False):
    """Is the ambient hum alive on at least one leaf? Cached ~10s so polls stay cheap."""
    now = time.time()
    if force or now - _ambient["checked"] > 10:
        streams = _ambient_cmds()
        if not streams:
            _ambient["running"], _ambient["checked"] = False, now
            return False
        alive = False
        deadline = now + PROBE_BUDGET_S           # hard cap for the WHOLE fan-out
        for s in streams:
            left = deadline - time.time()
            if left <= 0:
                break
            try:
                r = subprocess.run(s["check"], capture_output=True, text=True,
                                   timeout=min(15, max(1.0, left)))
                if r.returncode == 0:
                    alive = True
                    break
            except Exception:
                pass
        _ambient["running"], _ambient["checked"] = alive, now
    return _ambient["running"]


def ambient_control(action):
    """Start/stop/check the always-on baseline hum. Mirrors overload_control:
    start is idempotent (check first) + best-effort per stream. Returns a UI dict."""
    streams = _ambient_cmds()
    if not streams:
        return {"ok": False, "running": False,
                "msg": "no ambient pairs (need ≥2 leaves with loopbacks)"}
    try:
        if action == "start":
            if _ambient_running(force=True):
                return {"ok": True, "running": True, "msg": "ambient baseline already humming"}
            started = 0
            for s in streams:
                try:
                    subprocess.run(s["start"], capture_output=True, timeout=20)
                    started += 1
                except Exception:
                    pass
            _ambient["running"], _ambient["checked"] = (started > 0), time.time()
            return {"ok": started > 0, "running": started > 0,
                    "msg": "ambient baseline started — %d real flows (~23 kbps each) humming across the fabric" % started}
        if action == "stop":
            for s in streams:
                try:
                    subprocess.run(s["stop"], capture_output=True, timeout=20)  # rc=1 if none; fine
                except Exception:
                    pass
            _ambient["running"], _ambient["checked"] = False, time.time()
            return {"ok": True, "running": False, "msg": "ambient baseline stopped — map goes to true idle"}
        if action == "check":
            return {"ok": True, "running": _ambient_running(force=True), "msg": "ambient state"}
        return {"ok": False, "running": _ambient_running(), "msg": "unknown action"}
    except Exception as e:
        return {"ok": False, "running": _ambient_running(), "msg": "error: %s" % e}


def _ambient_lazy_poll():
    """Called from build_topology on every /api/topology poll: report ambient state
    and, if it's not running, kick a throttled best-effort background start so a
    server that came up before the fabric (or a failed reset-restart) self-heals the
    baseline within a poll or two. Never blocks the poll on a slow SSH."""
    running = _ambient_running()
    if not running:
        now = time.time()
        if now - _ambient_lazy["last_kick"] > 30:
            _ambient_lazy["last_kick"] = now
            threading.Thread(target=lambda: ambient_control("start"),
                             name="ambient-rearm", daemon=True).start()
    return running


def _as_pair_map(results):
    """Fold a prometheus vector into {(switch, port): float}."""
    out = {}
    for row in results:
        m = row.get("metric", {})
        sw, p = m.get("switch"), m.get("port")
        if sw and p:
            try:
                out[(sw, p)] = float(row.get("value", [None, "nan"])[1])
            except (TypeError, ValueError):
                continue
    return out


def build_topology(ctx=None, probe=True):
    """The full live picture for the command-center UI: nodes with temps/power,
    links with live bps + port names, the detected flows, overload state.
    Everything topological comes from fabric.json — nothing is hardcoded.

    `ctx` lets the server-side snapshot sampler pass the SAME build_context()
    sample it used for every other section, so two panels rendered from one
    snapshot can never disagree about the same quantity.
    `probe=False` reads the CACHED overload/ambient liveness instead of running
    the SSH liveness fan-out — used by the sampler when no client is reading, so
    adding a server-side tick puts ZERO extra SSH on the fabric. Every existing
    caller keeps probe=True and therefore behaves exactly as before."""
    # cheap stat; if fabric.json changed on disk (a live rebuild) re-derive first
    maybe_reload()
    # snapshot the structural globals under the lock so a concurrent reload can
    # never hand us a half-updated fabric (nodes from S×L, links from S'×L').
    with _fab_lock:
        topo_nodes = TOPOLOGY_NODES
        topo_links = TOPOLOGY_LINKS
        kind = KIND
        leaves = LEAVES
        uplinks = UPLINKS
        host_ports = HOST_PORTS
        fab_name, fab_params = FABRIC_NAME, PARAMS
        fab_hw = FAB_HW
    try:
        tx = _as_pair_map(_prom_query("rate(fabric_port_tx_bytes[30s])*8"))
        rx = _as_pair_map(_prom_query("rate(fabric_port_rx_bytes[30s])*8"))
    except Exception:
        tx, rx = {}, {}
    if ctx is None:
        ctx = build_context()

    def prate(sw, port):  # both directions through one switch port (link utilisation)
        return tx.get((sw, port), 0.0) + rx.get((sw, port), 0.0)

    def orate(sw, port):  # ONE-DIRECTIONAL dominant rate — the honest per-flow number
        return max(tx.get((sw, port), 0.0), rx.get((sw, port), 0.0))

    # link-level scenario state read ONCE before the loop: a cable cut shuts a
    # specific (leaf,port); a spine failure downs every fabric port of one spine.
    cc = _scen_state.get("cablecut")            # {leaf, port, spine}
    cut_ports = {(cc["leaf"], cc["port"])} if cc else set()
    sf = _scen_state.get("spinefail")           # {spine, ports}
    down_spine = sf.get("spine") if sf else None

    links = []
    for L in topo_links:
        # measure on a switch side (hosts aren't in the exporter)
        if kind.get(L["b"]) in ("leaf", "spine"):
            side, port = L["b"], L["bp"]
        else:
            side, port = L["a"], L["ap"]
        bps = prate(side, port)
        # resolve the fabric link's leaf-side (leaf,port) and spine via the KIND map,
        # then tag it down if it's the cut port or belongs to a failed spine.
        ka, kb = kind.get(L["a"]), kind.get(L["b"])
        is_fabric = {ka, kb} == {"leaf", "spine"}
        down, reason = False, None
        if is_fabric:
            if ka == "leaf":
                leaf, lport, spine = L["a"], L["ap"], L["b"]
            else:
                leaf, lport, spine = L["b"], L["bp"], L["a"]
            if (leaf, lport) in cut_ports:
                down, reason = True, "cut"
            elif spine == down_spine:
                down, reason = True, "down"
        # NOTE: grayfail is intentionally NOT tagged down — the silent 2% netem loss
        # is invisible by design (that's the scenario's whole point), so a grayfail
        # link stays styled by traffic. Only hard cuts / spine failures show as down.
        # NOTE the "and not down": a cut link must never read as active even if a
        # stale rate lingers in the 30s Prometheus window.
        links.append({**L, "bps": round(bps, 1),
                      "active": (bps > FLOW_THRESHOLD_BPS) and not down,
                      "down": down, "down_reason": reason})

    # Which spine did ECMP pick — per leaf. Derive flows from busy UPLINKS, not host
    # ingress: every REAL demo flow (surge, elephant, storm, ambient) is a ping the
    # leaf CPU sources toward a peer leaf loopback, so it never ingresses a host port
    # — gating on host_ingress left flows[] empty and the map dead. The busiest uplink
    # names the spine ECMP hashed the flow onto; different 5-tuples hash to different
    # spines, so concurrent flows naturally split across both spines for free.
    flows = []
    for leaf in leaves:
        best_bps, best_spine = 0.0, None
        for port, spine in uplinks.get(leaf, []):
            r = orate(leaf, port)
            if r > best_bps:
                best_bps, best_spine = r, spine
        if best_spine and best_bps > FLOW_THRESHOLD_BPS:
            flows.append({"src_leaf": leaf, "via_spine": best_spine, "bps": round(best_bps, 1)})
    flows.sort(key=lambda f: -f["bps"])

    # legacy single-flow shape (kept for compatibility; UI prefers "flows")
    flow = {"active": False}
    if flows:
        f0 = flows[0]
        flow = {"active": True, "via": f0["via_spine"], "bps": f0["bps"],
                "path": [f0["src_leaf"], f0["via_spine"]]}

    # power-efficiency anomaly pass. The EWMA baseline is now owned by the
    # server-side sampler thread, so it keeps learning when the browser tab is
    # closed; this only folds a sample in itself if the sampler has gone quiet.
    anomalies = _anomalies_for_nodes(ctx.get("switches", {}))

    # GPU servers on the map carry a couple of live modeled fields for the glyph
    # (util bar + heat). Cheap: 2 instant queries, only when GPU is shown + present.
    gpu_util_m, gpu_junc_m = {}, {}
    if GPU_ON_MAP and any(n.get("kind") == "gpu" for n in topo_nodes):
        try:
            _gr = _prom_batch(["gpu_util_percent", "gpu_junction_celsius"])
            gpu_util_m = _as_map(_gr.get("gpu_util_percent", []), "server")
            gpu_junc_m = _as_map(_gr.get("gpu_junction_celsius", []), "server")
        except Exception:
            pass

    nodes = []
    for n in topo_nodes:
        if n.get("kind") == "gpu" and not GPU_ON_MAP:
            continue                       # GPU servers surface only via /api/gpu when hidden
        d = dict(n)
        live = ctx.get("switches", {}).get(n["id"])
        if live:
            d.update(live)
        if n.get("kind") == "gpu":
            d["gpu_util"] = round(gpu_util_m.get(n["id"], 0.0), 1)
            d["gpu_junction"] = round(gpu_junc_m.get(n["id"], 0.0), 1)
        an = anomalies.get(n["id"])
        if an:
            d["anomaly"] = an
        nodes.append(d)

    return {
        "fabric": {"name": fab_name, "params": fab_params},
        # REAL gNMI series (GNMI_LEAF's live out-octets, read from :8080)
        "gnmi": ctx.get("gnmi"),
        # active power-efficiency anomalies (most severe first), for the UI banner
        "anomalies": _anom_active_list(),
        # modeled hardware: block for the panel + each node keeps its "hw" id
        "hardware": {"leaf": fab_hw.get("leaf"), "spine": fab_hw.get("spine"),
                     "breakout": fab_hw.get("breakout", DEFAULT_BREAKOUT)},
        "nodes": nodes, "links": links, "flow": flow, "flows": flows,
        "overload": {"running": _overload_running() if probe else _overload["running"],
                     "configured": bool(FLOOD),
                     "src_host": FLOOD[0]["src"] if FLOOD else OVERLOAD_CFG.get("src_host"),
                     "src_hosts": [s["src"] for s in FLOOD],
                     "dst_ip": OVERLOAD_CFG.get("dst_ip")},
        # baseline hum indicator (real always-on low-rate pings); lazy self-heal below
        "ambient": {"running": _ambient_lazy_poll() if probe else _ambient["running"]},
        # traffic-studio state (Phase A2) — cheap in-memory truth, no SSH on the poll
        "traffic": _traffic_summary(),
        # GPU collective state (training/all-to-all/straggler/sustained), no SSH on the poll
        "collective": collective_summary(),
        "hottest": ctx.get("hottest"), "total_tx_bps": ctx.get("total_tx_bps"),
        "offered_bps": ctx.get("offered_bps"),
        "brain": ctx.get("brain"), "notable": ctx.get("notable", []),
        "note": ctx.get("note"),
        # ── state the single 3s UI poll needs to reflect scenarios + the agent ──
        "agent": {"enabled": _agent["enabled"], "drained": sorted(_agent_drained()),
                  "health": _agent_health()},
        "scenarios": {"active": _active_list(probe=probe)},
        # GPU pod draw as ONE number, so the correlation strip can plot the
        # training sawtooth on the SAME sample as everything else. Reading it
        # from /api/gpu instead would put it on a different clock, and a strip
        # whose whole point is a shared time axis cannot have two clocks.
        "gpu_kw": _gpu_kw(),
    }


def _gpu_kw():
    """Total GPU pod draw in kW, or None when there is no pod.

    Cheap by construction: build_gpu() reads the same cached Prometheus data the
    rest of the poll uses. Wrapped because a GPU-side failure must never take
    the topology payload down with it.
    """
    try:
        g = build_gpu()
        pod = g.get("pod") if isinstance(g, dict) else None
        return pod.get("total_kw") if pod else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  EVPN / VXLAN OVERLAY — REAL read over SSH from each leaf, for real switches that
#  run an overlay. Every leaf is a VTEP at its loopback; VLAN -> VNI; EVPN
#  (BGP l2vpn evpn) is the control plane. Defensive (never raises), cached ~15s so
#  repeated UI polls stay cheap. Provenance is REAL (SSH-counter/CLI), NOT gNMI.
# ═══════════════════════════════════════════════════════════════════════════════
# Overlay (EVPN/VXLAN) view: reads each leaf over SSH via its mgmt_ip, so it only works in
# ssh mode with real switches. fabgen.py emits no mgmt_ip, so on a containerlab fabric every
# VTEP comes back empty, and the page doesn't offer the view. /api/overlay stays for ssh mode.
_overlay_cache = {"ts": 0.0, "data": None}
_OVERLAY_TTL = 15.0
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def _split_markers(text, markers):
    """Split one combined SSH stdout into sections by our own echo'd @@MARKER lines."""
    out, cur = {}, None
    for line in (text or "").splitlines():
        s = line.strip()
        if s in markers:
            cur = s
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v) for k, v in out.items()}


def _parse_vlanvni(text):
    """{VlanNN: VNI} from 'show vxlan vlanvnimap' — parse defensively (ascii table)."""
    out = {}
    for line in (text or "").splitlines():
        toks = [t for t in line.replace("|", " ").split() if t]
        vlan = vni = None
        for t in toks:
            m = re.match(r"(?i)^vlan(\d+)$", t)
            if m:
                vlan = "Vlan" + m.group(1)
            elif t.isdigit():
                vni = int(t)
        if vlan and vni is not None:
            out[vlan] = vni
    return out


def _parse_vxlan_tunnels(text):
    """[{dip,status}] from 'show vxlan tunnel' rows (SIP DIP CreationSource OperStatus)."""
    tunnels = []
    for line in (text or "").splitlines():
        toks = [t for t in line.replace("|", " ").split() if t]
        ips = [t for t in toks if _IP_RE.match(t)]
        if len(ips) < 2:                      # header/separator rows carry no IP pair
            continue
        dip = ips[1]                           # ips[0]=SIP(this leaf), ips[1]=DIP(remote VTEP)
        status = None
        for t in toks:
            tl = t.lower()
            if "oper" in tl or tl in ("up", "down"):
                status = t
        tunnels.append({"dip": dip, "status": status or toks[-1]})
    return tunnels


def _parse_evpn_summary(text):
    """(peers, established) from 'show bgp l2vpn evpn summary'. Use FRR's explicit
    'Total number of neighbors [established] N' footer — robust on an unnumbered
    fabric where neighbor rows are interface names (Ethernet0), not IPs, and the
    last column is PfxSnt rather than the State/PfxRcd field."""
    peers = estab = 0
    for line in (text or "").splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("total number of neighbors established"):
            m = re.search(r"(\d+)", s)
            if m:
                estab = int(m.group(1))
        elif low.startswith("total number of neighbors"):
            m = re.search(r"(\d+)", s)
            if m:
                peers = int(m.group(1))
    return peers, estab


def build_overlay(force=False):
    """GET /api/overlay payload — the REAL EVPN/VXLAN overlay read per-leaf over SSH.
    Cached ~15s. Never raises: any per-leaf error is skipped, the leaf still appears."""
    now = time.time()
    if not force and _overlay_cache["data"] is not None and now - _overlay_cache["ts"] < _OVERLAY_TTL:
        return _overlay_cache["data"]
    with _fab_lock:
        leaves = list(LEAVES)
        node_by_id = {n.get("id"): n for n in TOPOLOGY_NODES}
    remote = ("echo @@VNIMAP; sudo show vxlan vlanvnimap 2>/dev/null; "
              "echo @@TUNNEL; sudo show vxlan tunnel 2>/dev/null; "
              "echo @@EVPN; sudo vtysh -c 'show bgp l2vpn evpn summary' 2>/dev/null")
    vteps, vni_map = [], {}
    for lf in leaves:
        entry = {"leaf": lf, "sip": (node_by_id.get(lf) or {}).get("loopback"),
                 "vnis": [], "tunnels": [], "evpn_peers": 0, "evpn_estab": 0}
        try:
            rc, out, _err = _run(_node_ssh(lf, remote), timeout=12)
            if rc == 0 and out:
                secs = _split_markers(out, ["@@VNIMAP", "@@TUNNEL", "@@EVPN"])
                vmap = _parse_vlanvni(secs.get("@@VNIMAP", ""))
                entry["vnis"] = sorted(vmap.values())
                vni_map.update(vmap)
                entry["tunnels"] = _parse_vxlan_tunnels(secs.get("@@TUNNEL", ""))
                entry["evpn_peers"], entry["evpn_estab"] = _parse_evpn_summary(secs.get("@@EVPN", ""))
        except Exception as e:   # SSH/parse failure on one leaf must not sink the rest
            print("build_overlay: %s skipped (%s)" % (lf, e), file=sys.stderr)
        vteps.append(entry)
    data = {"vteps": vteps, "vni_map": vni_map,
            "note": ("VXLAN builds virtual L2 tenant networks on top of the L3 fabric; "
                     "EVPN is the BGP control plane that learns them. Read live over SSH "
                     "(show vxlan …, vtysh show bgp l2vpn evpn) — REAL, not modeled.")}
    _overlay_cache["data"] = data
    _overlay_cache["ts"] = now
    return data


# ─── live coverage audit (STATE_DB/COUNTERS_DB vs labeled platform fixture) ──────
_audit_cache = {"ts": 0.0, "data": None, "target": None}
_AUDIT_TTL = 30.0


def _default_audit_target():
    """The leaf the audit runs against: the first leaf."""
    with _fab_lock:
        leaves = list(LEAVES)
    return leaves[0] if leaves else "leaf1"


def build_audit(force=False):
    """GET /api/audit payload — the LIVE coverage audit of one leaf's own telemetry,
    read over SSH or docker exec (redis STATE_DB/COUNTERS_DB/CONFIG_DB, kernel /sys, FRR vtysh) and
    classified POPULATED / NULL / ABSENT against a labeled platform fixture. The
    grey/red rows are the point: a virtual switch (no ASIC/PMON) cannot fill the
    hardware-only fields a real box does. Cached ~30s. Never raises."""
    now = time.time()
    target = _default_audit_target()
    if (not force and _audit_cache["data"] is not None
            and _audit_cache["target"] == target
            and now - _audit_cache["ts"] < _AUDIT_TTL):
        return _audit_cache["data"]
    if _covaudit is None:
        return {"target": target, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "live_reachable": False, "categories": [],
                "summary": {"populated": 0, "null": 0, "absent": 0, "coverage_pct": 0.0},
                "fixture_label": "", "note": "coverage_audit module unavailable on this host."}

    # one batched, sudo-capable remote string per probe → a single round trip; bounded time.
    # Real switches (ssh mode) get it over SSH; the virtual lab's switches via docker exec.
    def probe_fn(remote):
        if EXEC.get("mode") == "ssh":
            return _run(_node_ssh(target, remote), timeout=15)
        return _run(_cnode(target, "sh", "-c", remote), timeout=15)

    try:
        data = _covaudit.build_audit(target, probe_fn)
    except Exception as e:  # build_audit already guards, this is belt-and-braces
        print("build_audit: %s" % e, file=sys.stderr)
        data = {"target": target, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "live_reachable": False, "categories": [],
                "summary": {"populated": 0, "null": 0, "absent": 0, "coverage_pct": 0.0},
                "fixture_label": "", "note": "audit failed: %s" % str(e)[:160]}
    _audit_cache["data"] = data
    _audit_cache["ts"] = now
    _audit_cache["target"] = target
    return data


# ═══════════════════════════════════════════════════════════════════════════════
#  PHASE A2 — per-switch drilldown, fleet table, capability matrix, traffic studio,
#  demo-path RTT. Every SSH-backed builder mirrors the overlay/audit cache pattern
#  (module dict + TTL + ONE batched sudo'd SSH), so NONE can ever block the 3s
#  /api/topology poll (which stays Prom-only). All builders NEVER raise.
# ═══════════════════════════════════════════════════════════════════════════════
def _first_int(text, default=0):
    """First integer found in a chunk of text; default on empty/garbage."""
    if text is None:
        return default
    m = re.search(r"-?\d+", str(text))
    return int(m.group(0)) if m else default


# ── demo-path RTT — REAL dataplane round-trip (leaf->spine->leaf) ──────────────
_RTT_RE = re.compile(r"rtt [^=]+= ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+) ms")
_LOSS_RE = re.compile(r"(\d+)% packet loss")
_rtt_cache = {"ts": 0.0, "data": None}
_rtt_lock = threading.Lock()
_RTT_TTL = 10.0


def _node_loopback(node):
    """Loopback IP of a fabric node from fabric.json. None if unknown."""
    with _fab_lock:
        for n in TOPOLOGY_NODES:
            if n.get("id") == node:
                return n.get("loopback")
    return None


def _peer_leaf_of(sid):
    """The demo-path peer leaf for a leaf: LEAVES[-1] for LEAVES[0], else LEAVES[0]."""
    with _fab_lock:
        leaves = list(LEAVES)
    if len(leaves) < 2 or sid not in leaves:
        return None
    return leaves[0] if sid == leaves[-1] else leaves[-1]


def _parse_rtt(text, src=None, dst=None):
    """Parse a `ping -c N` block -> {min_ms,avg_ms,max_ms,mdev_ms,loss_pct}. All None on fail."""
    out = {"min_ms": None, "avg_ms": None, "max_ms": None, "mdev_ms": None, "loss_pct": None}
    m = _RTT_RE.search(text or "")
    if m:
        out["min_ms"] = float(m.group(1))
        out["avg_ms"] = float(m.group(2))
        out["max_ms"] = float(m.group(3))
        out["mdev_ms"] = float(m.group(4))
    lm = _LOSS_RE.search(text or "")
    if lm:
        out["loss_pct"] = int(lm.group(1))
    return out


def _rtt_probe(src_leaf, dst_leaf):
    """REAL dataplane RTT src_leaf -> dst_leaf loopback. Bounded (~0.6s), never raises."""
    peer_lo = _node_loopback(dst_leaf)
    if not peer_lo:
        return {"from": src_leaf, "to": dst_leaf, "avg_ms": None,
                "note": "no loopback for %s" % dst_leaf}
    rc, out, _err = _run(_node_ssh(src_leaf, "ping -c 3 -i 0.2 -W 1 %s" % peer_lo), timeout=8)
    p = _parse_rtt(out, src_leaf, dst_leaf)
    p["from"] = src_leaf
    p["to"] = dst_leaf
    if p["avg_ms"] is None:
        p["note"] = "rtt probe failed/unparsed (rc=%s)" % rc
    return p


def build_rtt(force=False):
    """GET /api/rtt payload — the demo-path RTT banner (cached ~10s). REAL, not modeled.
    Default path = the first leaf -> the last leaf (LEAVES[0] -> LEAVES[-1])."""
    now = time.time()
    with _rtt_lock:
        if not force and _rtt_cache["data"] is not None and now - _rtt_cache["ts"] < _RTT_TTL:
            return _rtt_cache["data"]
    with _fab_lock:
        leaves = list(LEAVES)
    data = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "from": None, "to": None, "min_ms": None, "avg_ms": None, "max_ms": None,
            "mdev_ms": None, "loss_pct": None, "hops": "leaf->spine->leaf",
            "note": ("REAL dataplane RTT — measured, not modeled; contrasts the "
                     "modeled heat/power")}
    if len(leaves) < 2:
        data["note"] = "need >=2 leaves for a demo-path RTT"
    else:
        src = leaves[0]
        dst = leaves[-1]
        if src == dst:
            dst = leaves[-1] if src != leaves[-1] else leaves[0]
        try:
            p = _rtt_probe(src, dst)
            data.update({"from": src, "to": dst, "min_ms": p.get("min_ms"),
                         "avg_ms": p.get("avg_ms"), "max_ms": p.get("max_ms"),
                         "mdev_ms": p.get("mdev_ms"), "loss_pct": p.get("loss_pct")})
            if p.get("note"):
                data["note"] = p["note"]
        except Exception as e:
            data["note"] = "rtt probe error: %s" % str(e)[:120]
    with _rtt_lock:
        _rtt_cache["data"] = data
        _rtt_cache["ts"] = now
    return data


# ── GET /api/switch/<id> — on-demand per-switch drilldown (cached PER-ID ~12s) ──
_switch_cache = {}          # id -> {"ts","data"}
_switch_lock = threading.Lock()
_SWITCH_TTL = 12.0


def _switch_remote(peer_lo=None):
    """ONE batched, sudo-capable remote string (mirrors coverage_audit's @@marker
    style). Fast redis/proc/yml/vtysh-json reads only; RTT appended for leaves."""
    parts = [
        'echo @@VER; grep -E "build_version|asic_type" /etc/sonic/sonic_version.yml 2>/dev/null',
        'echo @@META; redis-cli -n 4 hget "DEVICE_METADATA|localhost" hwsku; '
        'redis-cli -n 4 hget "DEVICE_METADATA|localhost" type',
        'echo @@LOAD; cat /proc/loadavg',
        'echo @@NPROC; nproc',
        "echo @@MEM; awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print t,a}' /proc/meminfo",
        'echo @@UP; cat /proc/uptime',
        'echo @@BGP; sudo vtysh -c "show bgp summary json"',
        'echo @@LLDP; sudo lldpctl -f keyvalue 2>/dev/null | grep -E "\\.(port\\.descr|chassis\\.name)="',
        'echo @@MAPS; redis-cli -n 2 hlen COUNTERS_PORT_NAME_MAP; redis-cli -n 2 hlen COUNTERS_QUEUE_NAME_MAP',
    ]
    if peer_lo:
        parts.append('echo @@RTT; ping -c 3 -i 0.2 -W 1 %s' % peer_lo)
    parts.append('echo @@END')
    return "; ".join(parts)


def _parse_bgp_detail(section):
    """(router_id, local_as, peers[], total, established) from `show bgp summary json`."""
    try:
        j = json.loads(section)
    except Exception:
        return {"router_id": None, "local_as": None, "peers": [], "total": 0, "established": 0}
    uni = j.get("ipv4Unicast") or j.get("default", {}).get("ipv4Unicast") or {}
    peers = uni.get("peers") or {}
    out, estab = [], 0
    for iface, p in peers.items():
        st = str(p.get("state", "")).strip()
        if st.lower() == "established" or (not st and p.get("peerState") == "OK"):
            estab += 1
        out.append({"iface": iface, "remote_as": p.get("remoteAs"),
                    "state": st or p.get("peerState", ""),
                    "pfx_rcd": p.get("pfxRcd"), "pfx_snt": p.get("pfxSnt"),
                    "uptime": p.get("peerUptime")})
    total = uni.get("peerCount", len(peers))
    return {"router_id": uni.get("routerId"), "local_as": uni.get("as"),
            "peers": out, "total": int(total or 0), "established": estab}


def _parse_lldp(section):
    """[{local_port,remote_port,remote_name}] from lldpctl -f keyvalue lines."""
    neigh = {}
    for line in (section or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        parts = k.split(".")
        if len(parts) >= 4 and parts[0] == "lldp":
            lp = parts[1]
            attr = ".".join(parts[2:])
            d = neigh.setdefault(lp, {})
            if attr == "port.descr":
                d["remote_port"] = v
            elif attr == "chassis.name":
                d["remote_name"] = v
    return [{"local_port": lp, "remote_port": d.get("remote_port"),
             "remote_name": d.get("remote_name")} for lp, d in sorted(neigh.items())]


def _switch_prom(sid, ctxsw):
    """PROM half (FREE): ctx switch dict + a scoped per-instance sensor breakdown."""
    q_temp = 'sonic_temperature_celsius{instance="%s"}' % sid
    q_fan = 'sonic_fan_rpm{instance="%s"}' % sid
    q_po = 'sonic_psu_output_power_watts{instance="%s"}' % sid
    q_pi = 'sonic_psu_input_power_watts{instance="%s"}' % sid
    q_load = 'fabric_switch_load_bps{switch="%s"}' % sid
    R = _prom_batch([q_temp, q_fan, q_po, q_pi, q_load])
    temps = {}
    for row in R[q_temp]:
        sensor = row.get("metric", {}).get("sensor")
        try:
            temps[sensor] = float(row["value"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    fans = []
    for row in R[q_fan]:
        fan = row.get("metric", {}).get("fan")
        try:
            fans.append({"fan": fan, "rpm": int(round(float(row["value"][1])))})
        except (KeyError, TypeError, ValueError, IndexError):
            continue

    def _sum(res):
        t = 0.0
        for row in res:
            try:
                t += float(row["value"][1])
            except (KeyError, TypeError, ValueError, IndexError):
                continue
        return t

    # ABSENT vs ZERO applies to the drilldown too: a node whose platform has no
    # sensor must show ABSENT here, not a confident 0.0 summed from an empty
    # Prometheus vector. On every emulated node the vectors are populated, so this
    # block returns exactly the same numbers it always did.
    gone = declared_absent(sid)
    # On an EMULATED node with nothing declared absent, keep the legacy 0.0 default
    # verbatim so this endpoint is unchanged for today's fabric; only a node that
    # genuinely cannot produce the field reports ABSENT.
    _dflt = None if (gone or node_binding(sid).get("physical")) else 0.0

    def _t(sensor, fallback_key, metric):
        if metric in gone:
            return None
        v = temps.get(sensor)
        if v is None and fallback_key:
            v = (ctxsw or {}).get(fallback_key)
        if v is None:
            v = _dflt
        return None if v is None else round(v, 1)

    def _agg(res, metric):
        if metric in gone:
            return None
        if not res:
            # nothing published for this node: fall back to the context value, and
            # keep None rather than inventing a 0 when that is absent too.
            v = (ctxsw or {}).get(metric, _dflt)
            if v is None:
                v = _dflt
            return None if v is None else round(v, 1)
        return round(_sum(res), 1)

    prom = dict(ctxsw or {})
    prom.update({
        "asic_c": _t("ASIC", "asic_c", "asic_c"),
        "cpu_c": _t("CPU", "cpu_c", "cpu_c"),
        "inlet_c": _t("inlet", None, "inlet_c"),
        "outlet_c": _t("outlet", None, "outlet_c"),
        "fans": sorted(fans, key=lambda f: f["fan"] or ""),
        "psu_out_w": _agg(R[q_po], "power_w"),
        "psu_in_w": _agg(R[q_pi], "psu_in_w"),
        "load_bps": _agg(R[q_load], "load_bps"),
        "labels": {"source": "prometheus", "energy": "MODELED (from real load)",
                   "traffic": "REAL kernel counters", "gnmi": "REAL gNMI/OpenConfig"},
    })
    if gone:
        prom["absent"] = sorted(gone)
        prom["absent_why"] = gone
    b = node_binding(sid)
    if b.get("physical"):
        prom["labels"]["energy"] = "REAL platform sensors (physical node — nothing modeled)"
        prom["physical"] = True
        prom["platform"] = b.get("platform")
        prom["nos"] = b.get("nos")
    return prom


def _parse_switch_ssh(sid, secs, peer, role):
    """Parse the batched SSH sections into the provenance-labeled ssh half. Never raises."""
    # identity ------------------------------------------------------------------
    build_version = asic_type = None
    for line in secs.get("@@VER", "").splitlines():
        m = re.search(r"build_version:\s*'?([^'\s]+)'?", line)
        if m:
            build_version = m.group(1)
        m2 = re.search(r"asic_type:\s*'?([^'\s]+)'?", line)
        if m2:
            asic_type = m2.group(1)
    meta = [l.strip() for l in secs.get("@@META", "").splitlines() if l.strip()]
    identity = {"build_version": build_version,
                "hwsku": meta[0] if len(meta) >= 1 else None,
                "role": meta[1] if len(meta) >= 2 else None,
                "asic_type": asic_type, "source": "ssh:redis/proc/yml"}
    if asic_type == "vs":
        identity["badge"] = "virtual ASIC — hardware sensors modeled"

    # host ----------------------------------------------------------------------
    lt = secs.get("@@LOAD", "").split()

    def _f(toks, i, d=None):
        try:
            return float(toks[i])
        except (IndexError, ValueError):
            return d
    l1, l5, l15 = _f(lt, 0), _f(lt, 1), _f(lt, 2)
    ncpu = _first_int(secs.get("@@NPROC", ""), 0)
    mem = secs.get("@@MEM", "").split()
    mt = _first_int(mem[0]) if len(mem) >= 1 else 0
    ma = _first_int(mem[1]) if len(mem) >= 2 else 0
    upt = secs.get("@@UP", "").split()
    host = {"cpu_load1": l1, "cpu_load5": l5, "cpu_load15": l15, "ncpu": ncpu,
            "cpu_load_pct": round(l1 / ncpu * 100, 1) if (l1 is not None and ncpu) else None,
            "mem_total_kb": mt, "mem_avail_kb": ma,
            "mem_used_pct": round((mt - ma) * 100.0 / mt, 1) if mt else None,
            "uptime_s": int(_f(upt, 0, 0.0) or 0.0), "source": "ssh:proc"}

    # bgp / lldp / asic-null ----------------------------------------------------
    bgpd = _parse_bgp_detail(secs.get("@@BGP", ""))
    lldp = _parse_lldp(secs.get("@@LLDP", ""))
    maps = secs.get("@@MAPS", "").split()
    pmap = _first_int(maps[0]) if len(maps) >= 1 else 0
    qmap = _first_int(maps[1]) if len(maps) >= 2 else 0
    asic_null = {"port_name_map_hlen": pmap, "queue_name_map_hlen": qmap, "source": "ssh:redis"}
    if pmap == 0 and qmap == 0:
        asic_null["badge"] = ("REAL-but-zero (SAI counter map empty on vs; "
                              "CLI present, silicon absent)")

    ssh = {
        "identity": identity,
        "host": host,
        "bgp_router_id": bgpd["router_id"], "local_as": bgpd["local_as"],
        "bgp_peers": bgpd["peers"], "bgp_established": bgpd["established"],
        "bgp_total": bgpd["total"], "bgp_source": "ssh:vtysh",
        "lldp_neighbors": lldp, "lldp_count": len(lldp), "lldp_source": "ssh:lldpctl",
        "asic_null": asic_null,
    }
    if "@@RTT" in secs and peer:
        p = _parse_rtt(secs.get("@@RTT", ""))
        ssh["demo_path_rtt"] = {"from": sid, "to": peer, "avg_ms": p["avg_ms"],
                                "loss_pct": p["loss_pct"], "path": [sid, peer],
                                "hops": "leaf->spine->leaf",
                                "source": "REAL ping (leaf->spine->leaf dataplane)"}
    return ssh


def build_switch_detail(sid, force=False):
    """GET /api/switch/<id> — merges a FREE prom half + a cached (~12s) batched-SSH
    half. NEVER raises: SSH failure -> reachable:false, ssh:{}, prom half still renders."""
    now = time.time()
    with _switch_lock:
        c = _switch_cache.get(sid)
        if not force and c and now - c["ts"] < _SWITCH_TTL:
            return c["data"]
    with _fab_lock:
        role = KIND.get(sid, "")
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # PROM half (free, no SSH) --------------------------------------------------
    try:
        ctx = build_context()
        ctxsw = ctx.get("switches", {}).get(sid)
    except Exception:
        ctxsw = None
    try:
        prom = _switch_prom(sid, ctxsw)
    except Exception as e:
        prom = dict(ctxsw or {})
        prom["note"] = "prom sensor batch failed: %s" % str(e)[:120]

    # SSH half (one batched sudo remote; RTT folded in for leaves) ---------------
    ssh, reachable, note = {}, False, None
    peer = _peer_leaf_of(sid) if role == "leaf" else None
    peer_lo = _node_loopback(peer) if peer else None
    markers = ["@@VER", "@@META", "@@LOAD", "@@NPROC", "@@MEM", "@@UP",
               "@@BGP", "@@LLDP", "@@MAPS"]
    if peer_lo:
        markers.append("@@RTT")
    markers.append("@@END")
    try:
        rc, out, err = _run(_node_ssh(sid, _switch_remote(peer_lo)), timeout=15)
        reachable = (rc == 0)
        if rc == 0 and out:
            secs = _split_markers(out, markers)
            ssh = _parse_switch_ssh(sid, secs, peer, role)
        else:
            note = "ssh probe rc=%s (%s)" % (rc, (err or "").strip()[:120])
    except Exception as e:
        note = "ssh probe error: %s" % str(e)[:120]

    data = {"id": sid, "role": role, "mgmt_ip": _mgmt_ip(sid),
            "reachable": reachable, "generated": generated, "prom": prom, "ssh": ssh}
    if note:
        data["note"] = note
    with _switch_lock:
        _switch_cache[sid] = {"ts": now, "data": data}
    return data


# ── GET /api/fleet — the all-switches table (cached ~15s) ──────────────────────
_fleet_cache = {"ts": 0.0, "data": None}
_fleet_lock = threading.Lock()
_FLEET_TTL = 15.0
_FLEET_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="fleet")


def _bgp_expected(sid, role):
    """Derivable expected BGP peer count: leaf=#uplinks, spine=#leaf fabric ports, else None."""
    with _fab_lock:
        if role == "leaf":
            return len(UPLINKS.get(sid, [])) or None
    if role == "spine":
        return len(_spine_fabric_ports(sid)) or None
    return None


def _fleet_proc_probe(sid):
    """One tiny /proc read per switch (cpu/mem/uptime). Never raises. ~1s each."""
    remote = ("cat /proc/loadavg; nproc; "
              "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print (t-a)*100/t}' /proc/meminfo; "
              "cut -d' ' -f1 /proc/uptime")
    rc, out, _err = _run(_node_ssh(sid, remote), timeout=8)
    row = {"reachable": rc == 0, "cpu_load1": None, "ncpu": None,
           "cpu_load_pct": None, "mem_used_pct": None, "uptime_s": None}
    if rc == 0 and out:
        try:
            lines = [x for x in out.splitlines() if x.strip()]
            la = lines[0].split() if len(lines) >= 1 else []
            l1 = float(la[0]) if la else None
            ncpu = int(float(lines[1])) if len(lines) >= 2 else None
            mempct = float(lines[2]) if len(lines) >= 3 else None
            upt = int(float(lines[3])) if len(lines) >= 4 else None
            row.update({"cpu_load1": l1, "ncpu": ncpu,
                        "cpu_load_pct": round(l1 / ncpu * 100, 1) if (l1 is not None and ncpu) else None,
                        "mem_used_pct": round(mempct, 1) if mempct is not None else None,
                        "uptime_s": upt})
        except Exception:
            pass
    return sid, row


def build_fleet(force=False):
    """GET /api/fleet — Prom layer (free) + a CONCURRENT cpu/mem/uptime SSH sweep,
    cached ~15s. NEVER raises. Unreachable switches still render their Prom row."""
    now = time.time()
    with _fleet_lock:
        if not force and _fleet_cache["data"] is not None and now - _fleet_cache["ts"] < _FLEET_TTL:
            return _fleet_cache["data"]
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _fab_lock:
        ids = [n["id"] for n in TOPOLOGY_NODES if n.get("kind") in ("spine", "leaf", "access")]
        role_by = {i: KIND.get(i, "") for i in ids}
    try:
        ctx = build_context()
    except Exception:
        ctx = {}
    ctxsw = ctx.get("switches", {})
    anom = {a["switch"]: a for a in _anom_active_list()}

    # concurrent /proc sweep across ALL switches (whole sweep ~1-2s wall)
    proc = {}
    try:
        futs = {i: _FLEET_POOL.submit(_fleet_proc_probe, i) for i in ids}
        for i, f in futs.items():
            try:
                _sid, r = f.result(timeout=10)
                proc[i] = r
            except Exception:
                proc[i] = {"reachable": False, "cpu_load_pct": None,
                           "mem_used_pct": None, "uptime_s": None}
    except Exception:
        pass

    order = {"spine": 0, "leaf": 1, "access": 2}
    rows, reachable_n = [], 0
    sum_power = sum_tx = 0.0
    hottest = {"name": None, "asic_c": 0.0}
    any_anom = False
    for i in sorted(ids, key=lambda x: (order.get(role_by[x], 9), x)):
        s = ctxsw.get(i, {})
        pr = proc.get(i, {})
        reach = bool(pr.get("reachable"))
        a = anom.get(i)
        rows.append({
            "id": i, "role": role_by[i], "mgmt_ip": _mgmt_ip(i), "reachable": reach,
            "asic_c": s.get("asic_c"), "power_w": s.get("power_w"),
            "load_bps": s.get("load_bps"), "tx_bps": s.get("tx_bps"),
            "drops_ps": s.get("drops_ps"), "bgp_established": s.get("bgp_peers"),
            "bgp_expected": _bgp_expected(i, role_by[i]),
            "fib_vias": s.get("fib_vias"), "intensity": s.get("intensity"),
            "ecn_marks": s.get("ecn_marks"), "fan_rpm": s.get("fan_rpm"),
            "gnmi_live": bool(s.get("gnmi_live")),
            "cpu_load_pct": pr.get("cpu_load_pct"), "mem_used_pct": pr.get("mem_used_pct"),
            "uptime_s": pr.get("uptime_s"), "anomaly": a,
        })
        if reach:
            reachable_n += 1
        sum_power += s.get("power_w") or 0.0
        sum_tx += s.get("tx_bps") or 0.0
        ac = s.get("asic_c") or 0.0
        if hottest["name"] is None or ac > hottest["asic_c"]:
            hottest = {"name": i, "asic_c": ac}
        if a:
            any_anom = True
    data = {"generated": generated, "switches": rows,
            "totals": {"switches": len(rows), "reachable": reachable_n,
                       "sum_power_w": round(sum_power, 1), "sum_tx_bps": round(sum_tx, 1),
                       "hottest": hottest, "any_anomaly": any_anom},
            "note": ("asic/power/fan MODELED from real load; traffic/bgp/cpu/mem REAL; "
                     "per-switch detail at /api/switch/<id>")}
    with _fleet_lock:
        _fleet_cache["data"] = data
        _fleet_cache["ts"] = now
    return data


# ── TRAFFIC STUDIO — general traffic generator on the SAME real-ping plumbing ──
#  RESERVED SIGNATURE: ambient=`-i 0.05 -s 100`, surge=`-i 0.003 -s 1400`,
#  elephant=`-i 0.003 -s 1200`, storm=`-i 0.003 -s 1250`. THIS endpoint reserves
#  the SIZE axis `-s 1300` (unique) — pkill on `ping .* -s 1300` hits ONLY traffic
#  studio streams (any rate), never ambient/scenarios, and their pkills never hit us.
TRAFFIC_MAX_STREAMS = int(os.environ.get("TRAFFIC_MAX_STREAMS", "12"))
TRAFFIC_SIG = "ping -i 0.003 -s 1300"            # RESERVED — never reuse the -s 1300 axis
# kill 3x over ~1s so a stream still spawning during an all-all start-race is caught too
TRAFFIC_KILL = 'for i in 1 2 3; do sudo pkill -f "ping .* -s 1300" 2>/dev/null; sleep 0.3; done; true'
_TRAFFIC_IV = {"high": "0.003", "low": "0.02"}
_traffic_state = {}          # {} idle; else {"pattern","streams":[{src,dst,dst_lo}],"started","rate"}
_traffic_lock = threading.Lock()


def _norm_pattern(p):
    s = (p or "").strip().lower().replace("_", "-").replace(" ", "")
    if s in ("all-all", "all2all", "allall", "mesh", "alltoall"):
        return "all-all"
    if s in ("1-1", "1to1", "one-one", "11"):
        return "1-1"
    if s in ("1-n", "1ton", "one-n"):
        return "1-N"
    if s in ("n-1", "nto1", "n-one"):
        return "N-1"
    return None


def _resolve_leaf(x):
    """Map a src/dst arg (leaf id, host/PC id, or None) to a leaf id. None if unresolvable."""
    if not x:
        return None
    with _fab_lock:
        is_leaf = x in set(LEAVES)
    return x if is_leaf else _leaf_of_host(x)


def _leaf_loopback_map():
    with _fab_lock:
        leaves = list(LEAVES)
        lo = {n.get("id"): n.get("loopback") for n in TOPOLOGY_NODES}
    return leaves, lo


def _traffic_build_streams(pattern, src, dst):
    """Return (streams, capped, msg). streams=[{src,dst,dst_lo}] leaf->peer-loopback."""
    leaves, lo = _leaf_loopback_map()
    if len(leaves) < 2:
        return [], False, "need >=2 leaves for traffic patterns"
    src_leaf, dst_leaf = _resolve_leaf(src), _resolve_leaf(dst)
    streams = []
    if pattern == "1-1":
        src_leaf = src_leaf or leaves[0]
        dst_leaf = dst_leaf or leaves[-1]
        if src_leaf == dst_leaf:
            return [], False, "1-1 needs distinct src and dst leaves"
        if lo.get(dst_leaf):
            streams.append({"src": src_leaf, "dst": dst_leaf, "dst_lo": lo[dst_leaf]})
    elif pattern == "1-N":
        src_leaf = src_leaf or leaves[0]
        for b in leaves:
            if b != src_leaf and lo.get(b):
                streams.append({"src": src_leaf, "dst": b, "dst_lo": lo[b]})
    elif pattern == "N-1":
        dst_leaf = dst_leaf or leaves[-1]
        if not lo.get(dst_leaf):
            return [], False, "N-1 dst leaf has no loopback"
        for a in leaves:
            if a != dst_leaf:
                streams.append({"src": a, "dst": dst_leaf, "dst_lo": lo[dst_leaf]})
    elif pattern == "all-all":
        for a in leaves:
            for b in leaves:
                if a != b and lo.get(b):
                    streams.append({"src": a, "dst": b, "dst_lo": lo[b]})
    else:
        return [], False, "unknown pattern %r (use 1-1|1-N|N-1|all-all)" % pattern
    capped = False
    msg = "%d stream(s) for %s" % (len(streams), pattern)
    if len(streams) > TRAFFIC_MAX_STREAMS:
        streams = streams[:TRAFFIC_MAX_STREAMS]
        capped = True
        msg = "trimmed to cap TRAFFIC_MAX_STREAMS=%d (pattern %s)" % (TRAFFIC_MAX_STREAMS, pattern)
    if not streams:
        return [], False, "no runnable streams (missing loopbacks?)"
    return streams, capped, msg


def _traffic_stop_locked():
    """pkill the reserved -s 1300 signature on EVERY leaf, clear state. Assumes _traffic_lock."""
    with _fab_lock:
        leaves = list(LEAVES)
    for lf in leaves:
        try:
            _run(_node_ssh(lf, TRAFFIC_KILL), timeout=15)
        except Exception:
            pass
    _traffic_state.clear()
    return {"ok": True, "running": False, "pattern": None, "streams": [], "count": 0,
            "capped": False, "msg": "traffic stopped — all studio flows (-s 1300) killed on every leaf"}


def _traffic_stop_all():
    """State-independent traffic sweep for scenario_reset (acquires its own lock)."""
    with _traffic_lock:
        return _traffic_stop_locked()


# ALL generated load — overload (-s 1400) AND traffic-studio (-s 1300) — is high-rate
# `ping -i 0.003`; the ambient baseline is `ping -i 0.05`. So this one signature kills
# every generated flow while leaving the ambient hum intact. This is the true
# "return to baseline" sweep: no orphan flow can keep the fabric hot while the
# buttons read idle (the exact desync we hit).
_KILL_ALL_GENERATED = ('for i in 1 2 3; do sudo pkill -f "ping -i 0.003" 2>/dev/null; '
                       'sleep 0.3; done; true')


def _kill_all_generated():
    """Kill every generated flow (-i 0.003) on all leaf/access switches, preserving
    ambient (-i 0.05). Belt-and-braces baseline sweep used by reset + on startup."""
    with _fab_lock:
        nodes = [n["id"] for n in TOPOLOGY_NODES if n.get("kind") in ("leaf", "access")]
    n = 0
    for nid in nodes:
        try:
            _run(_node_ssh(nid, _KILL_ALL_GENERATED), timeout=15); n += 1
        except Exception:
            pass
    return n


def traffic_control(pattern, src=None, dst=None, action="start", rate="high"):
    """POST /api/traffic handler. Real ICMP on real leaves toward peer leaf loopbacks
    (leaf->spine->leaf). Idempotent start; stop pkills the reserved signature. Never raises."""
    action = (action or "start").strip().lower()
    with _traffic_lock:
        if action == "stop":
            return _traffic_stop_locked()
        if action != "start":
            return {"ok": False, "running": bool(_traffic_state), "pattern": _traffic_state.get("pattern"),
                    "streams": _traffic_state.get("streams", []), "count": len(_traffic_state.get("streams", [])),
                    "capped": False, "msg": "unknown action %r (use start|stop)" % action}
        if _traffic_state:
            return {"ok": True, "running": True, "pattern": _traffic_state.get("pattern"),
                    "streams": _traffic_state.get("streams", []),
                    "count": len(_traffic_state.get("streams", [])), "capped": False,
                    "msg": "already running (%s) — stop it first" % _traffic_state.get("pattern")}
        norm = _norm_pattern(pattern)
        if not norm:
            return {"ok": False, "running": False, "pattern": None, "streams": [], "count": 0,
                    "capped": False, "msg": "unknown pattern %r (use 1-1|1-N|N-1|all-all)" % pattern}
        try:
            streams, capped, msg = _traffic_build_streams(norm, src, dst)
        except Exception as e:
            return {"ok": False, "running": False, "pattern": norm, "streams": [], "count": 0,
                    "capped": False, "msg": "build error: %s" % str(e)[:120]}
        if not streams:
            return {"ok": False, "running": False, "pattern": norm, "streams": [], "count": 0,
                    "capped": capped, "msg": msg}
        iv = _TRAFFIC_IV.get((rate or "high").strip().lower(), "0.003")

        def _start_one(stm):
            cmd = _node_ssh(stm["src"], "sudo nohup ping -i %s -s 1300 %s >/dev/null 2>&1 </dev/null & echo ok"
                            % (iv, stm["dst_lo"]))
            rc, _o, _e = _run(cmd, timeout=15)
            return stm, rc
        started = []
        try:
            futs = [_FLEET_POOL.submit(_start_one, s) for s in streams]
            for f in futs:
                try:
                    stm, rc = f.result(timeout=20)
                    if rc == 0:
                        started.append(stm)
                except Exception:
                    pass
        except Exception as e:
            return {"ok": False, "running": False, "pattern": norm, "streams": [], "count": 0,
                    "capped": capped, "msg": "launch error: %s" % str(e)[:120]}
        if not started:
            return {"ok": False, "running": False, "pattern": norm, "streams": [], "count": 0,
                    "capped": capped, "msg": "could not launch any stream over SSH"}
        _traffic_state.clear()
        _traffic_state.update({"pattern": norm, "rate": (rate or "high"),
                               "streams": [{"src": s["src"], "dst": s["dst"], "dst_lo": s["dst_lo"]}
                                           for s in started],
                               "started": time.time()})
        return {"ok": True, "running": True, "pattern": norm,
                "streams": _traffic_state["streams"], "count": len(started),
                "capped": capped,
                "msg": "%s: launched %d real flow(s) (leaf->spine->leaf, %s rate). %s"
                       % (norm, len(started), (rate or "high"), msg)}


def _traffic_summary():
    """Cheap in-memory truth for the /api/topology poll (no SSH)."""
    try:
        return {"running": bool(_traffic_state), "pattern": _traffic_state.get("pattern"),
                "count": len(_traffic_state.get("streams", []))}
    except Exception:
        return {"running": False, "pattern": None, "count": 0}


# ── GET /api/features — the FABRIC-CAPABILITY matrix (pure aggregator, ZERO new SSH) ──
def build_features():
    """Aggregate already-cached sources into a capability matrix. NEVER raises, adds
    NO SSH (build_context cheap; build_overlay/build_audit already TTL-cached)."""
    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        ctx = build_context()
    except Exception:
        ctx = {}
    switches = ctx.get("switches", {})
    try:
        overlay = build_overlay()
    except Exception:
        overlay = {"vteps": [], "vni_map": {}}
    try:
        audit = build_audit()
    except Exception:
        audit = {"categories": []}
    try:
        active = set(_active_list())
    except Exception:
        active = set()

    audit_by = {}
    for cat in audit.get("categories", []):
        fs = cat.get("fields", [])
        if fs:
            audit_by[cat.get("name")] = fs[0]

    feats = []

    def add(fid, name, status, badge, explainer, why_idle, evidence, source):
        feats.append({"id": fid, "name": name, "status": status, "badge": badge,
                      "explainer": explainer, "why_idle": why_idle,
                      "evidence": evidence, "source": source})

    # ebgp_underlay
    bgp_sum = sum(int(s.get("bgp_peers", 0) or 0) for s in switches.values())
    add("ebgp_underlay", "eBGP unnumbered underlay",
        "active" if bgp_sum > 0 else "idle", "REAL",
        "eBGP unnumbered leaf-spine fabric — every switch peers with its neighbors.",
        "no BGP sessions established", {"bgp_peers_established": bgp_sum},
        "fabric_bgp_established (context)")

    # ecmp
    max_vias = max([int(s.get("fib_vias", 0) or 0) for s in switches.values()] or [0])
    add("ecmp", "ECMP multipath",
        "active" if max_vias > 1 else "idle", "REAL",
        "Equal-cost multipath spreads traffic (and heat) across the spines.",
        "single path — no ECMP fan-out observed", {"max_fib_vias": max_vias},
        "fabric_fib_vias (context)")

    # evpn_vxlan_overlay
    vteps = overlay.get("vteps", [])
    vnis = overlay.get("vni_map", {}) or {}
    evpn_up = sum(int(v.get("evpn_estab", 0) or 0) for v in vteps)
    ov_active = evpn_up > 0 and bool(vnis)
    add("evpn_vxlan_overlay", "EVPN/VXLAN overlay",
        "active" if ov_active else "idle", "REAL (SSH)",
        "VXLAN L2 tenant networks over the L3 fabric; BGP-EVPN control plane.",
        "VTEPs configured, EVPN peers not established",
        {"vteps": len(vteps), "vnis": sorted(set(vnis.values())), "evpn_estab": evpn_up},
        "build_overlay (SSH, cached)")

    # gnmi_read
    g = ctx.get("gnmi") or {}
    add("gnmi_read", "gNMI telemetry (polled Get)",
        "active" if g.get("live") else "idle", "REAL gNMI/OpenConfig",
        "OpenConfig out-octets read (polled) over gNMI Get from a leaf's :8080 gNMI server.",
        "gNMI target unreachable / GNMI_LEAF only",
        {"switch": g.get("switch"), "live": bool(g.get("live"))}, "fabric_gnmi_up")

    # energy_model (always active, MODELED)
    hot = ctx.get("hottest", {}) or {}
    add("energy_model", "Energy model (heat/power)", "active", "MODELED (from real load)",
        "Per-switch ASIC temp & PSU watts modeled from the REAL measured load/intensity.",
        "", {"hottest": hot.get("name"), "hottest_c": hot.get("asic_c")},
        "sonic_temperature/psu (modeled from load)")

    # congestion_ecn (REAL kernel tc RED CE marks)
    ecn_total = sum(int(s.get("ecn_marks", 0) or 0) for s in switches.values())
    add("congestion_ecn", "Congestion / ECN CE marks",
        "active" if ecn_total > 0 else "idle", "REAL (kernel tc RED)",
        "REAL kernel-tc RED ECN CE marks on the demo flow under the congestion storm.",
        "arm the congestion storm to see CE marks", {"ecn_marks": ecn_total},
        "fabric_ecn_marks_total")

    # lldp_discovery (cheap: warm switch-detail cache, else present — never SSH here)
    lldp_warm = None
    try:
        with _switch_lock:
            for wid, wc in _switch_cache.items():
                n = ((wc.get("data") or {}).get("ssh") or {}).get("lldp_count")
                if n:
                    lldp_warm = (wid, n)
                    break
    except Exception:
        lldp_warm = None
    add("lldp_discovery", "LLDP neighbor discovery",
        "active" if lldp_warm else "idle", "REAL (per-switch on drilldown)",
        "Link-layer neighbor discovery; read live per-switch on the drilldown.",
        "open a switch drilldown to read LLDP live",
        {"warm_from": lldp_warm[0] if lldp_warm else None,
         "neighbors": lldp_warm[1] if lldp_warm else None}, "build_switch_detail cache")

    # pfc / queue_watermark from audit categories (single source of truth)
    def _audit_feature(fid, name, catname):
        f = audit_by.get(catname)
        if not f:
            status, note, src = "absent", "no key at all — needs real silicon", "build_audit"
        else:
            ls = f.get("live_status", "")
            note = f.get("note", "") or f.get("platform_note", "")
            src = "build_audit (%s)" % catname
            status = {"POPULATED": "active", "NULL": "real-but-zero"}.get(ls, "absent")
        badge = {"active": "REAL", "real-but-zero": "REAL-but-zero (vs)",
                 "absent": "ABSENT (needs silicon)"}[status]
        add(fid, name, status, badge, note or name, note,
            {"audit_category": catname, "live_status": (f or {}).get("live_status")}, src)
    _audit_feature("pfc", "PFC pause frames", "PFC")
    _audit_feature("queue_watermark", "Queue depth / WRED drops", "Queue / WRED")

    # pmon_sensors — combine Temperature/Fan/PSU audit categories
    pmon_states = [audit_by[cn].get("live_status", "") for cn in
                   ("Temperature", "Fan", "PSU / Power") if cn in audit_by]
    if "POPULATED" in pmon_states:
        pst = "active"
    elif "NULL" in pmon_states:
        pst = "real-but-zero"
    else:
        pst = "absent"
    pbadge = {"active": "REAL", "real-but-zero": "REAL-but-zero (vs)",
              "absent": "ABSENT (needs silicon)"}[pst]
    add("pmon_sensors", "PMON sensors (temp/fan/PSU)", pst, pbadge,
        "PMON thermal/fan/PSU sensors — absent on a virtual switch (no PMON); "
        "energy is MODELED separately.",
        "no PMON on a virtual switch — needs real silicon",
        {"states": pmon_states}, "build_audit (Temperature/Fan/PSU)")

    # graceful_drain / auto_heal (ops)
    try:
        drain_on = ("drain" in _scen_state) or ("drain" in active)
    except Exception:
        drain_on = False
    add("graceful_drain", "Graceful drain (RFC 8326)",
        "active" if drain_on else "idle", "OPS",
        "Hitless spine drain via BGP graceful-shutdown — 0 packet loss.",
        "no drain in progress", {"active": drain_on}, "_scen_state")
    try:
        agent_on = bool(_agent["enabled"])
        drained = sorted(_agent_drained())
    except Exception:
        agent_on, drained = False, []
    add("auto_heal", "Thermal auto-heal agent",
        "active" if agent_on else "idle", "OPS",
        "Autonomous agent drains a spine at 72C, watches it cool, restores it.",
        "auto-heal agent disabled", {"enabled": agent_on, "drained": drained}, "_agent")

    summ = {"active": 0, "idle": 0, "real_but_zero": 0, "absent": 0}
    for f in feats:
        key = f["status"].replace("-", "_")
        if key in summ:
            summ[key] += 1
    return {"generated": generated, "features": feats, "summary": summ}


# ─── formatting helpers ─────────────────────────────────────────────────────────
def _numv(v, d=0.0):
    """Coerce a possibly-ABSENT (None) metric to a number for prose/formatting.
    Used ONLY where a sentence needs a figure — never in an aggregate, a baseline
    or an agent decision, all of which must SKIP absent inputs instead."""
    return d if v is None else v


def _fmt_bps(bps):
    bps = float(bps or 0)
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if bps < 1000:
            return "%.1f %s" % (bps, unit)
        bps /= 1000.0
    return "%.1f Tbps" % bps


def _sev(asic_c):
    if asic_c is None:
        return "ABSENT"
    if asic_c >= ASIC_CRIT:
        return "CRITICAL"
    if asic_c >= ASIC_WARN:
        return "warn"
    return "healthy"


# ─── OFFLINE responder (data-driven, no network) ─────────────────────────────────
def offline_answer(question, ctx):
    q = (question or "").strip().lower()
    switches = ctx.get("switches", {})
    hottest = ctx.get("hottest", {}) or {}
    hot_name = hottest.get("name")
    hot_c = hottest.get("asic_c", 0.0)
    # "fabric throughput" the copilot cites = one-directional OFFERED LOAD (host-edge
    # ingress), consistent with the headline + per-flow numbers. Fall back to the
    # per-switch tx sum only if offered_bps is unavailable.
    total = ctx.get("offered_bps")
    if total is None:
        total = ctx.get("total_tx_bps", 0.0)
    notable = ctx.get("notable", [])
    anomalies = ctx.get("anomalies", []) or []

    if not switches:
        return ("I'm not seeing any fabric metrics yet — %s. Once the exporter and "
                "Prometheus are up and traffic is flowing, ask me again."
                % ctx.get("note", "waiting for data"))

    def find_switch():
        for n in switches:
            if n.lower() in q:
                return n
        return None

    # anomaly / efficiency questions — explain the power-per-Gbps residual + the fix
    if ("anomal" in q or "efficien" in q or "per gbps" in q or "per-gbps" in q
            or "watts per" in q or "energy cost" in q or "wasting" in q):
        if not anomalies:
            return ("No power-efficiency anomalies right now — every switch is within its "
                    "watts-per-Gbps baseline (power tracking useful throughput as expected). "
                    "I watch each switch's power/Gbps against an EWMA baseline and flag a "
                    "spine that's drained-but-drawing or a leaf whose energy/bit spikes under "
                    "congestion.")
        a = anomalies[0]
        act = a.get("action", {}) or {}
        return ("%s %s Recommended: %s — run the '%s' scenario to shift traffic and bring "
                "its energy-per-bit back to baseline."
                % (a.get("level", "warn").upper(), a.get("reason", ""),
                   act.get("label", "graceful-drain a spine"), act.get("scenario", "drain")))

    # greeting
    if any(g == q or q.startswith(g) for g in ("hi", "hello", "hey", "yo")) and len(q) < 12:
        return ("Hi — I'm the Energy Lab assistant. The fabric is pushing %s right now; "
                "hottest ASIC is %s at %.0fC. Ask me what's happening, why a switch is hot, "
                "or what you should do." % (_fmt_bps(total), hot_name, hot_c))

    # "why is <switch> hot" / "why ... hot"
    if "why" in q and ("hot" in q or "warm" in q or "temp" in q or "heat" in q):
        target = find_switch() or hot_name
        s = switches.get(target, {})
        if s.get("asic_c") is None:
            why = (s.get("absent_why") or {}).get("asic_c", "no sensor on this platform")
            return ("%s has NO ASIC temperature reading — it is ABSENT, not 0 °C: %s. "
                    "I will not guess a number for it." % (target, why))
        asic = s.get("asic_c")
        sev = _sev(asic)
        line = ("%s's modeled ASIC is at %.0fC (%s; warn %.0f / critical %.0f). "
                "That heat tracks its traffic: it's running at intensity %.2f (0-1) carrying "
                "%s of tx." % (target, asic, sev, ASIC_WARN, ASIC_CRIT,
                               _numv(s.get("intensity")), _fmt_bps(_numv(s.get("tx_bps")))))
        line += (" On this lab the ASIC temperature is MODELED from the real measured traffic "
                 "(a virtual switch has no physical sensor), so the more this node forwards, "
                 "the higher the modeled junction temp climbs.")
        if _numv(s.get("drops_ps")) > 0.5:
            line += " It's also dropping ~%.1f tx pkt/s — a sign of congestion." % s["drops_ps"]
        return line

    # "is this normal" / "is this ok"
    if ("normal" in q) or ("is this ok" in q) or ("is it ok" in q) or ("healthy" in q):
        sev = _sev(hot_c)
        if sev == "healthy":
            return ("Yes — this looks healthy. Hottest ASIC is %s at %.0fC, comfortably below the "
                    "%.0fC warn line. Fabric throughput is %s and I see no drops worth flagging."
                    % (hot_name, hot_c, ASIC_WARN, _fmt_bps(total)))
        if sev == "warn":
            return ("Borderline. %s is at %.0fC, over the %.0fC warn threshold but under the %.0fC "
                    "critical line — worth watching. It's the traffic driving it (fabric total %s)."
                    % (hot_name, hot_c, ASIC_WARN, ASIC_CRIT, _fmt_bps(total)))
        return ("Not really — %s has crossed the %.0fC CRITICAL threshold at %.0fC. That's the load "
                "concentrating on this node. I'd move traffic off it (see 'what would you do')."
                % (hot_name, ASIC_CRIT, hot_c))

    # "what would you do" / recommend
    if ("what would you do" in q) or ("recommend" in q) or ("suggest" in q) or ("should i" in q) or ("mitigat" in q) or ("fix" in q):
        cooler = None
        cool_c = 1e9
        for n, s in switches.items():
            if n != hot_name and s.get("asic_c") is not None and s["asic_c"] < cool_c:
                cool_c = s["asic_c"]
                cooler = n
        rec = ("The load is concentrated on %s (%.0fC, intensity %.2f). I'd rebalance traffic onto "
               "the cooler path" % (hot_name, hot_c,
                                    _numv(switches.get(hot_name, {}).get("intensity"))))
        if cooler:
            rec += " — e.g. %s is only %.0fC and has ECMP headroom (%d FIB next-hops)." % (
                cooler, cool_c, switches.get(cooler, {}).get("fib_vias", 0))
        else:
            rec += "."
        rec += (" Concretely: shift a flow off %s (adjust ECMP hashing / drain a leaf uplink) and the "
                "modeled temp should relax back down. A future auto-agent could do this rebalancing "
                "automatically when a node crosses %.0fC." % (hot_name, ASIC_WARN))
        return rec

    # "what is <metric>"
    if q.startswith("what is") or q.startswith("what's ") or ("explain" in q) or ("define" in q):
        if "ecn" in q:
            return ("ECN (Explicit Congestion Notification) marks packets instead of dropping them when "
                    "a switch queue fills — sonic_ecn_marked_packets_total counts those marks. Rising ECN "
                    "means queues are backing up under load. It's modeled here above 65%% intensity.")
        if "power" in q or "watt" in q or "psu" in q:
            return ("Power is the modeled PSU output in watts (sonic_psu_output_power_watts), 180-400W per "
                    "switch scaling with load. Right now the fabric's hottest node %s draws ~%.0fW."
                    % (hot_name, _numv(switches.get(hot_name, {}).get("power_w"))))
        if "bgp" in q:
            return ("fabric_bgp_established is the count of BGP peers in Established state per switch — the "
                    "control-plane health signal. All peers up means the fabric's routing is converged.")
        if "ecmp" in q or "fib" in q or "via" in q:
            return ("fabric_fib_vias counts the 'via' next-hops in each switch's kernel FIB — a proxy for "
                    "ECMP richness. More next-hops means more equal-cost paths to spread traffic (and heat) "
                    "across, which is exactly what you want under load.")
        if "pfc" in q or "pause" in q:
            return ("PFC (Priority Flow Control) pause frames tell an upstream port to stop sending briefly "
                    "to avoid drops; sonic_pfc_rx_pause_frames_total counts them. A rising count signals the "
                    "fabric is applying backpressure under congestion.")
        if "wred" in q:
            return ("WRED (Weighted Random Early Detection) proactively drops packets from filling queues to "
                    "avoid tail-drop; sonic_queue_wred_dropped_packets_total counts those drops under load.")
        if "temp" in q or "asic" in q or "heat" in q:
            return ("ASIC temperature is the modeled switch-chip junction temp in Celsius, derived from real "
                    "measured traffic (warn %.0fC, critical %.0fC). Hottest right now: %s at %.0fC."
                    % (ASIC_WARN, ASIC_CRIT, hot_name, hot_c))
        if "intensity" in q:
            return ("Intensity is a 0-1 load index per switch: measured aggregate bits/s divided by a reference "
                    "rate, floored by a gentle idle cycle. It's what drives the modeled heat and power.")
        # fall through to status

    # default / "what's happening" / status
    parts = []
    parts.append("Fabric throughput is %s across %d switches." % (_fmt_bps(total), len(switches)))
    if hot_name:
        parts.append("Hottest is %s at %.0fC (%s)." % (hot_name, hot_c, _sev(hot_c)))
    if notable:
        parts.append("Notable: " + "; ".join(notable[:4]) + ".")
    else:
        parts.append("Nothing alarming — all ASICs below the %.0fC warn line." % ASIC_WARN)
    if anomalies:
        a = anomalies[0]
        parts.append("⚠ Power-efficiency anomaly: %s" % a.get("reason", ""))
    # per-switch one-liner
    busy = sorted(switches.items(),
                  key=lambda kv: _numv(kv[1].get("intensity")), reverse=True)
    if busy:
        n, s = busy[0]
        parts.append("Busiest node %s: %s tx, intensity %.2f, %dW, %d BGP peers up." % (
            n, _fmt_bps(_numv(s.get("tx_bps"))), _numv(s.get("intensity")),
            _numv(s.get("power_w")), s.get("bgp_peers", 0)))
    return " ".join(parts)


# ─── LLM brains ──────────────────────────────────────────────────────────────
def _http_post_json(url, headers, body, timeout=25):
    data = json.dumps(body).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # urllib's HTTPError stringifies to just "HTTP Error 400: Bad Request" and
        # DISCARDS the body — which is where the provider says what was actually
        # wrong. That cost real debugging time once (a rejected request argument
        # looked like a quota problem), so read the body and put it in the error.
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            detail = ""
        raise RuntimeError("HTTP %s: %s" % (e.code, detail or e.reason)) from None


def _sys_with_ctx(ctx):
    """System prompt with the live context folded in — keeps the user/assistant
    turns strictly alternating (Anthropic 400s on two consecutive user turns)."""
    return SYSTEM_PROMPT + "\n\nLIVE FABRIC CONTEXT (JSON):\n" + json.dumps(ctx)


def _conv(question, history):
    """Conversation turns only (alternating user/assistant); context lives in system."""
    msgs = []
    # Only the last 10 turns, each cut to 2,000 characters: a model bills every token,
    # and nothing else stops a request from sending a huge history.
    for h in (history if isinstance(history, list) else [])[-10:]:
        if not isinstance(h, dict):
            continue
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": str(content)[:2000]})
    msgs.append({"role": "user", "content": str(question)[:4000]})
    return msgs


def brain_openai(question, ctx, history):
    base = os.environ.get("OPENAI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "")
    # OPENAI_MODEL may be a comma-separated FALLBACK CHAIN — free models rate-limit,
    # so we try each in order and use the first that returns real content.
    models = [m.strip() for m in os.environ.get("OPENAI_MODEL", "llama-3.3-70b-versatile").split(",") if m.strip()]
    msgs = [{"role": "system", "content": _sys_with_ctx(ctx)}] + _conv(question, history)
    # Gemini 3.x Flash "thinking" tokens count against max_tokens — a low cap made the
    # reasoning eat the whole budget, truncating (or emptying) the visible answer.
    # Give it real headroom and keep thinking light so tokens go to the answer.
    max_toks = int(os.environ.get("COPILOT_MAX_TOKENS", "1500"))
    reff = os.environ.get("COPILOT_REASONING_EFFORT", "low").strip().lower()
    retries = int(os.environ.get("COPILOT_RETRIES", "3"))   # retry transient 503/overload
    tmo = float(os.environ.get("COPILOT_TIMEOUT", "40"))     # Gemini thinking can be slow → generous
    last = None
    for model in models:
        body = {"model": model, "messages": msgs, "max_tokens": max_toks, "temperature": 0.3}
        # reasoning_effort is only understood by reasoning models. Azure OpenAI 400s on
        # it for gpt-4.1, which broke the chat completely while the metrics kept
        # working. Rather than maintain a list of which models accept which argument,
        # send it and drop it if the provider says it does not know it.
        if reff and reff not in ("off", "none", "default"):
            body["reasoning_effort"] = reff
        for attempt in range(retries + 1):
            try:
                resp = _http_post_json(base + "/chat/completions",
                                       {"Authorization": "Bearer " + key}, body, timeout=tmo)
            except Exception as e:
                es = str(e); low = es.lower(); last = "%s: %s" % (model, es)
                # provider rejected an optional argument → drop it and try again now
                if ("reasoning_effort" in es and
                        any(t in low for t in ("unrecognized", "unsupported", "unknown",
                                               "not supported", "extra_forbidden"))):
                    body.pop("reasoning_effort", None)
                    print("brain openai: %s rejects reasoning_effort — retrying without it"
                          % model, file=sys.stderr)
                    continue
                # transient overload / rate-limit → wait and retry the SAME model
                if attempt < retries and any(t in es for t in
                        ("503", "429", "UNAVAILABLE", "overloaded", "high demand", "RESOURCE_EXHAUSTED")):
                    time.sleep(1.2 * (attempt + 1)); continue
                break
            if isinstance(resp, list) or "choices" not in resp:
                err = (resp[0] if isinstance(resp, list) and resp else resp)
                last = "%s: %s" % (model, json.dumps((err.get("error", err) if isinstance(err, dict) else err))[:140])
                break
            content = ((resp["choices"][0] or {}).get("message", {}) or {}).get("content", "") or ""
            if content.strip():
                print("brain openai: answered via %s (attempt %d)" % (model, attempt + 1), file=sys.stderr)
                return content.strip()
            last = "%s: empty content" % model
            break
    # Do NOT name a cause here. This wrapper used to read "all models
    # rate-limited/failed", and the caller classifies by substring — so every
    # failure, whatever it was, got reported to the user as a quota problem.
    raise RuntimeError("no model answered (%s)" % last)


def brain_azure_anthropic(question, ctx, history):
    endpoint = os.environ.get("AZURE_ANTHROPIC_ENDPOINT", "").rstrip("/")
    token = os.environ.get("AZURE_ANTHROPIC_TOKEN", "")
    deployment = os.environ.get("AZURE_ANTHROPIC_DEPLOYMENT", "")
    # Anthropic Messages format: 'system' is a TOP-LEVEL field, NOT a message.
    body = {
        "model": deployment,
        "max_tokens": 400,
        "system": _sys_with_ctx(ctx),
        "messages": _conv(question, history),
    }
    resp = _http_post_json(
        endpoint + "/anthropic/v1/messages",
        {"Authorization": "Bearer " + token, "anthropic-version": "2023-06-01"},
        body)
    # content is a list of blocks; concatenate text blocks
    out = []
    for block in resp.get("content", []):
        if block.get("type") == "text":
            out.append(block.get("text", ""))
    text = "".join(out).strip()
    if not text:
        raise RuntimeError("empty azure-anthropic response")
    return text


def with_wiring(ctx):
    """Add the live link rates to a context, for the chat only.

    build_topology() is the one place that turns per-port Prometheus rates into
    per-link bps and marks cut/failed links down. Reusing it here means the
    copilot and the topology map can never quote different numbers for the same
    link. It cannot live in build_context() because build_topology() calls that.
    Best-effort: a failure just leaves the chat with no link detail, exactly as
    before, rather than taking the chat down.
    """
    try:
        topo = build_topology(ctx=ctx, probe=False)
        ctx["links"] = [
            {"a": l["a"], "b": l["b"], "a_port": l.get("ap"), "b_port": l.get("bp"),
             "bps": l.get("bps", 0.0), "down": bool(l.get("down")),
             "down_reason": l.get("down_reason"),
             "carrying_traffic": bool(l.get("active"))}
            for l in topo.get("links", [])]
        # Busiest first. Asked for "the top three", the model quoted the right
        # three links but in the wrong order — it should not have to sort.
        ctx["links"].sort(key=lambda l: l["bps"], reverse=True)

        # ── WHAT IS ACTUALLY RUNNING ─────────────────────────────────────────
        # The copilot could see every number but not a single scenario, so when a
        # user asked where the traffic was coming from it guessed "probably the
        # GPU collectives" — while the GPU pod sat idle and the real source was
        # the leaves pinging each other. It also could not explain the three
        # source arrows on the map, because the incast is CONFIGURED but not
        # RUNNING and nothing told it the map draws those routes when idle.
        ov = topo.get("overload") or {}
        amb = topo.get("ambient") or {}
        coll = topo.get("collective") or {}
        trf = topo.get("traffic") or {}
        gpus = [n["id"] for n in TOPOLOGY_NODES if n.get("kind") == "gpu"]
        ctx["activity"] = {
            "ambient_baseline": {
                "running": bool(amb.get("running")),
                "what_it_is": ("the always-on background hum. It is REAL traffic, but it is "
                               "generated BY THE LEAF SWITCHES THEMSELVES — ping streams between "
                               "leaf loopback addresses (pairs of non-adjacent leaves, each "
                               "direction its own stream, ~23 kbps each). The pairs are "
                               "deliberately non-adjacent so their paths cross and ECMP hashes "
                               "them onto different spines. NO HOST SENDS ANY OF IT."),
            },
            "incast_overload": {
                "running": bool(ov.get("running")),
                "configured": bool(ov.get("configured")),
                "sources": ov.get("src_hosts") or ([ov["src_host"]] if ov.get("src_host") else []),
                "destination_ip": ov.get("dst_ip"),
                "what_it_is": ("the N-to-1 incast demo, started with the 'Start incast' button. "
                               "IMPORTANT: the map draws these source->destination routes AT ALL "
                               "TIMES, even while it is not running, so the direction of the demo "
                               "is legible when idle. Arrows on the map therefore do NOT mean "
                               "traffic is flowing — check 'running'."),
            },
            "scenario_traffic": {"running": bool(trf.get("running")),
                                 "pattern": trf.get("pattern"), "count": trf.get("count", 0)},
            "gpu_pod": {
                "present": bool(gpus), "nodes": gpus,
                "collective_running": bool(coll.get("scen")),
                "scenario": coll.get("scen"),
                "note": ("The GPU pod EXISTS in this fabric but runs a collective only while "
                         "'scenario' is set. If collective_running is false it is idle and is "
                         "NOT the source of any traffic — never attribute traffic to it then."),
            },
            "detected_flows": topo.get("flows") or [],
        }
        ctx["units"]["total_tx_bps"] = (
            "sum of PER-SWITCH transmit rates. A packet crossing leaf then spine is counted "
            "at BOTH, so this is roughly twice the one-directional traffic actually offered. "
            "It is not a host-to-host throughput figure.")
    except Exception as e:
        print("with_wiring: %s" % e, file=sys.stderr)
    return ctx


def answer(question, ctx, history):
    """Route to the configured brain; on ANY error fall back to offline."""
    try:
        if BRAIN == "openai":
            return brain_openai(question, ctx, history)
        if BRAIN == "azure-anthropic":
            return brain_azure_anthropic(question, ctx, history)
        # default offline — no network
        return offline_answer(question, ctx)
    except Exception as e:
        es = str(e); low = es.lower()
        print("brain '%s' error: %s" % (BRAIN, es), file=sys.stderr)
        # A real LLM was configured but unreachable → be DIRECT about WHY, don't substitute an
        # answer from the offline rules-engine (that read like a real reply and confused).
        if BRAIN in ("openai", "azure-anthropic"):
            # Match on whole words, not substrings — "rate" is inside "generate" and
            # "generated", so the old check fired on unrelated errors and reported a
            # quota problem for a request the provider had simply rejected.
            def has(*words):
                return any(re.search(r"\b%s\b" % w, low) for w in words)
            if has("429", "quota", "rate_limit", "rate", "resource_exhausted", "tpm"):
                why = "the model is **rate-limited / out of quota** (check the key's plan/billing)"
            elif has("503", "502", "504", "unavailable", "timeout") or "timed out" in low:
                why = "the **model server isn't responding** (down or timed out)"
            elif has("401", "403", "permission", "denied") or "api key" in low:
                why = "the **API key was rejected**"
            elif has("400", "404", "unrecognized", "unsupported", "invalid_request_error"):
                why = "the **request was rejected by the model API** (400/404 — a bad argument, or a deployment name that does not exist)"
            else:
                why = "the **model couldn't be reached**"
            # Always show the provider's own words. Guessing a cause and hiding the
            # real one is what made the last outage take an hour to diagnose.
            return ("⚠️ **Copilot LLM is offline** — %s.\n\nProvider said: `%s`\n\n"
                    "The dashboard metrics are still live." % (why, es[:300]))
        # BRAIN=offline was an intentional choice (no LLM configured) → offline brain IS the answer
        return offline_answer(question, ctx)


# ═══════════════════════════════════════════════════════════════════════════════
#  CONFIGURE_SH — the idempotent generated configure-fabric.sh. Re-running it
#  restores a clean fabric (used by restores + the reset button).
# ═══════════════════════════════════════════════════════════════════════════════
def _configure_sh_path():
    p = os.environ.get("CONFIGURE_SH", "").strip()
    if p:
        return p
    return os.path.join(os.path.dirname(os.path.abspath(FABRIC_PATH)), "configure-fabric.sh")


# CONFIGURE_SH is (re)set by reload_fabric().

# During a bulk reset we tear down every scenario THEN run configure once at the
# end — this flag lets the per-scenario stops defer their own configure call.
_suppress_configure = False


def _run_configure():
    """Run configure-fabric.sh (idempotent) to restore a clean fabric.
    Runs it where the docker daemon lives (locally, or inside the Lima VM)."""
    if _suppress_configure:
        return True, "restore deferred"
    if EXEC.get("mode") == "ssh":
        # ssh fabric has no configure-fabric.sh; each scenario owns its reversible
        # SSH undo, so a clean restore here is a no-op success.
        return True, "clean fabric restored (per-scenario SSH undo; no configure-fabric.sh in ssh mode)"
    if EXEC.get("mode") == "limactl":
        vm = EXEC.get("vm", "sonic")
        cmd = ["limactl", "shell", vm, "--", "sudo", "bash", CONFIGURE_SH]
    else:
        if not os.path.exists(CONFIGURE_SH):
            return False, "configure-fabric.sh not found at %s (set CONFIGURE_SH=...)" % CONFIGURE_SH
        cmd = ["bash", CONFIGURE_SH]
    rc, out, err = _run(cmd, timeout=300)
    if rc != 0:
        return False, "configure-fabric.sh rc=%d (%s)" % (rc, (err or out).strip()[-140:])
    return True, "clean fabric restored via configure-fabric.sh"


# ═══════════════════════════════════════════════════════════════════════════════
#  Live target selection — every scenario picks its target from REAL topology +
#  REAL live rates, never a hardcoded 'spine1'/'Ethernet0'.
# ═══════════════════════════════════════════════════════════════════════════════
def _port_rates():
    """{(switch,port): bps} for tx and rx, one-directional. Never raises."""
    try:
        tx = _as_pair_map(_prom_query("rate(fabric_port_tx_bytes[30s])*8"))
        rx = _as_pair_map(_prom_query("rate(fabric_port_rx_bytes[30s])*8"))
    except Exception:
        tx, rx = {}, {}
    return tx, rx


def _busiest_uplink(exclude=()):
    """(leaf, port, spine, bps) for the busiest leaf→spine uplink by one-directional
    rate, skipping any (leaf,port) in `exclude`. Falls back to LEAVES[0]'s first
    uplink when there is no live traffic. None only if the fabric has no uplinks."""
    tx, rx = _port_rates()

    def orate(sw, p):
        return max(tx.get((sw, p), 0.0), rx.get((sw, p), 0.0))

    best = None
    for leaf in LEAVES:
        for port, spine in UPLINKS.get(leaf, []):
            if (leaf, port) in exclude:
                continue
            b = orate(leaf, port)
            if best is None or b > best[3]:
                best = (leaf, port, spine, b)
    if best is not None:
        return best
    # no live traffic → deterministic fallback (still topology-derived)
    for leaf in LEAVES:
        for port, spine in UPLINKS.get(leaf, []):
            if (leaf, port) not in exclude:
                return (leaf, port, spine, 0.0)
    return None


def _busiest_spine(exclude=()):
    """The spine carrying the most traffic right now (the ECMP-active one), skipping
    `exclude`. Falls back to the first eligible spine when the fabric is idle."""
    tx, rx = _port_rates()
    tot = {}
    for (sw, _p), v in tx.items():
        if sw in SPINES:
            tot[sw] = tot.get(sw, 0.0) + v
    for (sw, _p), v in rx.items():
        if sw in SPINES:
            tot[sw] = tot.get(sw, 0.0) + v
    best = None
    for s in SPINES:
        if s in exclude:
            continue
        v = tot.get(s, 0.0)
        if best is None or v > best[1]:
            best = (s, v)
    return best[0] if best else None


def _bgp_neighbors(node):
    """Discover a switch's BGP peer IPs from FRR (for graceful-drain fallback and
    the bad-config break). Returns [] if unavailable."""
    rc, out, _err = _run(_cnode(node, "vtysh", "-c", "show bgp summary json"))
    peers = []
    if rc == 0 and out.strip():
        try:
            j = json.loads(out)
            for af in j.values():
                if isinstance(af, dict) and isinstance(af.get("peers"), dict):
                    peers.extend(af["peers"].keys())
        except Exception:
            pass
    return sorted(set(peers))


# ═══════════════════════════════════════════════════════════════════════════════
#  Graceful drain (RFC 8326) — shared by scenario 5 AND the ACT agent, so both
#  drain a spine the exact same reversible way.
# ═══════════════════════════════════════════════════════════════════════════════
def _spine_deprefer_fallback(spine, on):
    """FALLBACK when 'bgp graceful-shutdown' is unsupported: prepend own ASN 5×
    on all outbound so neighbors deprefer this spine. Fully reversible."""
    asn = ASN.get(spine)
    peers = _bgp_neighbors(spine)
    if not asn or not peers:
        return False, "graceful-shutdown unsupported and no BGP peers found to deprefer"
    rm = "LAB_DRAIN"
    cmds = ["configure terminal"]
    if on:
        cmds += ["route-map %s permit 10" % rm,
                 "set as-path prepend %s" % " ".join([str(asn)] * 5),
                 "exit", "router bgp %s" % asn]
        cmds += ["neighbor %s route-map %s out" % (p, rm) for p in peers]
    else:
        cmds += ["router bgp %s" % asn]
        cmds += ["no neighbor %s route-map %s out" % (p, rm) for p in peers]
        cmds += ["exit", "no route-map %s" % rm]
    argv = ["vtysh"]
    for c in cmds:
        argv += ["-c", c]
    rc, out, err = _run(_cnode(spine, *argv))
    if rc != 0:
        return False, "deprefer fallback failed (%s)" % (err or out).strip()[:80]
    return True, ("as-path-prepend ×5 applied" if on else "as-path-prepend removed")


def _spine_graceful(spine, on):
    """Engage/clear RFC 8326 graceful-shutdown on <spine>. Prefers the real knob;
    detects rc / 'unknown command' and falls back to ASN prepend. Returns (ok,msg)."""
    asn = ASN.get(spine)
    if not asn:
        return False, "no ASN known for %s" % spine
    verb = "bgp graceful-shutdown" if on else "no bgp graceful-shutdown"
    rc, out, err = _run(_cnode(spine, "vtysh",
                               "-c", "configure terminal",
                               "-c", "router bgp %s" % asn,
                               "-c", verb))
    combined = (out + err).lower()
    if rc == 0 and "unknown" not in combined and "% " not in combined:
        return True, "graceful-shutdown " + ("engaged (RFC 8326)" if on else "cleared")
    return _spine_deprefer_fallback(spine, on)


# ═══════════════════════════════════════════════════════════════════════════════
#  THE ACT AGENT — a boot-time daemon thread: sense → think → ACT → verify.
#  Default OFF. Drains a spine that hits CRITICAL, watches it cool, restores it.
#  Never isolates the fabric; hysteresis + cooldown; exception-safe forever.
# ═══════════════════════════════════════════════════════════════════════════════
AGENT_CRIT = ASIC_CRIT      # 72C — drain trigger
AGENT_HYST = 58.0           # cooled below this (sustained) → restore
AGENT_SUSTAIN = 6.0         # seconds the spine must stay cool before restore
AGENT_COOLDOWN = 10.0       # min seconds between agent actions
AGENT_PERIOD = 4.0          # loop cadence

_agent = {
    "enabled": False,
    "drained": set(),        # spines the agent itself drained
    "decisions": [],         # [{t, level, msg}] — level info|act|warn|skip
    "last_action": 0.0,
    "cool_since": {},        # spine -> epoch it first dipped below hysteresis
    # ── SILENT-ZERO GUARD state ──────────────────────────────────────────────
    # Nodes whose REQUIRED input (ASIC temperature) is unavailable. On a real
    # Z9100 the sensor is genuinely ABSENT; publishing 0.0 for it would make
    # 'temp < AGENT_CRIT' true forever and the agent would never fire, never
    # log and never error. So: absent input => SKIP the node, RECORD why, and
    # raise a health signal the UI can show.
    "blind": {},             # spine -> reason string
    "skips": {},             # spine -> {"reason","since","count","kind"}
    "armed_at": 0.0,
    "last_blind_log": {},    # spine -> epoch of the last blind warning (rate limit)
    "ticks": 0,
    "last_tick": 0.0,
}
# RLock (not Lock): _agent_log and the health helpers are called from inside
# locked sections, and every mutation of drained/cool_since/blind now holds it.
_agent_lock = threading.RLock()
AGENT_BLIND_RELOG = 300.0    # seconds between repeat "input unavailable" warnings


def _agent_log(level, msg):
    with _agent_lock:
        _agent["decisions"].append({"t": int(time.time()), "level": level, "msg": msg})
        if len(_agent["decisions"]) > 400:
            _agent["decisions"] = _agent["decisions"][-400:]
    print("[agent:%s] %s" % (level, msg), file=sys.stderr)


def _agent_drained():
    """Snapshot of the agent-drained set, taken under the lock."""
    with _agent_lock:
        return set(_agent["drained"])


def _agent_health():
    """The visible health signal: can this agent actually do its job right now?
    'blind' means its required input is unavailable on EVERY node it watches —
    the failure mode that used to be completely silent."""
    with _agent_lock:
        observed = dict(_agent["blind"])
        enabled = _agent["enabled"]
        skips = {k: dict(v) for k, v in _agent["skips"].items()}
    watched = list(SPINES)
    # DECLARED blindness counts even before the agent has ever ticked: if the
    # platform manifest says this node has no ASIC sensor, the agent is blind on
    # it the moment it is bound — the UI must be able to say so up front rather
    # than after the demo has silently failed.
    blind = {}
    for sp in watched:
        why = declared_absent(sp).get("asic_c")
        if why:
            blind[sp] = why
    blind.update(observed)
    usable = [x for x in watched if x not in blind]
    if not watched:
        state, msg = "idle", "no spines in this fabric to watch"
    elif not usable:
        state = "blind"
        msg = ("thermal agent is BLIND — ASIC temperature is unavailable on all %d "
               "watched spine(s); it cannot act and will not pretend to" % len(watched))
    elif blind:
        state = "degraded"
        msg = ("thermal agent running on %d of %d spine(s) — %s skipped (input "
               "unavailable)" % (len(usable), len(watched), ", ".join(sorted(blind))))
    else:
        state, msg = "ok", "thermal agent has a live temperature signal on all %d spine(s)" % len(watched)
    return {
        "state": state,                       # ok | degraded | blind | idle
        "ok": state in ("ok", "idle"),
        "armed": enabled,
        "msg": msg,
        "watched": watched,
        "usable": usable,
        "blind": blind,                       # {node: why}
        "skipped": skips,                     # {node: {reason, since, count, kind}}
        "required_input": "thermal.asic_c",
    }


def _agent_note_blind(sp, reason, kind="absent"):
    """Record (and rate-limit-log) that a watched node was SKIPPED this tick."""
    now = time.time()
    with _agent_lock:
        first = sp not in _agent["blind"]
        prev = _agent["skips"].get(sp) or {}
        _agent["blind"][sp] = reason
        _agent["skips"][sp] = {"reason": reason, "kind": kind,
                               "since": prev.get("since", now),
                               "count": int(prev.get("count", 0)) + 1}
        last = _agent["last_blind_log"].get(sp, 0.0)
        due = first or (now - last >= AGENT_BLIND_RELOG)
        if due:
            _agent["last_blind_log"][sp] = now
    if due:
        _agent_log("warn", "SKIPPED %s — required input thermal.asic_c is %s: %s. "
                           "No decision was made for this node (it is NOT 0 °C)."
                   % (sp, kind.upper(), reason))


def _agent_note_signal(sp):
    """The node's input came back (or was fine all along)."""
    with _agent_lock:
        had = sp in _agent["blind"]
        _agent["blind"].pop(sp, None)
        _agent["skips"].pop(sp, None)
        _agent["last_blind_log"].pop(sp, None)
    if had:
        _agent_log("info", "%s thermal signal RESTORED — the node is being watched again" % sp)


def _agent_set_enabled(on):
    """Toggle the agent. Disabling UN-DRAINS everything it drained.
    The decision log PERSISTS across arm/disarm — it is the audit trail of what
    the agent did and why, and clearing it destroyed exactly the evidence the
    demo is about. Arm/disarm are logged as markers instead."""
    on = bool(on)
    with _agent_lock:
        was = _agent["enabled"]
        _agent["enabled"] = on
        if on and not was:
            _agent["armed_at"] = time.time()
        to_restore = list(_agent["drained"]) if (was and not on) else []
    if on and not was:
        h = _agent_health()
        _agent_log("info", "auto-heal agent ENABLED — watching %d spine(s) for ≥%.0f°C"
                   % (len(SPINES), AGENT_CRIT))
        if h["state"] == "blind":
            _agent_log("warn", "ARMED BUT BLIND — %s" % h["msg"])
        elif h["state"] == "degraded":
            _agent_log("warn", h["msg"])
    for sp in to_restore:
        ok, msg = _spine_graceful(sp, False)
        _agent_log("info", "agent disabled — restoring %s to service (%s)" % (sp, msg))
    if was and not on:
        with _agent_lock:
            _agent["drained"].clear()
            _agent["cool_since"].clear()
            _agent["blind"].clear()
            _agent["skips"].clear()
            _agent["last_blind_log"].clear()
        _agent_log("info", "auto-heal agent DISABLED — all agent-drained spines restored")


def _agent_summary():
    with _agent_lock:
        enabled = _agent["enabled"]
        d = sorted(_agent["drained"])
    if not enabled:
        return "agent idle (disabled)"
    h = _agent_health()
    if h["state"] == "blind":
        return h["msg"]
    if d:
        base = "actively shedding heat — drained %s to a cooler path" % ", ".join(d)
    else:
        base = "watching %d spine(s); all within thermal limits" % len(h["usable"])
    if h["state"] == "degraded":
        base += " (%s skipped: input unavailable)" % ", ".join(sorted(h["blind"]))
    return base


def _spines_out_of_service():
    """Every spine NOT carrying traffic right now, across ALL owners: the agent's
    own drains, plus scenario-owned ones — the 'spinefail' kill and the manual
    'drain'. The agent uses this so the never-isolate guardrail counts EVERY
    out-of-service spine (not just its own), and so it never touches — or later
    mis-restores — a spine a manual scenario currently owns. Read at call time,
    so _scen_state being defined later in the module is fine."""
    out = _agent_drained()
    sf = _scen_state.get("spinefail")
    if sf and sf.get("spine"):
        out.add(sf["spine"])
    dr = _scen_state.get("drain")
    if dr and dr.get("spine"):
        out.add(dr["spine"])
    return out


def _agent_tick():
    """One sense→think→act→verify cycle. Only ever called from the daemon thread."""
    with _agent_lock:
        if not _agent["enabled"]:
            return
    ctx = build_context()
    switches = ctx.get("switches", {})
    now = time.time()
    with _agent_lock:
        _agent["ticks"] += 1
        _agent["last_tick"] = now

    # ── SENSE, with the silent-zero guard ────────────────────────────────────
    # temps holds ONLY nodes with a genuine reading. A node whose ASIC sensor is
    # ABSENT (real Z9100) or that has no telemetry at all is SKIPPED and recorded
    # — never treated as 0 °C, which would read as "cool" forever.
    temps = {}
    for sp in SPINES:
        rec = switches.get(sp)
        if rec is None:
            _agent_note_blind(sp, "no telemetry for this node in the current sample "
                                  "(the collector has not reported it)", kind="no-data")
            continue
        t = rec.get("asic_c")
        if t is None:
            why = (rec.get("absent_why") or {}).get(
                "asic_c", "this platform does not expose an ASIC temperature sensor")
            _agent_note_blind(sp, why, kind="absent")
            continue
        _agent_note_signal(sp)
        temps[sp] = t
    if SPINES and not temps:
        # every watched node is blind — say so once, then stand down for this tick
        return

    # ── verify + restore: agent-drained spines, once the fabric is calm (hysteresis) ──
    # A drained spine ALWAYS cools (it carries no traffic), so "this spine cooled" is NOT
    # a safe restore trigger by itself — restoring it while another spine is still hot
    # just flips the hotspot back and forth (drain A → B heats → restore A → drain B …).
    # Only restore when the whole fabric is calm: no LIVE (non-drained) spine is at/above
    # the warn threshold. That means the thermal event has actually passed.
    drained_now = _agent_drained()
    # 'live_hot' only counts nodes we can actually SEE — a blind node contributes
    # neither heat nor calm to the decision.
    live_hot = any(temps[x] >= ASIC_WARN for x in temps if x not in drained_now)
    for sp in sorted(drained_now):
        if sp not in temps:
            # drained and now blind: hold the drain, never auto-restore on a guess
            with _agent_lock:
                _agent["cool_since"].pop(sp, None)
            continue
        t = temps[sp]
        with _agent_lock:
            last_action = _agent["last_action"]
            since = _agent["cool_since"].get(sp)
        if 0 < t <= AGENT_HYST and not live_hot:
            if since is None:
                with _agent_lock:
                    _agent["cool_since"][sp] = now
            elif now - since >= AGENT_SUSTAIN and now - last_action >= AGENT_COOLDOWN:
                ok, msg = _spine_graceful(sp, False)
                with _agent_lock:
                    _agent["drained"].discard(sp)
                    _agent["cool_since"].pop(sp, None)
                    _agent["last_action"] = now
                _agent_log("info", "%s recovered to %.0f°C + fabric calm → restored to service (%s)" % (sp, t, msg))
        else:
            with _agent_lock:
                _agent["cool_since"].pop(sp, None)   # still hot somewhere / heated back up — reset timer

    # ── decide + act: drain any spine at CRITICAL, but NEVER isolate the fabric ──
    # "out of service" = agent-drained ∪ scenario-owned (spinefail kill / manual
    # drain). Counting ALL owners keeps the never-isolate guardrail correct even
    # when a manual scenario has already taken a spine down, and keeps the agent's
    # hands off a spine a scenario owns (so it can't later mis-restore it).
    out_of_service = _spines_out_of_service()
    drained_now = _agent_drained()
    scen_owned = out_of_service - drained_now
    for sp in SPINES:
        if sp in drained_now:
            continue
        if sp in scen_owned:          # a manual scenario owns this spine — hands off
            continue
        if sp not in temps:           # ABSENT/no-data input — already recorded, SKIP
            continue
        t = temps[sp]
        if t < AGENT_CRIT:
            continue
        # spines that would STILL be live if we drained sp — across every owner
        healthy_remaining = [x for x in SPINES if x != sp and x not in out_of_service]
        if len(healthy_remaining) < 1:
            _agent_log("warn", "%s hot at %.0f°C but it's the LAST live path — holding, alerting instead" % (sp, t))
            continue
        with _agent_lock:
            cooling = now - _agent["last_action"] < AGENT_COOLDOWN
            still_on = _agent["enabled"]
        if cooling:
            continue
        if not still_on:              # a disable landed mid-tick — stand down cleanly
            return
        ok, msg = _spine_graceful(sp, True)
        if ok:
            with _agent_lock:
                _agent["drained"].add(sp)
                _agent["last_action"] = now
            _agent_log("act", "%s hit %.0f°C CRITICAL → graceful-drain to cooler path (%s)" % (sp, t, msg))
        else:
            _agent_log("warn", "%s hit %.0f°C but drain FAILED: %s" % (sp, t, msg))


def _agent_loop():
    """Boot-time daemon. Exception-safe: a bad cycle is logged, never fatal."""
    while True:
        try:
            time.sleep(AGENT_PERIOD)
            _agent_tick()
        except Exception as e:
            try:
                _agent_log("warn", "agent loop error (continuing): %s" % e)
            except Exception:
                pass


def agent_state():
    """GET /api/agent payload. Decisions newest-first, last 40.

    The decision log PERSISTS across arm/disarm (it is the audit trail); clients
    that only want the current arming window filter on `armed_at`. ui.html already
    hides decisions while the agent is off, so nothing regresses there."""
    with _agent_lock:
        enabled = _agent["enabled"]
        drained = sorted(_agent["drained"])
        log = list(reversed(_agent["decisions"][-40:]))
        armed_at = _agent["armed_at"]
        total = len(_agent["decisions"])
    h = _agent_health()
    return {
        "enabled": enabled,
        "drained": drained,
        "decisions": log,
        "decisions_total": total,
        "armed_at": armed_at,
        # the visible health signal: 'blind' means the agent's required input is
        # unavailable and it is deliberately NOT acting.
        "health": h,
        "summary": _agent_summary(),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  SCENARIO REGISTRY — each is REVERSIBLE, targets are chosen from live topology,
#  every start/stop is guarded (timeouts, returns dicts, never wedges the fabric).
#  _scen_state[id] remembers a running scenario's chosen target so stop restores
#  the SAME resource. active-set is derived from concrete state (single truth).
# ═══════════════════════════════════════════════════════════════════════════════
_scen_state = {}
_active = set()          # only tracks state-less scenarios (thermalguard)
_scen_lock = threading.Lock()


def _reserved_uplinks(exclude_self=None):
    """(leaf,port) pairs already held by a link-level scenario, so cablecut and
    grayfail never collide on the same link."""
    res = set()
    for sid in ("cablecut", "grayfail"):
        if sid == exclude_self:
            continue
        st = _scen_state.get(sid)
        if st and st.get("leaf") is not None:
            res.add((st["leaf"], st["port"]))
    return res


# ── 1 · surge — reuse the existing overload flood (single source of truth) ──
def scen_surge_start():
    r = overload_control("start")
    return bool(r.get("ok")), r.get("msg", "")


def scen_surge_stop():
    r = overload_control("stop")
    return bool(r.get("ok")), r.get("msg", "")


# ── 2 · cablecut — shut the busiest leaf uplink; BGP self-heals (brief loss) ──
def scen_cablecut_start():
    tgt = _busiest_uplink(exclude=_reserved_uplinks(exclude_self="cablecut"))
    if not tgt:
        return False, "no leaf uplink available to cut (conflicts with gray failure?)"
    leaf, port, spine, _b = tgt
    rc, out, err = _run(_cnode(leaf, "config", "interface", "shutdown", port))
    if rc != 0:
        return False, "cable cut failed on %s %s (%s)" % (leaf, port, (err or out).strip()[:80])
    _scen_state["cablecut"] = {"leaf": leaf, "port": port, "spine": spine}
    return True, "cut %s %s (→ %s) — abrupt link loss; BGP detects it and reroutes (brief blip)" % (leaf, port, spine)


def scen_cablecut_stop():
    st = _scen_state.get("cablecut")
    if not st:
        return True, "cable cut not active"
    rc, out, err = _run(_cnode(st["leaf"], "config", "interface", "startup", st["port"]))
    _scen_state.pop("cablecut", None)
    if rc != 0:
        return False, "restore failed on %s %s (%s)" % (st["leaf"], st["port"], (err or out).strip()[:80])
    return True, "restored %s %s — BGP session re-establishing" % (st["leaf"], st["port"])


# ── 3 · grayfail — silent 2% loss via netem on a DIFFERENT uplink than cablecut ──
def scen_grayfail_start():
    tgt = _busiest_uplink(exclude=_reserved_uplinks(exclude_self="grayfail"))
    if not tgt:
        return False, "no free leaf uplink for gray failure (cable cut may hold the only one)"
    leaf, port, spine, _b = tgt
    # CAPABILITY GATE: netem also rides the netdev — a no-op on real silicon.
    ok_t, why_t = scenario_node_ok("grayfail", leaf)
    if not ok_t:
        return False, ("gray failure REFUSED on %s: %s. Nothing was executed — netem "
                       "on the host interface would not drop a single datapath packet."
                       % (leaf, why_t))
    rc, out, err = _run(_cnode(leaf, "tc", "qdisc", "add", "dev", port, "root", "netem", "loss", "2%"))
    if rc != 0:
        return False, "gray failure not applied — tc/netem unavailable on %s (%s)" % (leaf, (err or out).strip()[:80])
    _scen_state["grayfail"] = {"leaf": leaf, "port": port, "spine": spine}
    return True, "injected silent 2%% loss on %s %s (→ %s) — invisible to link/BGP state; needs active probing" % (leaf, port, spine)


def scen_grayfail_stop():
    st = _scen_state.get("grayfail")
    if not st:
        return True, "gray failure not active"
    _run(_cnode(st["leaf"], "tc", "qdisc", "del", "dev", st["port"], "root"))
    _scen_state.pop("grayfail", None)
    return True, "cleared netem loss on %s %s" % (st["leaf"], st["port"])


# ── 4 · spinefail — kill a whole spine; fabric survives on the rest ──
def scen_spinefail_start():
    if len(SPINES) < 2:
        return False, "refused — only %d spine(s); killing it would isolate the fabric" % len(SPINES)
    down = set(filter(None, [_scen_state.get("spinefail", {}).get("spine")]))
    exclude = down | _agent_drained() | set(filter(None, [_scen_state.get("drain", {}).get("spine")]))
    target = _busiest_spine(exclude=exclude) or next((s for s in SPINES if s not in exclude), None)
    if not target:
        return False, "no eligible spine to fail"
    if len([s for s in SPINES if s != target]) < 1:
        return False, "refused — would leave 0 spines"
    if EXEC.get("mode") == "ssh":
        # no container to kill on a real switch — hard-down every fabric port on the
        # spine, so all its BGP sessions drop and the leaves reroute to the other spine.
        ports = _spine_fabric_ports(target)
        if not ports:
            return False, "no fabric ports found on spine %s to down" % target
        remote = " ; ".join("sudo config interface shutdown %s" % p for p in ports)
        rc, out, err = _run(_node_ssh(target, remote), timeout=40)
        if rc != 0:
            return False, "could not down spine %s ports (%s)" % (target, (err or out).strip()[:80])
        _scen_state["spinefail"] = {"spine": target, "ports": ports}
        return True, "downed all %d fabric ports on spine %s — every leaf loses this spine; fabric survives on the other (blast radius 1/%d)" % (
            len(ports), target, len(SPINES))
    # Virtual lab: take the spine dark instead of killing its container. A killed container
    # loses the links containerlab wired at deploy time, and only root on the host could
    # re-create them, so it could never come back. Downing every fabric link and stopping
    # bgpd looks the same to the network: the leaves lose this spine at once and reroute.
    eths = [_eth_of(p) for p in _spine_fabric_ports(target)]
    if not eths:
        return False, "no fabric ports found on spine %s to down" % target
    cmd = "; ".join(["ip link set %s down" % e for e in eths] + ["supervisorctl stop bgpd >/dev/null"])
    rc, out, err = _run(_cnode(target, "sh", "-c", cmd), timeout=30)
    if rc != 0:
        return False, "could not take spine %s down (%s)" % (target, (err or out).strip()[:80])
    _scen_state["spinefail"] = {"spine": target, "eths": eths}
    return True, ("spine %s is down: all %d fabric links cut and BGP stopped — fabric survives on "
                  "%d remaining spine(s) (blast radius 1/%d)" % (target, len(eths), len(SPINES) - 1,
                                                                 len(SPINES)))


def _eth_of(port):
    """containerlab interface for a SONiC port: Ethernet<(k-1)*4> is eth<k> (fabgen's convention)."""
    return "eth%d" % (int(port[len("Ethernet"):]) // 4 + 1)


def scen_spinefail_stop():
    st = _scen_state.get("spinefail")
    if not st:
        return True, "spine failure not active"
    spine = st["spine"]
    if EXEC.get("mode") == "ssh":
        ports = st.get("ports") or _spine_fabric_ports(spine)
        remote = " ; ".join("sudo config interface startup %s" % p for p in ports)
        rc, out, err = _run(_node_ssh(spine, remote), timeout=40)
        _scen_state.pop("spinefail", None)
        if rc != 0:
            return False, "restore failed on spine %s (%s)" % (spine, (err or out).strip()[:80])
        return True, "restored spine %s — %d fabric ports back up; BGP re-establishing across the fabric" % (spine, len(ports))
    eths = st.get("eths") or [_eth_of(p) for p in _spine_fabric_ports(spine)]
    cmd = "; ".join(["ip link set %s up" % e for e in eths] + ["supervisorctl start bgpd >/dev/null"])
    rc, out, err = _run(_cnode(spine, "sh", "-c", cmd), timeout=30)
    # configure-fabric.sh re-applies the BGP config bgpd lost when it stopped, and restarts
    # bgpd only after zebra answers (ADR-0008).
    ok, msgc = _run_configure()
    _scen_state.pop("spinefail", None)
    if rc != 0:
        return False, "could not bring spine %s back (%s) — %s" % (spine, (err or out).strip()[:80], msgc)
    return ok, "restored spine %s — links up, BGP back — %s" % (spine, msgc)


# ── 5 · drain — graceful (RFC 8326) drain of a spine: hitless, 0 packet loss ──
def scen_drain_start():
    if len(SPINES) < 2:
        return False, "refused — need ≥2 spines to drain one hitlessly"
    exclude = _agent_drained() | set(filter(None, [_scen_state.get("spinefail", {}).get("spine")]))
    target = _busiest_spine(exclude=exclude) or next((s for s in SPINES if s not in exclude), None)
    if not target:
        return False, "no spine available to drain (conflicts with spine failure?)"
    ok, msg = _spine_graceful(target, True)
    if not ok:
        return False, "drain failed on %s — %s" % (target, msg)
    _scen_state["drain"] = {"spine": target}
    return True, "gracefully draining %s (%s) — 0 packet loss; traffic shifts to the other spine(s)" % (target, msg)


def scen_drain_stop():
    st = _scen_state.get("drain")
    if not st:
        return True, "drain not active"
    ok, msg = _spine_graceful(st["spine"], False)
    _scen_state.pop("drain", None)
    return True, "undrained %s — %s" % (st["spine"], msg)


# ── 6 · elephant — 2-3 concurrent floods from DIFFERENT leaves to one dst ──
def _elephant_sources(limit=3):
    dst = OVERLOAD_CFG.get("dst_ip")
    surge_src = OVERLOAD_CFG.get("src_host")
    if EXEC.get("mode") == "ssh":
        # Big flows from several LEAVES to the dst leaf's loopback: multiple flows
        # cross the spines, so flow-hashed ECMP loads them unevenly. Returns
        # (dst_loopback, [source_leaf_ids]); sources exclude the dst leaf.
        nodes = {n["id"]: n for n in TOPOLOGY_NODES}
        dst_host = next((n["id"] for n in TOPOLOGY_NODES
                         if (n.get("ip") or "").split("/")[0] == dst), None)
        dst_leaf = _leaf_of_host(dst_host)
        dst_lo = (nodes.get(dst_leaf) or {}).get("loopback")
        srcs = []
        for leaf in LEAVES:
            if leaf == dst_leaf:            # don't flood a leaf toward its own loopback
                continue
            srcs.append(leaf)
            if len(srcs) >= limit:
                break
        return dst_lo, srcs
    srcs = []
    for leaf in LEAVES:
        for h in HOSTS_OF_LEAF.get(leaf, []):
            hip = (HOST_IP.get(h) or "").split("/")[0]
            if dst and hip == dst:
                continue
            if h == surge_src:      # don't fight the surge scenario's pkill
                continue
            srcs.append(h)
            break                    # one host per leaf (different-leaf sources)
        if len(srcs) >= limit:
            break
    return dst, srcs


def scen_elephant_start():
    dst, srcs = _elephant_sources()
    if EXEC.get("mode") == "ssh":
        if not dst or len(srcs) < 2:
            return False, "need ≥2 source leaves + a dst loopback — not enough for an elephant flow"
        # distinct signature from the surge flood (-s 1200 vs surge's -s 1400) so
        # pgrep/pkill target only these flows, never the surge.
        pat = "ping -i 0.003 -s 1200 %s" % dst
        started = []
        for leaf in srcs:
            rc, _o, _e = _run(_node_ssh(
                leaf, "sudo nohup %s >/dev/null 2>&1 </dev/null & echo ok" % pat), timeout=15)
            if rc == 0:
                started.append(leaf)
        if not started:
            return False, "could not launch elephant flows over SSH"
        _scen_state["elephant"] = {"leaves": started, "dst": dst, "pat": pat}
        return True, "launched %d big flows → %s from %s — flow-hashed ECMP can't split one flow, so the spines load unevenly" % (
            len(started), dst, ", ".join(started))
    if not dst or len(srcs) < 2:
        return False, "need ≥2 host-bearing leaves + a dst — not enough for an elephant flow"
    ex = _exec_prefix()
    started = []
    for h in srcs:
        rc, _o, _e = _run(ex + ["-d", CONTAINER_PREFIX + h] + VIRTUAL_ELEPHANT_SIG.split() + [dst])
        if rc == 0:
            started.append(h)
    if not started:
        return False, "could not launch elephant flows"
    _scen_state["elephant"] = {"hosts": started, "dst": dst}
    return True, "launched %d concurrent flows → %s from %s — flow-based ECMP can't split one big flow; spines load unevenly" % (
        len(started), dst, ", ".join(started))


def scen_elephant_stop():
    st = _scen_state.get("elephant")
    if EXEC.get("mode") == "ssh":
        pat = (st or {}).get("pat")
        for leaf in (st or {}).get("leaves", []):
            if pat:
                _run(_node_ssh(leaf, "sudo pkill -f %s" % shlex.quote(pat)), timeout=15)
        _scen_state.pop("elephant", None)
        return True, "stopped elephant flows"
    hosts = st["hosts"] if st else []
    for h in hosts:
        _run(_cnode(h, "pkill", "-f", VIRTUAL_ELEPHANT_SIG))
    _scen_state.pop("elephant", None)
    return True, "stopped elephant flows"


# ── 7 · badconfig — break ONE leaf→spine BGP session; roll back via config-as-code ──
def scen_badconfig_start():
    if not LEAVES:
        return False, "no leaf to misconfigure"
    leaf = LEAVES[0]
    asn = ASN.get(leaf)
    peers = _bgp_neighbors(leaf)
    if not asn or not peers:
        return False, "could not read %s BGP neighbors to break one" % leaf
    peer = peers[0]
    rc, out, err = _run(_cnode(leaf, "vtysh",
                               "-c", "configure terminal",
                               "-c", "router bgp %s" % asn,
                               "-c", "neighbor %s shutdown" % peer))
    if rc != 0:
        return False, "bad-config push failed on %s (%s)" % (leaf, (err or out).strip()[:80])
    _scen_state["badconfig"] = {"leaf": leaf, "peer": peer, "asn": asn}
    return True, "pushed a bad config on %s: shut BGP neighbor %s — one session drops; roll back to recover" % (leaf, peer)


def scen_badconfig_stop():
    st = _scen_state.get("badconfig")
    if not st:
        return True, "bad config not active"
    # direct undo first, then the authoritative config-as-code rollback
    _run(_cnode(st["leaf"], "vtysh",
                "-c", "configure terminal",
                "-c", "router bgp %s" % st["asn"],
                "-c", "no neighbor %s shutdown" % st["peer"]))
    ok, msgc = _run_configure()
    _scen_state.pop("badconfig", None)
    return ok, "rolled back %s — %s" % (st["leaf"], msgc)


# ── 8 · thermalguard — the money shot: enable the ACT agent + give it heat ──
def scen_thermalguard_start():
    _agent_set_enabled(True)
    overload_control("start")   # give the agent something to react to
    return True, "auto-heal agent ON + surge started — watch it drain the hot spine at 72°C, then restore it as it cools"


def scen_thermalguard_stop():
    _agent_set_enabled(False)   # un-drains everything the agent drained
    overload_control("stop")
    return True, "auto-heal agent OFF + surge stopped + all spines restored"


# ── 9 · storm — the money shot: REAL congestion + REAL ECN marks + heat/power ──
# One toggle drives TWO real things on the target leaf at once, so pause/ECN, temperature
# and power visibly move together on the correlation strip:
#   (a) REAL ECN — a scoped kernel tc RED-ecn bottleneck on the target leaf's uplinks that
#       ONLY throttles the demo flow (TCP dport 5201) to a peer leaf's loopback,
#       so ECN CE marks accrue on the 'red' qdisc while BGP/transit stay untouched.
#       This is kernel tc RED ECN — NOT ASIC WRED, NOT gNMI. Label it exactly so.
#   (b) an incast ping flood from the other leaves → the target leaf's loopback (distinct
#       -s 1250 signature) to raise the REAL measured intensity that drives the
#       MODELED heat/power model. ICMP rides the unshaped 10gbit default class, so
#       it adds load/heat without being marked — the two signals stay independent.
# Every sidecar is started PERSISTENTLY via systemd-run (a plain nohup python over
# SSH dies on channel close). Teardown ALWAYS fully reverses both, best-effort.

# TCP sink: bind 0.0.0.0:5201, accept + drain forever, never exits.
_STORM_SINK_PY = (
    "import socket, threading\n"
    "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('0.0.0.0', 5201)); s.listen(128)\n"
    "def h(c):\n"
    "    try:\n"
    "        while c.recv(65536):\n"
    "            pass\n"
    "    except Exception:\n"
    "        pass\n"
    "    finally:\n"
    "        try:\n"
    "            c.close()\n"
    "        except Exception:\n"
    "            pass\n"
    "while True:\n"
    "    try:\n"
    "        c, _ = s.accept()\n"
    "        threading.Thread(target=h, args=(c,), daemon=True).start()\n"
    "    except Exception:\n"
    "        pass\n"
)

# TCP client: N threads each blast a 64 KB buffer at <DST>:5201 forever. %s = DST.
_STORM_CLIENT_PY = (
    "import socket, threading, time\n"
    "DST = '%s'\n"
    "buf = b'x' * 65536\n"
    "def w():\n"
    "    while True:\n"
    "        s = None\n"
    "        try:\n"
    "            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
    "            s.connect((DST, 5201))\n"
    "            while True:\n"
    "                s.sendall(buf)\n"
    "        except Exception:\n"
    "            time.sleep(0.5)\n"
    "        finally:\n"
    "            try:\n"
    "                if s: s.close()\n"
    "            except Exception:\n"
    "                pass\n"
    "for _ in range(4):\n"
    "    threading.Thread(target=w, daemon=True).start()\n"
    "while True:\n"
    "    time.sleep(3600)\n"
)


def _storm_target():
    """Pick the target leaf (the one with the most uplinks) + a distinct peer leaf to sink at.
    Returns (target, uplink_devs, peer, peer_loopback) or (None, …) if not enough."""
    if not LEAVES:
        return None, [], None, None
    target = max(LEAVES, key=lambda lf: len(UPLINKS.get(lf, [])))
    devs = [p for p, _sp in UPLINKS.get(target, [])]
    peer = next((lf for lf in LEAVES if lf != target), None)
    peer_lo = next((n.get("loopback") for n in TOPOLOGY_NODES
                    if n.get("id") == peer), None)
    return target, devs, peer, peer_lo


def _storm_write_start(node, path, content, unit):
    """Heredoc the sidecar script to <path> on <node>, then (re)start it under a
    transient systemd unit so it survives the SSH channel close. One round-trip."""
    remote = (
        "cat > %s <<'PYEOF'\n%s\nPYEOF\n"
        "sudo systemctl stop %s 2>/dev/null; "
        "sudo systemctl reset-failed %s 2>/dev/null; "
        "sudo systemd-run --unit=%s --collect python3 %s"
    ) % (path, content, unit, unit, unit, path)
    return _run(_node_ssh(node, remote), timeout=20)


def _storm_tc_cmd(devs):
    """Scoped RED-ecn bottleneck: htb default class is 10gbit (BGP/transit ride it
    unshaped), ONLY TCP dport 5201 is steered into a 1mbit RED-ecn class → CE marks
    accrue on that flow alone. This is why BGP stays up during the storm."""
    parts = ["sudo sysctl -w net.ipv4.tcp_ecn=1"]
    for d in devs:
        parts += [
            "sudo ethtool -K %s gso off tso off gro off" % d,
            "sudo tc qdisc del dev %s root 2>/dev/null" % d,
            "sudo tc qdisc add dev %s root handle 1: htb default 1" % d,
            "sudo tc class add dev %s parent 1: classid 1:1 htb rate 10gbit ceil 10gbit" % d,
            "sudo tc class add dev %s parent 1: classid 1:10 htb rate 1mbit ceil 1mbit burst 4k" % d,
            "sudo tc qdisc add dev %s parent 1:10 handle 20: red limit 100000 "
            "min 3000 max 6000 avpkt 1500 burst 5 ecn" % d,
            "sudo tc filter add dev %s protocol ip parent 1: prio 1 u32 "
            "match ip dport 5201 0xffff flowid 1:10" % d,
        ]
    return "; ".join(parts)


def _storm_teardown(st):
    """FULLY reverse the storm, best-effort. Safe to call with partial/empty state;
    every sub-step is guarded so one failure never blocks the rest."""
    st = st or {}
    target = st.get("target")
    peer = st.get("peer")
    devs = st.get("uplinks", [])
    pat = st.get("pat")
    # 1) stop the traffic generators
    if target:
        _run(_node_ssh(target, "sudo systemctl stop ecnclient 2>/dev/null; "
                               "sudo systemctl reset-failed ecnclient 2>/dev/null"), timeout=15)
    if peer:
        _run(_node_ssh(peer, "sudo systemctl stop ecnsink 2>/dev/null; "
                             "sudo systemctl reset-failed ecnsink 2>/dev/null"), timeout=15)
    for lf in st.get("ping_leaves", []):
        if pat:
            _run(_node_ssh(lf, "sudo pkill -f %s" % shlex.quote(pat)), timeout=15)
    # 2) remove the RED-ecn bottleneck + restore offload on the target leaf's uplinks
    if target and devs:
        undo = []
        for d in devs:
            undo.append("sudo tc qdisc del dev %s root 2>/dev/null" % d)
            undo.append("sudo ethtool -K %s gso on tso on gro on" % d)
        _run(_node_ssh(target, "; ".join(undo)), timeout=20)


def _storm_teardown_all():
    """State-independent storm sweep: clean EVERY leaf regardless of _scen_state, so an
    orphaned storm (state lost across a copilot restart) can't leave RED-ecn qdiscs or the
    ecnsink/ecnclient units running. Mirrors reset's per-spine drain belt-and-braces."""
    pat = "ping -i 0.003 -s 1250"
    for lf in LEAVES:
        try:
            cmds = ["sudo systemctl stop ecnclient 2>/dev/null",
                    "sudo systemctl reset-failed ecnclient 2>/dev/null",
                    "sudo systemctl stop ecnsink 2>/dev/null",
                    "sudo systemctl reset-failed ecnsink 2>/dev/null",
                    "sudo pkill -f %s 2>/dev/null" % shlex.quote(pat)]
            for d, _sp in UPLINKS.get(lf, []):
                cmds.append("sudo tc qdisc del dev %s root 2>/dev/null" % d)
                cmds.append("sudo ethtool -K %s gso on tso on gro on 2>/dev/null" % d)
            _run(_node_ssh(lf, "; ".join(cmds) + "; true"), timeout=20)
        except Exception as e:
            print("reset: storm sweep on %s error: %s" % (lf, e), file=sys.stderr)


def scen_storm_start():
    if EXEC.get("mode") != "ssh":
        return False, ("storm needs an ssh-mode fabric whose switches use the Linux netdev "
                       "as the datapath (kernel tc RED-ecn + incast)")
    target, devs, peer, peer_lo = _storm_target()
    if not target or not devs or not peer or not peer_lo:
        return False, "storm needs a target leaf with uplinks + a peer leaf loopback to sink at"
    # CAPABILITY GATE (landmine 3): `tc qdisc … red … ecn` drives the Linux netdev.
    # On real silicon that netdev is only the SAI host-interface representor, so the
    # command returns rc=0 and never reaches the ASIC datapath — ECN would read a
    # permanent, confident zero. Refuse loudly instead of reporting success.
    ok_t, why_t = scenario_node_ok("storm", target)
    if not ok_t:
        return False, ("congestion storm REFUSED on %s: %s. Nothing was executed — "
                       "the tc RED-ecn technique cannot produce real ECN marks here."
                       % (target, why_t))
    pat = "ping -i 0.003 -s 1250 %s" % ((next(
        (n.get("loopback") for n in TOPOLOGY_NODES if n.get("id") == target), None)) or "")
    st = {"target": target, "peer": peer, "peer_lo": peer_lo,
          "uplinks": devs, "ping_leaves": [], "pat": pat}
    _scen_state["storm"] = st          # record BEFORE acting so stop can always reverse
    try:
        # (a) REAL ECN sidecar — sink on the peer leaf, scoped RED-ecn and client on the target leaf
        rc, _o, e = _storm_write_start(peer, "/tmp/ecnsink.py", _STORM_SINK_PY, "ecnsink")
        if rc != 0:
            raise RuntimeError("ecn sink on %s failed: %s" % (peer, (e or "").strip()[:80]))
        rc, _o, e = _run(_node_ssh(target, _storm_tc_cmd(devs)), timeout=25)
        if rc != 0:
            raise RuntimeError("RED-ecn setup on %s failed: %s" % (target, (e or "").strip()[:80]))
        rc, _o, e = _storm_write_start(
            target, "/tmp/ecnclient.py", _STORM_CLIENT_PY % peer_lo, "ecnclient")
        if rc != 0:
            raise RuntimeError("ecn client on %s failed: %s" % (target, (e or "").strip()[:80]))
        # (b) incast ping flood from the OTHER leaves → target loopback (heat/power)
        started = []
        for lf in LEAVES:
            if lf == target:
                continue
            rc, _o, _e = _run(_node_ssh(
                lf, "sudo nohup %s >/dev/null 2>&1 </dev/null & echo ok" % pat), timeout=15)
            if rc == 0:
                started.append(lf)
        st["ping_leaves"] = started
        return True, ("storm on %s: REAL tc RED-ecn CE marks on %s (flow→%s:5201) + incast "
                      "from %s — ECN is measured; heat and power are modelled from it (BGP stays up: only "
                      "the demo flow is shaped)" % (
                          target, "/".join(devs), peer, ", ".join(started) or "no leaves"))
    except Exception as ex:
        _storm_teardown(st)            # any sub-step failed → leave the fabric clean
        _scen_state.pop("storm", None)
        return False, "storm aborted + rolled back: %s" % (str(ex)[:120])


def scen_storm_stop():
    st = _scen_state.get("storm")
    _storm_teardown(st)
    _scen_state.pop("storm", None)
    return True, "storm stopped — RED-ecn removed, offload restored, sink/client/incast killed"


# ═══════════════════════════════════════════════════════════════════════════════
#  GPU COLLECTIVES — model AI-training network patterns with the SAME real ping
#  engine as every other flow, on two RESERVED signatures so teardown never crosses
#  the other flows:  -s 1500 = ring all-reduce,  -s 1600 = all-to-all.
#  Rails = the leaves (the honest "1 leaf = 1 rail" abstraction — flows launch on a
#  leaf toward a peer-leaf loopback, exactly like ambient/traffic-studio). A worker
#  thread bursts the flood during the "comms" window (wall-clock phase < burst_ms)
#  and kills it during the "compute" window, so the REAL network sawtooths on a
#  training-step cadence; the exporter's step clock (SAME phase) puts MODELED GPU
#  power in anti-phase — power peaks in compute, dips during all-reduce. Continuous
#  mode (no pause) pins util high for the thermal-throttle story. One collective at
#  a time (they share rails + signatures). Cadence is slowed (~16s step) for stage
#  visibility — real steps are sub-second; say so honestly.
# ═══════════════════════════════════════════════════════════════════════════════
ALLREDUCE_SIG = "ping -i 0.003 -s 1500"      # RESERVED — ring all-reduce
ALLTOALL_SIG = "ping -i 0.003 -s 1600"       # RESERVED — all-to-all
# SELF-MATCH TRAP: pkill -f matches the WHOLE command line, and this command's
# own shell contains the signature — so `pkill -f "ping -i 0.003 -s 1500"` kills
# its own SSH session on the first iteration and the retries never run. The
# collective then keeps streaming while the API cheerfully reports it stopped,
# and a later restart orphans the flows entirely. Bracketing the first character
# makes the pattern still match a real `ping ...` process but no longer match the
# literal text of this command. (Same trap as the BENCHBURST kill.)
_COLLECTIVE_KILL = ('for i in 1 2 3; do sudo pkill -f "%s" 2>/dev/null; sleep 0.2; done; '
                    'sudo pkill -9 -f "%s" 2>/dev/null; true')


def _brk(sig):
    """Bracket the first character of a pkill pattern so it cannot match itself."""
    return ("[" + sig[0] + "]" + sig[1:]) if sig else sig
# cross-process channel to the exporter's GPU energy model (same host). app.py owns
# it: collective start/stop + coolant-loss scenario write it; the exporter reads it
# each sweep to drive MODELED GPU power/thermals in phase-lock with the real bursts.
GPU_STATE_FILE = os.environ.get("GPU_STATE_FILE", os.path.join(_DS_DIR, ".run", "gpu_state.json"))
_gpu_extra = {"coolant_flow_factor": {}}     # scenario overrides (coolant-loss, Phase D)
_collective = {"scen": None, "mode": None, "phased": False, "straggler": None,
               "streams": [], "sig": None, "bursting": set(), "started": 0.0}
_collective_lock = threading.Lock()
_collective_stop = threading.Event()
_collective_thread = None


def _collective_cfg():
    with _fab_lock:
        return dict(COLLECTIVE_CFG or {})


def _rail_loopbacks():
    """Ordered [(rail, loopback)] for the collective rails (default: every leaf).
    Skips rails with no loopback; derived fresh so a fabric rebuild self-adjusts."""
    cfg = _collective_cfg()
    with _fab_lock:
        leaves = list(LEAVES)
        lo = {n.get("id"): n.get("loopback") for n in TOPOLOGY_NODES}
    rails = [r for r in (cfg.get("rails") or leaves) if lo.get(r)]
    return [(r, lo[r]) for r in rails]


def _collective_streams(mode):
    """[{rail, dst_lo, pat}] for the collective. ring: rail[i]->rail[(i+1)%N] loopback;
    mesh: every rail->every other rail (capped). Each pat is a full ping cmdline."""
    rl = _rail_loopbacks()
    n = len(rl)
    if n < 2:
        return []
    out = []
    if mode == "ring":
        for i, (rail, _lo) in enumerate(rl):
            nxt = rl[(i + 1) % n][1]
            out.append({"rail": rail, "dst_lo": nxt, "pat": "%s %s" % (ALLREDUCE_SIG, nxt)})
    else:  # mesh (all-to-all)
        for i, (rail, _lo) in enumerate(rl):
            for j, (_r2, lo2) in enumerate(rl):
                if i != j:
                    out.append({"rail": rail, "dst_lo": lo2, "pat": "%s %s" % (ALLTOALL_SIG, lo2)})
        out = out[:TRAFFIC_MAX_STREAMS]
    return out


def _collective_launch_rails(rails):
    """Start every stream whose rail is in `rails` (parallel over SSH)."""
    with _collective_lock:
        streams = [s for s in _collective["streams"] if s["rail"] in rails]

    def _one(s):
        _run(_node_ssh(s["rail"], "sudo nohup %s >/dev/null 2>&1 </dev/null & echo ok" % s["pat"]),
             timeout=15)
    try:
        for f in [_FLEET_POOL.submit(_one, s) for s in streams]:
            try:
                f.result(timeout=20)
            except Exception:
                pass
    except Exception:
        pass


def _collective_kill_rails(rails, sig):
    """pkill the signature on each rail in `rails` (parallel; 3x for start-races)."""
    kill = _COLLECTIVE_KILL % (_brk(sig), _brk(sig))

    def _one(r):
        _run(_node_ssh(r, kill), timeout=15)
    try:
        for f in [_FLEET_POOL.submit(_one, r) for r in rails]:
            try:
                f.result(timeout=20)
            except Exception:
                pass
    except Exception:
        pass


def _collective_worker():
    """Drive burst/compute windows off the wall clock so the exporter (same phase)
    can put GPU power in anti-phase. Never raises; exits when _collective_stop is set."""
    cfg = _collective_cfg()
    step_ms = float(cfg.get("step_ms", 16000)) or 16000.0
    burst_ms = float(cfg.get("burst_ms", 6000)) or 6000.0
    while not _collective_stop.is_set():
        with _collective_lock:
            mode = _collective["mode"]
            phased = _collective["phased"]
            straggler = _collective["straggler"]
            sig = _collective["sig"]
            allr = [s["rail"] for s in _collective["streams"]]
            bursting = set(_collective["bursting"])
        if not mode or not allr:
            break
        rails = list(dict.fromkeys(allr))                 # unique, order-preserving
        if not phased:
            should = set(rails)                           # continuous: everyone always on
        else:
            phase = (time.time() * 1000.0) % step_ms
            should = set(r for r in rails if phase < burst_ms)
            if straggler in rails:                        # straggler holds the ring open longer
                if phase < min(step_ms - 1, burst_ms * 1.8):
                    should.add(straggler)
                else:
                    should.discard(straggler)
        start = should - bursting
        stop = bursting - should
        if start:
            _collective_launch_rails(start)
        if stop:
            _collective_kill_rails(stop, sig)
        if start or stop:
            with _collective_lock:
                _collective["bursting"] = should
        _collective_stop.wait(0.5 if phased else 1.0)
    # exit: make sure BOTH signatures are dead on every rail
    allrails = [r for r, _ in _rail_loopbacks()]
    _collective_kill_rails(allrails, ALLREDUCE_SIG)
    _collective_kill_rails(allrails, ALLTOALL_SIG)


def collective_start(scen, mode="ring", phased=True, straggler=None):
    """Start a collective (one at a time). If a DIFFERENT collective is running it is
    switched (stopped first) — so an event like straggler cleanly takes over from the
    baseline training. Returns (ok, msg)."""
    global _collective_thread
    with _collective_lock:
        cur = _collective["scen"]
    if cur == scen:
        return True, "%s already running" % scen
    if cur:
        collective_stop()                 # switch collectives (baseline -> event, or vice versa)
    with _collective_lock:
        streams = _collective_streams(mode)
        if not streams:
            return False, "need >=2 rails with loopbacks for a collective"
        rails_present = {s["rail"] for s in streams}
        sig = ALLREDUCE_SIG if mode == "ring" else ALLTOALL_SIG
        _collective.update({"scen": scen, "mode": mode, "phased": phased,
                            "straggler": straggler if straggler in rails_present else None,
                            "streams": streams, "sig": sig, "bursting": set(),
                            "started": time.time()})
        nrails = len(rails_present)
    _collective_stop.clear()
    _collective_thread = threading.Thread(target=_collective_worker, name="collective", daemon=True)
    _collective_thread.start()
    _write_gpu_state()               # tell the exporter's GPU model training is on
    cad = "phased (training-step burst/compute)" if phased else "continuous (sustained)"
    extra = " · straggler=%s" % straggler if straggler else ""
    kind = "ring all-reduce" if mode == "ring" else "all-to-all"
    return True, ("%s across %d rails, %s%s — real -s %s flows; GPU power sawtooths in anti-phase"
                  % (kind, nrails, cad, extra, "1500" if mode == "ring" else "1600"))


def collective_stop():
    """Stop the running collective + kill both signatures on every rail. Idempotent."""
    _collective_stop.set()
    th = _collective_thread
    if th and th.is_alive():
        th.join(timeout=6)
    allrails = [r for r, _ in _rail_loopbacks()]
    _collective_kill_rails(allrails, ALLREDUCE_SIG)
    _collective_kill_rails(allrails, ALLTOALL_SIG)
    with _collective_lock:
        _collective.update({"scen": None, "mode": None, "phased": False, "straggler": None,
                            "streams": [], "sig": None, "bursting": set(), "started": 0.0})
    _write_gpu_state()               # tell the exporter's GPU model training is off
    return True, "collective stopped — all-reduce/all-to-all flows killed on every rail"


def _write_gpu_state():
    """Publish the collective's state to GPU_STATE_FILE for the exporter's GPU energy
    model (cross-process, same host). Atomic write; never raises."""
    try:
        s = collective_summary()
        doc = {"training": bool(s["scen"]), "scen": s["scen"], "phased": s["phased"],
               "step_ms": s["step_ms"], "burst_ms": s["burst_ms"],
               "straggler": s["straggler"], "rails": s["rails"],
               "coolant_flow_factor": dict(_gpu_extra.get("coolant_flow_factor") or {}),
               "ts": time.time()}
        os.makedirs(os.path.dirname(GPU_STATE_FILE) or ".", exist_ok=True)
        tmp = GPU_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, GPU_STATE_FILE)
    except Exception as e:
        print("gpu_state write error:", e, file=sys.stderr)


def _collective_active():
    with _collective_lock:
        return _collective["scen"]


def build_gpu():
    """GET /api/gpu — the GPU Pods view. Per-server + per-rail MODELED metrics (from
    the exporter via Prometheus), the rail leaves' congestion (PFC/ECN/WRED rate, from
    the modeled per-switch series), real rail load, and the live collective state.
    Never raises; degrades to zeros if Prometheus is unavailable."""
    maybe_reload()
    with _fab_lock:
        gpu_nodes = [dict(n) for n in TOPOLOGY_NODES if n.get("kind") == "gpu"]
        coll_cfg = dict(COLLECTIVE_CFG or {})
    coll = collective_summary()
    if not gpu_nodes:
        return {"pod": None, "rails": [], "servers": [], "collective": coll,
                "msg": "no GPU pod in this fabric"}
    Q = {
        "util": "gpu_util_percent", "power": "gpu_server_power_watts",
        "junc": "gpu_junction_celsius", "hbmt": "gpu_hbm_temp_celsius",
        "hbmbw": "gpu_hbm_bandwidth_gbps", "nvl": "gpu_nvlink_gbps",
        "rack": "gpu_rack_power_kw", "railutil": "gpu_rail_util_percent",
        "cs": "gpu_coolant_supply_celsius", "cr": "gpu_coolant_return_celsius",
        "cf": "gpu_coolant_flow_lpm", "pump": "gpu_cdu_pump_kw",
        "load": "fabric_switch_load_bps",
        "pfc": "sum by (instance) (rate(sonic_pfc_rx_pause_frames_total[30s]))",
        "ecn": "sum by (instance) (rate(sonic_ecn_marked_packets_total[30s]))",
        "wred": "sum by (instance) (rate(sonic_queue_wred_dropped_packets_total[30s]))",
    }
    try:
        res = _prom_batch(list(Q.values()))
    except Exception:
        res = {}

    def m(key, label):
        return _as_map(res.get(Q[key], []), label)

    util, power, junc = m("util", "server"), m("power", "server"), m("junc", "server")
    hbmt, hbmbw, nvl = m("hbmt", "server"), m("hbmbw", "server"), m("nvl", "server")
    rack, railutil = m("rack", "rail"), m("railutil", "rail")
    cs, cr, cf, pump = m("cs", "rail"), m("cr", "rail"), m("cf", "rail"), m("pump", "rail")
    load = m("load", "switch")
    pfc, ecn, wred = m("pfc", "instance"), m("ecn", "instance"), m("wred", "instance")

    servers = []
    for n in gpu_nodes:
        sid = n["id"]
        servers.append({"id": sid, "rail": n.get("rail"), "model": n.get("gpu_model"),
                        "gpus": n.get("gpus"), "tdp_w": n.get("tdp_w"),
                        "util": round(util.get(sid, 0.0), 1),
                        "power_w": round(power.get(sid, 0.0)),
                        "junction": round(junc.get(sid, 0.0), 1),
                        "hbm_temp": round(hbmt.get(sid, 0.0), 1),
                        "hbm_bw_gbps": round(hbmbw.get(sid, 0.0)),
                        "nvlink_gbps": round(nvl.get(sid, 0.0))})
    railset = []
    for n in gpu_nodes:
        if n.get("rail") and n["rail"] not in railset:
            railset.append(n["rail"])
    rails = []
    for rail in railset:
        on = [s for s in servers if s["rail"] == rail]
        rails.append({"rail": rail,
                      "rack_kw": round(rack.get(rail, 0.0), 2),
                      "util": round(railutil.get(rail, 0.0), 1),
                      "coolant_supply": round(cs.get(rail, 0.0), 1),
                      "coolant_return": round(cr.get(rail, 0.0), 1),
                      "coolant_flow": round(cf.get(rail, 0.0), 1),
                      "cdu_pump_kw": round(pump.get(rail, 0.0), 2),
                      "junction": round(max([s["junction"] for s in on] or [0.0]), 1),
                      "load_bps": round(load.get(rail, 0.0)),
                      "pfc_rate": round(pfc.get(rail, 0.0), 1),
                      "ecn_rate": round(ecn.get(rail, 0.0), 1),
                      "wred_rate": round(wred.get(rail, 0.0), 1),
                      "bursting": rail in coll.get("bursting", []),
                      "servers": [s["id"] for s in on]})
    total_kw = round(sum(r["rack_kw"] for r in rails), 1)
    avg_util = round(sum(s["util"] for s in servers) / len(servers), 1) if servers else 0.0
    hottest = max(servers, key=lambda s: s["junction"], default=None)
    pod = {"cluster": coll_cfg.get("cluster") or gpu_nodes[0].get("cluster"),
           "gpu_model": coll_cfg.get("gpu_model") or gpu_nodes[0].get("gpu_model"),
           "servers": len(servers), "rails": len(rails),
           "gpus_total": sum(int(n.get("gpus", 0)) for n in gpu_nodes),
           "total_kw": total_kw, "avg_util": avg_util,
           "hottest_junction": hottest["junction"] if hottest else 0.0,
           "hottest_server": hottest["id"] if hottest else None,
           "training": coll["scen"], "phased": coll["phased"],
           "straggler": coll["straggler"]}
    return {"pod": pod, "rails": rails, "servers": servers, "collective": coll}


def collective_summary():
    """Cheap in-memory truth for /api/topology + /api/gpu (no SSH)."""
    cfg = _collective_cfg()
    with _collective_lock:
        return {"scen": _collective["scen"], "mode": _collective["mode"],
                "phased": _collective["phased"], "straggler": _collective["straggler"],
                "rails": sorted({s["rail"] for s in _collective["streams"]}),
                "bursting": sorted(_collective["bursting"]),
                "step_ms": float(cfg.get("step_ms", 16000)),
                "burst_ms": float(cfg.get("burst_ms", 6000)),
                "started": _collective["started"]}


# ── GPU collective scenarios (registered in SCENARIOS below) ──
def scen_training_start():
    return collective_start("training", mode="ring", phased=True)


def scen_training_stop():
    return collective_stop()


def scen_alltoall_start():
    return collective_start("alltoall", mode="mesh", phased=True)


def scen_alltoall_stop():
    return collective_stop()


def scen_straggler_start():
    rl = _rail_loopbacks()
    strag = rl[len(rl) // 2][0] if rl else None      # a middle rail holds up the ring
    return collective_start("straggler", mode="ring", phased=True, straggler=strag)


def scen_straggler_stop():
    return collective_stop()


def scen_sustained_start():
    return collective_start("sustained", mode="ring", phased=False)   # no compute gaps


def scen_sustained_stop():
    return collective_stop()


def scen_coolant_start():
    """Liquid-cooling loss-of-flow on one rack: the CDU flow drops, dT spikes, junction
    runs away fast — the liquid failure mode. OVERLAYS whatever collective is running
    (the baseline training keeps going); starts a sustained one only if nothing runs."""
    rl = _rail_loopbacks()
    if not rl:
        return False, "no GPU rails to starve"
    victim = rl[len(rl) // 2][0]                 # a middle rack loses coolant
    if not _collective_active():
        collective_start("coolant", mode="ring", phased=False)   # own heat source if idle
    _gpu_extra["coolant_flow_factor"] = {victim: 0.2}            # 20% flow — starved loop
    _write_gpu_state()
    return True, ("coolant loss on %s (flow -80%%) — junction runs away as dT spikes; the "
                  "liquid-cooling failure mode (vs air's slow warm-up)" % victim)


def scen_coolant_stop():
    _gpu_extra["coolant_flow_factor"] = {}       # restore full flow
    if _collective_active() == "coolant":        # only stop the collective if coolant started it
        return collective_stop()
    _write_gpu_state()                           # otherwise leave the baseline training running
    return True, "coolant restored — flow back to normal"


SCENARIOS = [
    {"id": "surge", "emoji": "🌊", "label": "Traffic surge", "group": "traffic", "kind": "toggle",
     "tip": "Flood one host from all the others and watch heat climb.",
     "proves": "congestion + heat",
     "start": scen_surge_start, "stop": scen_surge_stop},
    {"id": "cablecut", "emoji": "✂️", "label": "Cable cut", "group": "failure", "kind": "toggle",
     "tip": "Hard-down the busiest uplink; the link and one BGP session drop.",
     "proves": "abrupt link loss + BGP self-heal (brief loss)",
     "start": scen_cablecut_start, "stop": scen_cablecut_stop},
    {"id": "grayfail", "emoji": "👻", "label": "Gray failure", "group": "failure", "kind": "toggle",
     "tip": "Inject a silent 2% packet loss with netem — no alarm fires.",
     "proves": "silent 2% packet loss — invisible to basic monitoring; needs active probing",
     "start": scen_grayfail_start, "stop": scen_grayfail_stop},
    {"id": "spinefail", "emoji": "💀", "label": "Spine failure", "group": "failure", "kind": "toggle",
     "tip": "Take a whole spine down (its links cut, BGP stopped); the fabric keeps forwarding.",
     "proves": "a whole spine goes down; fabric survives on remaining spines (blast radius = 1/S)",
     "start": scen_spinefail_start, "stop": scen_spinefail_stop},
    {"id": "drain", "emoji": "🔧", "label": "Graceful drain", "group": "ops", "kind": "toggle",
     "tip": "RFC 8326 graceful-shutdown a spine — traffic leaves it with 0 loss.",
     "proves": "hitless maintenance — 0 packet loss, vs the cable cut's loss",
     "start": scen_drain_start, "stop": scen_drain_stop},
    {"id": "elephant", "emoji": "🐘", "label": "Elephant flow / ECMP imbalance", "group": "traffic", "kind": "toggle",
     "tip": "Several big flows toward one dst — ECMP can't rebalance a single flow.",
     "proves": "flow-based ECMP can't split one big flow; entropy matters for AI fabrics",
     "start": scen_elephant_start, "stop": scen_elephant_stop},
    {"id": "storm", "emoji": "🌩️", "label": "Congestion storm", "group": "traffic", "kind": "toggle",
     "tip": "Drive one leaf into congestion — REAL tc RED-ecn CE marks + an incast flood; ECN, heat and power move together.",
     "proves": "real congestion + real ECN marks + heat/power rise together",
     "start": scen_storm_start, "stop": scen_storm_stop},
    {"id": "badconfig", "emoji": "⚙️", "label": "Bad config push", "group": "ops", "kind": "toggle",
     "tip": "Break one BGP session with a bad change; roll back via config-as-code.",
     "proves": "a mistaken change breaks a session; detect + roll back",
     "start": scen_badconfig_start, "stop": scen_badconfig_stop},
    {"id": "thermalguard", "emoji": "🤒", "label": "Thermal auto-heal", "group": "ops", "kind": "toggle",
     "tip": "Arm the ACT agent + a surge; it drains the hot switch, watches it cool, restores it.",
     "proves": "the fabric heals ITSELF: on 72°C the agent drains the hot switch, watches it cool, restores it",
     "start": scen_thermalguard_start, "stop": scen_thermalguard_stop},
    {"id": "training", "emoji": "🧠", "label": "AI training step (all-reduce)", "group": "gpu", "kind": "toggle",
     "tip": "Ring all-reduce on a training-step cadence — real -s 1500 flows burst then pause; GPU power sawtooths in anti-phase with the fabric.",
     "proves": "compute↔comms: GPU power peaks in compute, dips during all-reduce — power and network in anti-phase over gNMI",
     "start": scen_training_start, "stop": scen_training_stop},
    {"id": "alltoall", "emoji": "🔀", "label": "All-to-all (MoE)", "group": "gpu", "kind": "toggle",
     "tip": "Full rail-mesh all-to-all (-s 1600, N² flows) — elephant flows hash-collide and polarize onto one spine.",
     "proves": "all-to-all is the harshest fabric test; ECMP polarization drives asymmetric ECN/WRED + a spine hotspot",
     "start": scen_alltoall_start, "stop": scen_alltoall_stop},
    {"id": "straggler", "emoji": "🐢", "label": "Straggler GPU", "group": "gpu", "kind": "toggle",
     "tip": "One rail holds the ring open past the compute window — the whole collective waits on the slow link.",
     "proves": "one slow link stalls thousands of GPUs — a network straggler shows up as a fleet-wide power/thermal signature",
     "start": scen_straggler_start, "stop": scen_straggler_stop},
    {"id": "sustained", "emoji": "🔥", "label": "Sustained training (thermal)", "group": "gpu", "kind": "toggle",
     "tip": "Run all-reduce continuously (no compute gaps) — GPU util pins high, junction temp climbs toward the throttle limit.",
     "proves": "sustained load → junction temp climbs the RC curve → thermal throttle caps the power peak; liquid-loop inertia",
     "start": scen_sustained_start, "stop": scen_sustained_stop},
    {"id": "coolant", "emoji": "💧", "label": "Coolant loss (liquid failure)", "group": "gpu", "kind": "toggle",
     "tip": "Drop one rack's CDU coolant flow under sustained training — dT spikes, junction runs away fast.",
     "proves": "liquid cooling is stable under load but fails FAST on loss-of-flow — junction runs away, unlike air's slow warm-up",
     "start": scen_coolant_start, "stop": scen_coolant_stop},
]
SCEN_BY_ID = {s["id"]: s for s in SCENARIOS}

# ═══════════════════════════════════════════════════════════════════════════════
#  SCENARIO CAPABILITY GATE (landmine 3).
#  Some scenarios only work where the Linux netdev IS the datapath. `tc qdisc …
#  red … ecn` and `netem loss` both act on the front-panel netdev; on real
#  Tomahawk silicon that netdev is a CPU representor, so the command returns rc=0
#  and NEVER touches the ASIC datapath — the scenario would report success while
#  the graphs stayed flat at zero. Each scenario therefore DECLARES the node
#  classes it is valid for and the actions it needs; the gate refuses (or names
#  the alternative technique) before anything is executed, and the refusal is
#  surfaced in the result instead of a pretend success.
# ═══════════════════════════════════════════════════════════════════════════════
# "enterprise-sonic" below (and in _node_class) is the node class nodecaps.py gives
# Enterprise SONiC switches, which report more hardware sensors than the virtual image.
SCEN_CAPS = {
    # id:        (required action ids,   valid node classes)
    "storm":     (["tc_red_ecn"],        ["virtual", "enterprise-sonic"]),
    "grayfail":  (["netem_loss"],        ["virtual", "enterprise-sonic"]),
    "spinefail": ([],                    ["any"]),
    "drain":     (["bgp_graceful_shutdown"], ["any"]),
    "surge":     (["ping_flood"],        ["any"]),
    "elephant":  (["ping_flood"],        ["any"]),
    "cablecut":  (["port_shutdown"],     ["any"]),
}
for _s in SCENARIOS:
    _req, _cls = SCEN_CAPS.get(_s["id"], ([], ["any"]))
    _s.setdefault("requires", _req)
    _s.setdefault("node_classes", _cls)


def _scen_candidates(sid):
    """The node ids a scenario could act on — the set the gate is evaluated over."""
    if sid in ("spinefail", "drain", "thermalguard"):
        return list(SPINES)
    if sid in ("storm", "grayfail", "cablecut", "surge", "elephant"):
        return list(LEAVES)
    if sid.startswith("traffic"):
        return list(LEAVES)
    return list(LEAVES) + list(SPINES)


def _node_class(nid):
    """The class a node belongs to for scenario validity: virtual | enterprise-sonic
    | community-sonic (a physical node always reports its NOS, never 'virtual')."""
    b = node_binding(nid)
    return b.get("nos") if b.get("physical") else "virtual"


# Scenarios that only work in ssh mode (a hand-written fabric.json reached over SSH). The
# dashboard greys their buttons in the docker lab and shows the reason, instead of failing
# on press.
_GPU_ONLY = ("needs a GPU fabric with rail loopbacks (ssh mode); ./run.sh virtual builds a "
             "plain spine-leaf fabric without them")
SSH_ONLY_SCENARIOS = {
    "storm": "needs an ssh-mode fabric whose switches use the Linux netdev as the datapath "
             "(for example virtual switches reached over SSH): the storm drives a kernel "
             "tc RED-ecn queue on a leaf's uplinks, which ./run.sh virtual doesn't set up. "
             "It is refused on community-SONiC hardware such as the Z9100, because tc RED "
             "never reaches the ASIC",
    "training": _GPU_ONLY, "alltoall": _GPU_ONLY, "straggler": _GPU_ONLY,
    "sustained": _GPU_ONLY, "coolant": _GPU_ONLY,
}


def scenario_capability(sid):
    """(available, reason, detail) for one scenario against the LIVE fabric.

    available: True if at least one candidate node can really run it.
    detail: per-node verdicts + the suggested alternative technique.
    """
    sc = SCEN_BY_ID.get(sid)
    if not sc:
        return False, "unknown scenario '%s'" % sid, {}
    if sid in SSH_ONLY_SCENARIOS and EXEC.get("mode") != "ssh":
        return False, SSH_ONLY_SCENARIOS[sid], {"nodes": {}}
    req = sc.get("requires") or []
    classes = sc.get("node_classes") or ["any"]
    cands = _scen_candidates(sid)
    if not req and "any" in classes:
        return True, "", {"nodes": {}, "requires": req, "node_classes": classes}
    per, ok_nodes, alts = {}, [], []
    for nid in cands:
        cls = _node_class(nid)
        acts = node_actions(nid)
        bad = None
        for aid in req:
            a = acts.get(aid) or {"supported": True}
            if not a.get("supported"):
                bad = a.get("reason") or ("action '%s' is unsupported here" % aid)
                if a.get("alternative"):
                    alts.append(a["alternative"])
                break
        if bad is None and "any" not in classes and cls not in classes:
            bad = "node class '%s' is not one of %s" % (cls, "/".join(classes))
        if bad:
            per[nid] = {"ok": False, "reason": bad, "class": cls}
        else:
            per[nid] = {"ok": True, "class": cls}
            ok_nodes.append(nid)
    detail = {"nodes": per, "requires": req, "node_classes": classes,
              "eligible": ok_nodes}
    if alts:
        detail["alternative"] = sorted(set(alts))[0]
    if ok_nodes:
        return True, "", detail
    reasons = sorted({v["reason"] for v in per.values() if not v.get("ok")})
    why = reasons[0] if reasons else "no eligible node in this fabric"
    msg = "%s is not valid on this fabric — %s" % (sc.get("label", sid), why)
    if detail.get("alternative"):
        msg += " (use '%s' instead on this node class)" % detail["alternative"]
    return False, msg, detail


def scenario_node_ok(sid, nid):
    """(ok, reason) for ONE concrete target — used inside a scenario body just
    before it acts, so a scenario can never claim success on a node it cannot
    actually drive."""
    sc = SCEN_BY_ID.get(sid) or {}
    req = sc.get("requires") or []
    classes = sc.get("node_classes") or ["any"]
    cls = _node_class(nid)
    acts = node_actions(nid)
    for aid in req:
        a = acts.get(aid) or {"supported": True}
        if not a.get("supported"):
            r = a.get("reason") or ("action '%s' is unsupported on %s" % (aid, nid))
            if a.get("alternative"):
                r += " — use '%s' on this platform instead" % a["alternative"]
            return False, r
    if "any" not in classes and cls not in classes:
        return False, ("%s is a '%s' node; this scenario is only valid on %s"
                       % (nid, cls, "/".join(classes)))
    return True, ""


def _active_list(probe=True):
    """The single source of truth for what is running, derived from concrete state.
    probe=False uses the cached flood liveness instead of re-running the SSH check."""
    act = set()
    if (_overload_running() if probe else _overload["running"]):
        act.add("surge")
    for sid in ("cablecut", "grayfail", "spinefail", "drain", "elephant", "badconfig", "storm"):
        if sid in _scen_state:
            act.add(sid)
    if "thermalguard" in _active:
        act.add("thermalguard")
    if _traffic_state:                       # traffic-studio flows (Phase A2)
        act.add("traffic:%s" % _traffic_state.get("pattern", "?"))
    csc = _collective_active()               # GPU collective (training/alltoall/straggler/sustained)
    if csc:
        act.add(csc)
    return sorted(act)


def scenarios_public(probe=True):
    """GET /api/scenarios payload — registry minus the callables + live state.
    Each entry now carries its capability verdict (available / unavailable_reason /
    requires / node_classes) so the UI can grey a button and print WHY, instead of
    letting an operator run a technique that cannot work on the bound hardware."""
    pub = []
    for sc in SCENARIOS:
        e = {k: sc[k] for k in ("id", "emoji", "label", "tip", "proves", "kind", "group")}
        try:
            avail, why, detail = scenario_capability(sc["id"])
        except Exception as ex:            # a gate bug must never hide a scenario
            avail, why, detail = True, "", {"error": str(ex)}
        e["available"] = bool(avail)
        e["unavailable_reason"] = "" if avail else why
        e["requires"] = sc.get("requires") or []
        e["node_classes"] = sc.get("node_classes") or ["any"]
        if detail.get("alternative"):
            e["alternative"] = detail["alternative"]
        if not avail:
            e["nodes"] = detail.get("nodes", {})
        pub.append(e)
    return {"scenarios": pub, "active": _active_list(probe=probe),
            "agent": {"enabled": _agent["enabled"], "health": _agent_health()}}


def scenario_reset():
    """Stop EVERYTHING and restore a clean fabric via configure-fabric.sh."""
    global _suppress_configure
    _agent_set_enabled(False)
    _suppress_configure = True
    try:
        for sc in SCENARIOS:
            try:
                sc["stop"]()
            except Exception as e:
                print("reset: %s stop error: %s" % (sc["id"], e), file=sys.stderr)
        overload_control("stop")
        _traffic_stop_all()         # Phase A2: kill any traffic-studio -s 1300 flows
        _kill_all_generated()       # belt-and-braces: kill ANY generated flow (-i 0.003) on leaves+access
        ambient_control("stop")     # cycle the baseline; restarted after reconfigure
    finally:
        _suppress_configure = False
    _scen_state.clear()
    _active.clear()
    # belt-and-braces: unconditionally clear graceful-shutdown / the drain route-map
    # on EVERY spine, whatever our in-memory state thinks. This recovers an orphaned
    # drain from a disable race and any drain state lost across a server restart —
    # configure-fabric.sh re-applies BGP config additively and may not negate it,
    # so reset owns the negation. Idempotent + guarded; no-op when nothing is drained.
    for sp in SPINES:
        try:
            _spine_graceful(sp, False)
        except Exception as e:
            print("reset: clear drain on %s error: %s" % (sp, e), file=sys.stderr)
    # belt-and-braces: sweep any orphaned storm sidecars/qdiscs off every leaf too.
    try:
        _storm_teardown_all()
    except Exception as e:
        print("reset: storm sweep error: %s" % e, file=sys.stderr)
    ok, msg = _run_configure()
    # resume the baseline hum so the map stays alive post-reset (best-effort; the
    # lazy first-poll re-arm self-heals if this SSH happens to fail)
    try:
        ambient_control("start")
    except Exception as e:
        print("reset: ambient restart error: %s" % e, file=sys.stderr)
    return {"ok": ok, "active": _active_list(), "msg": "reset — stopped all scenarios; " + msg}


def scenario_control(sid, action):
    """POST /api/scenario dispatcher. Every path returns the API contract dict."""
    with _scen_lock:
        if action == "reset":
            return scenario_reset()
        sc = SCEN_BY_ID.get(sid)
        if not sc:
            return {"ok": False, "active": _active_list(), "msg": "unknown scenario '%s'" % sid}
        active = set(_active_list())
        try:
            if action == "start":
                if sid in active:
                    return {"ok": True, "active": _active_list(), "msg": "%s already active" % sid}
                # capability pre-flight — refuse BEFORE executing anything, and say
                # why, rather than running a technique that silently does nothing.
                avail, why, detail = scenario_capability(sid)
                if not avail:
                    return {"ok": False, "active": _active_list(), "msg": why,
                            "unsupported": True, "reason": why,
                            "requires": sc.get("requires") or [],
                            "node_classes": sc.get("node_classes") or ["any"],
                            "alternative": detail.get("alternative"),
                            "nodes": detail.get("nodes", {})}
                ok, msg = sc["start"]()
                if ok and sid == "thermalguard":
                    _active.add("thermalguard")
                return {"ok": ok, "active": _active_list(), "msg": msg}
            if action == "stop":
                ok, msg = sc["stop"]()
                _active.discard(sid)
                return {"ok": ok, "active": _active_list(), "msg": msg}
        except Exception as e:   # a scenario must never wedge the server
            return {"ok": False, "active": _active_list(), "msg": "error in %s %s: %s" % (sid, action, e)}
        return {"ok": False, "active": _active_list(), "msg": "unknown action '%s'" % action}


# ═══════════════════════════════════════════════════════════════════════════════
#  INITIAL FABRIC LOAD — populate every fabric-derived global once, at import,
#  now that every helper (_flood_cmds, _configure_sh_path, ...) is defined.
#  reset_state=False: there is no scenario/agent state to reset on first load.
# ═══════════════════════════════════════════════════════════════════════════════
reload_fabric(reset_state=False)


# ═══════════════════════════════════════════════════════════════════════════════
#  FABRIC ARCHITECT — in-UI live rebuild (preview + apply orchestrator).
#  Preview runs fabgen into a THROWAWAY temp dir (never the live build). Apply
#  spawns ONE background thread running rebuild-fabric.sh, maps its @@STEP/@@ERROR
#  markers to a state machine the UI polls, then reload_fabric()s so the next
#  /api/topology shows the new fabric. Single-flight + gated by DEPLOY_MAX_SWITCHES.
# ═══════════════════════════════════════════════════════════════════════════════
# state the apply thread writes and /api/fabric/status reads
_rebuild = {
    "state": "idle",       # idle|generating|deploying|configuring|reloading|ready|error
    "step_msg": "",
    "params": {},
    "started": 0.0,
    "log": [],             # captured stdout lines (tail kept)
    "error": "",
}
_rebuild_lock = threading.Lock()
# states that mean a rebuild is genuinely in-flight (single-flight guard)
_REBUILD_BUSY = ("generating", "deploying", "configuring", "reloading")
# best-effort progress % per state (honest, monotonic)
_REBUILD_PCT = {"idle": 0, "generating": 15, "deploying": 55,
                "configuring": 85, "reloading": 95, "ready": 100, "error": 100}


def _rebuild_set(state, step_msg=None, error=None):
    with _rebuild_lock:
        _rebuild["state"] = state
        if step_msg is not None:
            _rebuild["step_msg"] = step_msg
        if error is not None:
            _rebuild["error"] = error
    print("[rebuild:%s] %s" % (state, step_msg or error or ""), file=sys.stderr)


def _rebuild_log(line):
    with _rebuild_lock:
        _rebuild["log"].append(line)
        if len(_rebuild["log"]) > 200:
            _rebuild["log"] = _rebuild["log"][-200:]


# markers rebuild-fabric.sh emits on their own line -> (state, human step message)
_STEP_STATE = {
    "generating": ("generating", "Generating fabric definition…"),
    "deploying": ("deploying", "Deploying switches (containerlab --reconfigure)…"),
    "configuring": ("configuring", "Configuring cables, BGP, ECMP, hosts…"),
    "done": ("reloading", "Deploy complete — reloading the new fabric…"),
}


def _validate_dims(spines, leaves, hosts):
    """Clamp/validate the three dims to fabgen's hard ranges. Returns (tuple|None, err)."""
    try:
        s, l, h = int(spines), int(leaves), int(hosts)
    except (TypeError, ValueError):
        return None, "spines, leaves and hosts must be whole numbers"
    if not (FAB_MIN["spines"] <= s <= FAB_MAX["spines"]):
        return None, "spines must be %d..%d" % (FAB_MIN["spines"], FAB_MAX["spines"])
    if not (FAB_MIN["leaves"] <= l <= FAB_MAX["leaves"]):
        return None, "leaves must be %d..%d" % (FAB_MIN["leaves"], FAB_MAX["leaves"])
    if not (FAB_MIN["hosts_per_leaf"] <= h <= FAB_MAX["hosts_per_leaf"]):
        return None, "hosts/leaf must be %d..%d" % (FAB_MIN["hosts_per_leaf"], FAB_MAX["hosts_per_leaf"])
    return (s, l, h), None


def _est_ram_gb(s, l, h):
    """~1.5 GB per sonic-vs switch + ~0.05 GB per host container (matches fabgen)."""
    return round((s + l) * 1.5 + (l * h) * 0.05, 2)


def fabric_limits():
    """GET /api/fabric/limits payload."""
    with _fab_lock:
        cur = {"spines": PARAMS.get("spines"), "leaves": PARAMS.get("leaves"),
               "hosts_per_leaf": PARAMS.get("hosts_per_leaf")}
        mode = EXEC.get("mode", "docker")
        leaf_radix = (FAB_HW.get("leaf") or {}).get("radix", DEFAULT_RADIX)
        spine_radix = (FAB_HW.get("spine") or {}).get("radix", DEFAULT_RADIX)
    return {"max_switches": DEPLOY_MAX_SWITCHES, "current": cur, "exec_mode": mode,
            "min": dict(FAB_MIN), "max": dict(FAB_MAX),
            "leaf_radix": leaf_radix, "spine_radix": spine_radix}


def fabric_hardware():
    """GET /api/fabric/hardware payload — the catalog, the defaults, and what the
    live fabric.json currently models (null when it predates hardware)."""
    with _fab_lock:
        hw = FAB_HW
    cur_leaf = (hw.get("leaf") or {}).get("id")
    cur_spine = (hw.get("spine") or {}).get("id")
    breakout = hw.get("breakout", DEFAULT_BREAKOUT)
    return {"catalog": [hw_model(m["id"]) for m in HARDWARE],
            "defaults": {"leaf": DEFAULT_LEAF_MODEL, "spine": DEFAULT_SPINE_MODEL},
            "current": {"leaf": cur_leaf, "spine": cur_spine, "breakout": breakout}}


def _resolve_models(leaf_model, spine_model):
    """Fill in leaf/spine model ids from request → live fabric → defaults, and
    validate them. Returns (leaf_id, spine_id, err)."""
    lm = leaf_model or (FAB_HW.get("leaf") or {}).get("id") or DEFAULT_LEAF_MODEL
    sm = spine_model or (FAB_HW.get("spine") or {}).get("id") or DEFAULT_SPINE_MODEL
    if lm not in HW_BY_ID:
        return None, None, "unknown leaf_model %r" % lm
    if sm not in HW_BY_ID:
        return None, None, "unknown spine_model %r" % sm
    return lm, sm, None


def fabric_preview(spines, leaves, hosts, leaf_model=None, spine_model=None, mode="ai"):
    """POST /api/fabric/preview — run fabgen into a TEMP dir (never the live build),
    parse the emitted fabric.json, return the proposed topology PLUS the
    authoritative capacity object. Does NOT deploy. leaf_model/spine_model select
    the MODELED hardware; `mode` ('general'|'ai') sets the oversubscription target."""
    dims, err = _validate_dims(spines, leaves, hosts)
    if err:
        return {"ok": False, "msg": err}
    s, l, h = dims
    with _fab_lock:
        exec_mode = EXEC.get("mode", "docker")
        leaf_id, spine_id, herr = _resolve_models(leaf_model, spine_model)
        breakout = FAB_HW.get("breakout", DEFAULT_BREAKOUT)
    if herr:
        return {"ok": False, "msg": herr}
    capmode = "ai" if str(mode).lower() == "ai" else "general"
    Rl, Rs = HW_BY_ID[leaf_id]["radix"], HW_BY_ID[spine_id]["radix"]
    leaf_speed, spine_speed = HW_BY_ID[leaf_id]["port_speed_gbps"], HW_BY_ID[spine_id]["port_speed_gbps"]

    # capacity is pure math — always authoritative, even when the shape is infeasible
    cap = compute_capacity(s, l, h, Rl, Rs, leaf_speed, spine_speed, breakout, capmode)
    est = _est_ram_gb(s, l, h)
    switches = s + l

    # radix feasibility: an S×L 2-tier fabric is IMPOSSIBLE when L exceeds the spine
    # radix (no port to land the leaf). Report clearly; fabgen would also reject it.
    if l > Rs:
        return {"ok": True, "feasible": False, "capacity": cap,
                "switches": switches, "est_ram_gb": est,
                "leaf_model": hw_model(leaf_id), "spine_model": hw_model(spine_id),
                "msg": "leaves L=%d exceeds spine radix %d (%s) — impossible as a flat 2-tier; go 3-tier"
                       % (l, Rs, spine_id)}

    tmp = tempfile.mkdtemp(prefix="lab-preview-")
    try:
        cmd = [sys.executable, FABGEN, "--spines", str(s), "--leaves", str(l),
               "--hosts-per-leaf", str(h), "--name", "lab", "--exec-mode", exec_mode,
               "--leaf-model", leaf_id, "--spine-model", spine_id, "--out", tmp]
        rc, out, errout = _run(cmd, timeout=60)
        if rc != 0:
            return {"ok": False, "msg": "fabgen failed: %s" % (errout or out).strip()[-160:],
                    "capacity": cap}
        with open(os.path.join(tmp, "fabric.json"), "r", encoding="utf-8") as f:
            fab = json.load(f)
    except Exception as e:
        return {"ok": False, "msg": "preview error: %s" % e, "capacity": cap}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # same node/link shape as /api/topology, minus any live metrics
    nodes = [dict(n) for n in fab["nodes"]]
    links = [{**L, "bps": 0.0, "active": False} for L in fab["links"]]
    feasible = switches <= DEPLOY_MAX_SWITCHES
    if feasible:
        msg = "%dx%d = %d switches, ~%.1f GB — deployable on this host." % (s, l, switches, est)
    else:
        msg = ("%dx%d = %d switches needs ~%.1f GB — over this host's limit of %d. "
               "Preview only. To deploy it, run ./run.sh virtual S L H on a machine with more memory." % (s, l, switches, est, DEPLOY_MAX_SWITCHES))
    return {"ok": True,
            "topology": {"nodes": nodes, "links": links, "params": fab.get("params", {}),
                         "hardware": fab.get("hardware", {})},
            "switches": switches, "links": len(links), "est_ram_gb": est,
            "leaf_model": hw_model(leaf_id), "spine_model": hw_model(spine_id),
            "capacity": cap, "feasible": feasible, "msg": msg}


def _rebuild_worker(s, l, h, mode, leaf_model=DEFAULT_LEAF_MODEL, spine_model=DEFAULT_SPINE_MODEL):
    """The apply orchestrator thread: run rebuild-fabric.sh, map its markers to the
    state machine, then reload_fabric() so the next poll shows the new fabric.
    Exception-safe: any failure lands the state machine in 'error'."""
    argv = ["bash", REBUILD_CMD, str(s), str(l), str(h), mode, leaf_model, spine_model]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as e:
        _rebuild_set("error", error="could not launch rebuild-fabric.sh: %s" % e)
        return
    saw_error = None
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line:
            _rebuild_log(line)
        if line.startswith("@@STEP "):
            step = line[len("@@STEP "):].strip()
            mapped = _STEP_STATE.get(step)
            if mapped:
                _rebuild_set(mapped[0], step_msg=mapped[1])
        elif line.startswith("@@ERROR"):
            saw_error = line[len("@@ERROR"):].strip() or "rebuild failed"
            _rebuild_set("error", error=saw_error)
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    rc = proc.returncode
    if saw_error is not None:
        return  # already in error state
    if rc != 0:
        _rebuild_set("error", error="rebuild-fabric.sh exited rc=%s" % rc)
        return
    # @@STEP done was seen -> state is 'reloading'; re-read the new fabric.json
    _rebuild_set("reloading", step_msg="Reloading the new fabric…")
    try:
        reload_fabric(reset_state=True)
    except (Exception, SystemExit) as e:
        # load_fabric() sys.exit()s (SystemExit, NOT an Exception subclass) when the
        # freshly-written fabric.json is unreadable/broken. Catch it too, or the daemon
        # thread would die silently leaving state stuck at 'reloading' (in _REBUILD_BUSY)
        # and wedge every future apply behind the single-flight guard.
        _rebuild_set("error", error="deploy ok but reload failed: %s" % e)
        return
    _rebuild_set("ready", step_msg="Fabric is now %dx%d — live." % (s, l))


def fabric_apply(spines, leaves, hosts, leaf_model=None, spine_model=None, mode=None):
    """POST /api/fabric/apply — validate + gate, then spawn ONE rebuild thread.
    leaf_model/spine_model select the MODELED hardware (passed through to fabgen).
    `mode` is accepted for request-shape symmetry with preview and ignored here."""
    if EXEC.get("mode") == "ssh":
        # HARD GUARD: in ssh mode the switches are real (or managed by something else), not
        # built by rebuild-fabric.sh. Running it would rewrite fabric.json out of sync with
        # them and break all telemetry and scenarios. Preview stays available.
        return {"ok": False, "msg": "Apply & Deploy is disabled in ssh mode: these switches "
                "aren't built by this lab, so they can't be rebuilt from here. Preview still works."}
    dims, err = _validate_dims(spines, leaves, hosts)
    if err:
        return {"ok": False, "msg": err}
    s, l, h = dims
    switches = s + l
    if switches > DEPLOY_MAX_SWITCHES:
        return {"ok": False,
                "msg": "%dx%d = %d switches needs ~%.1f GB — over this host's limit of %d. "
                       "Preview works. To deploy it, run ./run.sh virtual S L H on a machine with more memory."
                       % (s, l, switches, _est_ram_gb(s, l, h), DEPLOY_MAX_SWITCHES)}
    with _fab_lock:
        exec_mode = EXEC.get("mode", "docker")
        leaf_id, spine_id, herr = _resolve_models(leaf_model, spine_model)
    if herr:
        return {"ok": False, "msg": herr}
    # radix guard: refuse an impossible 2-tier before spawning a rebuild
    if l > HW_BY_ID[spine_id]["radix"]:
        return {"ok": False,
                "msg": "leaves L=%d exceeds spine radix %d (%s) — impossible as a flat 2-tier; go 3-tier"
                       % (l, HW_BY_ID[spine_id]["radix"], spine_id)}
    with _rebuild_lock:
        if _rebuild["state"] in _REBUILD_BUSY:
            return {"ok": False, "msg": "a rebuild is already in progress"}
        # (re)arm for a fresh run — clears a previous 'ready'/'error'
        _rebuild["state"] = "generating"
        _rebuild["step_msg"] = "Starting rebuild…"
        _rebuild["error"] = ""
        _rebuild["log"] = []
        _rebuild["started"] = time.time()
        _rebuild["params"] = {"spines": s, "leaves": l, "hosts_per_leaf": h,
                              "leaf_model": leaf_id, "spine_model": spine_id}
    threading.Thread(target=_rebuild_worker, args=(s, l, h, exec_mode, leaf_id, spine_id),
                     name="fabric-rebuild", daemon=True).start()
    return {"ok": True, "started": True}


def fabric_status():
    """GET /api/fabric/status payload."""
    with _rebuild_lock:
        state = _rebuild["state"]
        # in the error state the failure message is what matters; otherwise the step
        step_msg = _rebuild["error"] if state == "error" else _rebuild["step_msg"]
        step_msg = step_msg or _rebuild["error"] or _rebuild["step_msg"]
        params = dict(_rebuild["params"])
        started = _rebuild["started"]
        log_tail = list(_rebuild["log"][-12:])
    elapsed = round(time.time() - started, 1) if started else 0.0
    return {"state": state, "step_msg": step_msg, "params": params,
            "pct": _REBUILD_PCT.get(state, 0),
            "elapsed_s": elapsed, "log_tail": log_tail}


# ─── fallback HTML page (used only if ui.html is missing next to this file) ──
# ═══════════════════════════════════════════════════════════════════════════════
#  HARDWARE-READY NODE BINDING — the per-node/per-metric provenance surface.
#  The UI badges a metric from what arrives here; it never has to know the words
#  "thermalctld", "Tomahawk" or "counterpoll" — those arrive as `note` strings and
#  are displayed verbatim. This is the schema the one-click swap will later flip;
#  the swap itself is deliberately NOT implemented here.
# ═══════════════════════════════════════════════════════════════════════════════
# Which signals are synthesized from real load on an emulated node, and which are
# genuinely measured. On a PHYSICAL node nothing is modeled — a field is either a
# real reading or ABSENT.
_MODELED_METRICS = {"asic_c", "cpu_c", "inlet_c", "outlet_c",
                    "fan_rpm", "power_w", "psu_in_w", "ecn_ps"}
_MEASURED_METRICS = {"tx_bps", "load_bps", "drops_ps", "intensity",
                     "bgp_peers", "fib_vias", "ecn_marks"}
_PROV_METRICS = sorted(_MODELED_METRICS | _MEASURED_METRICS)


def metric_provenance(nid):
    """{metric: {prov, signal, note}} for one node — the per-metric badge source.

    prov vocabulary matches studio.html's chip system:
      real | gnmi | modeled | realzero | null | absent
    (`realzero` and `null` are decided by the VALUE, client-side; this call fixes
    the structural half: what the source IS, and whether it exists at all.)
    """
    b = node_binding(nid)
    gone = declared_absent(nid)
    physical = bool(b.get("physical"))
    gnmi = b.get("transport") == "gnmi"
    out = {}
    for m in _PROV_METRICS:
        if m in gone:
            out[m] = {"prov": "absent", "signal": "absent", "note": gone[m]}
            continue
        if physical:
            prov = "gnmi" if gnmi else "real"
            note = ""
        elif m in _MODELED_METRICS:
            prov, note = "modeled", "synthesized by the exporter from REAL measured traffic"
        else:
            prov, note = "real", ""
        out[m] = {"prov": prov, "signal": "ok", "note": note}
    return out


def nodes_payload():
    """GET /api/nodes — every node's hardware binding, capability flags, per-metric
    provenance and per-action availability. Pure in-memory; no I/O."""
    maybe_reload()
    with _fab_lock:
        nodes = [dict(n) for n in TOPOLOGY_NODES]
        exec_mode = EXEC.get("mode", "docker")
    out = []
    for n in nodes:
        nid = n.get("id")
        if not nid:
            continue
        b = node_binding(nid)
        rec = {
            "id": nid,
            "kind": n.get("kind", ""),
            "site": b.get("site"),
            "platform": b.get("platform"),
            "nos": b.get("nos"),
            "transport": b.get("transport"),
            "physical": bool(b.get("physical")),
            "counter_source": b.get("counter_source"),
            "driver": b.get("driver"),
            "modeled_energy": bool(b.get("modeled_energy", True)),
            "capabilities": b.get("caps") or {},
            "endpoints": b.get("endpoints") or {},
            "class": _node_class(nid),
        }
        if n.get("kind") in SWITCH_KINDS:
            rec["absent"] = declared_absent(nid)
            rec["metrics"] = metric_provenance(nid)
            rec["actions"] = node_actions(nid)
        out.append(rec)
    return {
        "exec_mode": exec_mode,
        "spec_version": FABRIC.get("spec_version", 1),
        "capability_flags": list(_nc.CAP_FLAGS) if _nc is not None else [],
        "nodes": out,
        "physical_count": sum(1 for r in out if r["physical"]),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  ONE AUTHORITATIVE SNAPSHOT — the fix for the desync root cause.
#
#  The UI ran SEVEN independent poll loops (topology 3s, agent 3s, gpu 3s, overlay
#  15s, fleet 15s, features 20s, audit 30s) with no shared instant, so two panels
#  could legitimately show different values for the same quantity in the same
#  frame. One sampler thread now builds ONE consistent view from ONE build_context()
#  sample, stamps it with a monotonically increasing revision and a server clock,
#  and publishes it whole. `GET /api/snapshot` serves it; `?since=<rev>` long-polls
#  until the revision moves (SSE was deliberately not used:
#  it needs Handler._send bypassed and was not tested here).
#
#  Every pre-existing endpoint keeps working EXACTLY as before, so ui.html is
#  untouched; it simply is no longer the only thing driving the pipeline.
# ═══════════════════════════════════════════════════════════════════════════════
SNAPSHOT_ENABLED = os.environ.get("SNAPSHOT_ENABLED", "1").strip().lower() not in ("0", "false", "no")
SNAPSHOT_SLOW_PERIOD = float(os.environ.get("SNAPSHOT_SLOW_PERIOD", "15.0"))
SNAPSHOT_HOLD = float(os.environ.get("SNAPSHOT_HOLD", "25.0"))     # long-poll ceiling
SNAPSHOT_IDLE_S = float(os.environ.get("SNAPSHOT_IDLE_S", "90.0"))  # stop slow refresh when unread
SNAPSHOT_MAX_WAITERS = int(os.environ.get("SNAPSHOT_MAX_WAITERS", "48"))

_snap = {"rev": 0, "ts": 0.0, "data": None, "error": None,
         "build_ms": 0.0, "slow_ts": 0.0, "slow": {}, "started": 0.0, "ticks": 0}
_snap_cv = threading.Condition(threading.Lock())
_snap_clients = {"last_request": 0.0, "waiters": 0, "requests": 0}

_SLOW_SECTIONS = (
    ("overlay", lambda: build_overlay()),
    ("fleet", lambda: build_fleet()),
    ("features", lambda: build_features()),
    ("audit", lambda: build_audit()),
)


def _snapshot_slow(force=False):
    """Refresh the SSH-backed sections. These are already TTL-cached, and we only
    touch them when a client has actually read the snapshot recently — so adding
    the sampler puts ZERO extra SSH load on an idle demo box."""
    now = time.time()
    idle = (now - _snap_clients["last_request"]) > SNAPSHOT_IDLE_S
    if idle and not force:
        return dict(_snap["slow"]), True
    if not force and (now - _snap["slow_ts"]) < SNAPSHOT_SLOW_PERIOD:
        return dict(_snap["slow"]), False
    slow = dict(_snap["slow"])
    for name, fn in _SLOW_SECTIONS:
        try:
            slow[name] = fn()
        except Exception as e:
            slow[name] = {"error": "%s: %s" % (type(e).__name__, e)}
    _snap["slow"] = slow            # cache it: the next ticks reuse this until due
    _snap["slow_ts"] = now
    return slow, False


def _snapshot_build():
    """Build ONE consistent view. Everything below reads the SAME ctx sample."""
    t0 = time.time()
    ctx = build_context()
    # the EWMA baselines now advance here, on the server clock — not on whether a
    # browser tab happens to be open (which was the old, invisible failure).
    _update_anomalies(ctx.get("switches", {}))
    # An idle sampler must not put ANY new SSH on the fabric: with no reader we
    # keep the cheap Prometheus pass (which is what keeps the anomaly baselines
    # learning with the browser closed) and read cached liveness instead.
    probe = (time.time() - _snap_clients["last_request"]) <= SNAPSHOT_IDLE_S
    topo = build_topology(ctx=ctx, probe=probe)
    slow, slow_idle = _snapshot_slow()
    now = time.time()
    with _anom_lock:
        anom_skipped = {k: list(v) for k, v in _anom["skipped"].items()}
    agent = agent_state()
    payload = {
        "ts": now,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "period_s": SNAPSHOT_PERIOD,
        "fabric": {"name": FABRIC_NAME, "params": PARAMS, "path": os.path.basename(FABRIC_PATH),
                   "exec_mode": EXEC.get("mode", "docker")},
        "topology": topo,
        "context": {
            "switches": ctx.get("switches", {}),
            "hottest": ctx.get("hottest"),
            "total_tx_bps": ctx.get("total_tx_bps"),
            "offered_bps": ctx.get("offered_bps"),
            "notable": ctx.get("notable", []),
            "blind_thermal": ctx.get("blind_thermal", []),
            "gnmi": ctx.get("gnmi"),
            "brain": ctx.get("brain"),
            "note": ctx.get("note"),
        },
        "agent": agent,
        "anomalies": anomalies_payload(),
        "anomaly_skipped": anom_skipped,
        "scenarios": scenarios_public(probe=False),   # already probed above
        "traffic": _traffic_summary(),
        "collective": collective_summary(),
        "nodes": nodes_payload(),
        "slow": slow,
        "slow_idle": slow_idle,
    }
    try:
        payload["gpu"] = build_gpu()
    except Exception as e:
        payload["gpu"] = {"error": "%s: %s" % (type(e).__name__, e)}
    payload["health"] = {
        "sampler": {"period_s": SNAPSHOT_PERIOD, "ticks": _snap["ticks"],
                    "build_ms": round((time.time() - t0) * 1000.0, 1),
                    "probing": probe, "slow_idle": slow_idle},
        "agent": agent.get("health"),
        "collector": {"note": ctx.get("note")},
        # every node whose required signals are absent, fabric-wide — the single
        # place to look when something on screen says ABSENT.
        "absent": {nid: declared_absent(nid) for nid in sorted(BINDINGS)
                   if declared_absent(nid)},
    }
    payload["build_ms"] = round((time.time() - t0) * 1000.0, 1)
    return payload


def _snapshot_publish(payload, error=None):
    with _snap_cv:
        _snap["rev"] += 1
        _snap["ts"] = payload.get("ts", time.time()) if payload else time.time()
        _prev_ts = _snap.get("_prev_ts")
        _snap["_prev_ts"] = _snap["ts"]
        if _prev_ts:
            _iv = _snap["ts"] - _prev_ts
            # EWMA of the ACTUAL cycle time — a build does Prometheus + SSH work and
            # routinely takes far longer than SNAPSHOT_PERIOD, so staleness must be
            # judged against observed cadence, not the configured sleep.
            _snap["interval"] = _iv if not _snap.get("interval") else (0.7*_snap["interval"] + 0.3*_iv)

        _snap["error"] = error
        if payload is not None:
            payload["rev"] = _snap["rev"]
            _snap["data"] = payload
            _snap["build_ms"] = payload.get("build_ms", 0.0)
        _snap["ticks"] += 1
        _snap_cv.notify_all()


def _snapshot_loop():
    """The ONE sampler thread. Exception-safe: a bad cycle is recorded on the
    snapshot as an error and the loop keeps its cadence."""
    _snap["started"] = time.time()
    while True:
        try:
            payload = _snapshot_build()
            _snapshot_publish(payload)
        except Exception as e:
            try:
                print("snapshot sampler error (continuing): %s" % e, file=sys.stderr)
                _snapshot_publish(None, error="%s: %s" % (type(e).__name__, e))
            except Exception:
                pass
        time.sleep(SNAPSHOT_PERIOD)


def snapshot_payload(since=None, sections=None, wait=True):
    """The /api/snapshot response.

    since=<rev>  long-poll: block (≤ SNAPSHOT_HOLD) until rev > since, then return
                 the new snapshot; on timeout return {rev, unchanged: true} so the
                 client can distinguish "nothing moved" from "server is dead".
    sections=... comma-separated subset of the top-level keys.
    """
    deadline = time.time() + SNAPSHOT_HOLD
    with _snap_cv:
        _snap_clients["last_request"] = time.time()
        _snap_clients["requests"] += 1
        if since is not None and wait and _snap["data"] is not None:
            if _snap_clients["waiters"] < SNAPSHOT_MAX_WAITERS:
                _snap_clients["waiters"] += 1
                try:
                    while _snap["rev"] <= since:
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            return {"rev": _snap["rev"], "ts": _snap["ts"],
                                    "unchanged": True,
                                    "age_s": round(time.time() - _snap["ts"], 3)}
                        _snap_cv.wait(min(remaining, 1.0))
                finally:
                    _snap_clients["waiters"] -= 1
        data = _snap["data"]
        rev, ts, err = _snap["rev"], _snap["ts"], _snap["error"]
    if data is None:
        return {"rev": rev, "ts": ts, "ready": False, "error": err,
                "msg": "snapshot sampler has not produced a sample yet"
                       if SNAPSHOT_ENABLED else "snapshot sampler is disabled",
                "enabled": SNAPSHOT_ENABLED}
    out = dict(data)
    out["rev"] = rev
    out["age_s"] = round(time.time() - ts, 3)
    _obs = _snap.get("interval") or SNAPSHOT_PERIOD
    out["interval_s"] = round(_obs, 2)
    # stale = we have genuinely missed cycles, not merely "a build is slow"
    out["stale"] = out["age_s"] > max(6.0, _obs * 3.0)
    out["ready"] = True
    if err:
        out["error"] = err
    if sections:
        keep = {x.strip() for x in sections.split(",") if x.strip()}
        keep |= {"rev", "ts", "age_s", "stale", "ready", "generated"}
        out = {k: v for k, v in out.items() if k in keep}
    return out



PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Energy Lab assistant</title>
<style>
  :root{--bg:#0b0e14;--panel:#141922;--panel2:#1b2130;--edge:#232b3a;
        --fg:#c8d2e0;--dim:#7d8aa0;--accent:#3bb7f6;--green:#3ecf8e;--amber:#f5c04a;--red:#f0645f;}
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;font-size:14px}
  .num{font-family:"SF Mono",Menlo,Consolas,monospace}
  #app{display:flex;flex-direction:column;height:100vh;max-width:900px;margin:0 auto}
  #strip{display:flex;gap:10px;padding:10px 14px;background:var(--panel);
    border-bottom:1px solid var(--edge);align-items:stretch;flex-wrap:wrap}
  .stat{background:var(--panel2);border:1px solid var(--edge);border-radius:8px;
    padding:8px 12px;min-width:150px;flex:1}
  .stat .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
  .stat .v{font-size:22px;margin-top:3px}
  #log{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
  .msg{max-width:80%;padding:9px 13px;border-radius:12px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word}
  .me{align-self:flex-end;background:var(--accent);color:#04121c;border-bottom-right-radius:3px}
  .bot{align-self:flex-start;background:var(--panel2);border:1px solid var(--edge);border-bottom-left-radius:3px}
  .bot.think{color:var(--dim);font-style:italic}
  #bar{display:flex;gap:8px;padding:12px 14px;border-top:1px solid var(--edge);background:var(--panel)}
  #q{flex:1;background:var(--panel2);border:1px solid var(--edge);color:var(--fg);
    border-radius:8px;padding:10px 12px;font-size:14px;outline:none}
  #sendbtn{background:var(--accent);color:#04121c;border:none;border-radius:8px;
    padding:0 18px;font-weight:600;cursor:pointer}
  .foot{color:var(--dim);font-size:11px;padding:0 14px 8px}
</style></head><body>
<div id="app">
  <div id="strip">
    <div class="stat"><div class="k">Hottest ASIC</div><div class="v num" id="s-hot">—</div></div>
    <div class="stat"><div class="k">Fabric throughput</div><div class="v num" id="s-tx">—</div></div>
    <div class="stat"><div class="k">Switches</div><div class="v num" id="s-n">—</div></div>
    <div class="stat"><div class="k">🧠 Brain</div><div class="v num" id="s-brain" style="font-size:13px">—</div></div>
  </div>
  <div id="log"></div>
  <div id="bar">
    <input id="q" placeholder="Ask about the fabric…" autocomplete="off">
    <button id="sendbtn">Send</button>
  </div>
  <div class="foot">ui.html not found next to app.py — minimal chat fallback. Heat &amp; power are <b>modeled</b> from real fabric traffic.</div>
</div>
<script>
var chatHist = [];
function fmtBps(v){ v=v||0; var u=['bps','Kbps','Mbps','Gbps'];
  for(var i=0;i<u.length;i++){ if(v<1000) return v.toFixed(1)+' '+u[i]; v/=1000; }
  return v.toFixed(1)+' Tbps'; }
function refresh(){
  fetch('/api/context').then(function(r){return r.json();}).then(function(ctx){
    var h = ctx.hottest || {};
    document.getElementById('s-n').textContent = Object.keys(ctx.switches||{}).length;
    document.getElementById('s-hot').textContent = h.name ? (h.asic_c.toFixed(0)+'°C '+h.name) : '—';
    document.getElementById('s-tx').textContent = fmtBps(ctx.total_tx_bps||0);
    if(ctx.brain){ document.getElementById('s-brain').textContent = ctx.brain; }
  }).catch(function(){});
}
function add(text, who){
  var d=document.createElement('div'); d.className='msg '+who; d.textContent=text;
  var log=document.getElementById('log'); log.appendChild(d); log.scrollTop=log.scrollHeight;
  return d;
}
function sendMsg(){
  var q=document.getElementById('q'); var text=q.value.trim(); if(!text) return;
  q.value=''; add(text,'me'); chatHist.push({role:'user',content:text});
  var btn=document.getElementById('sendbtn'); btn.disabled=true;
  var think=add('thinking…','bot think');
  fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({question:text,history:chatHist.slice(0,-1)})})
   .then(function(r){ if(!r.ok) throw new Error('HTTP '+r.status); return r.json(); })
   .then(function(j){ think.remove(); var a=j.answer||'(no answer)';
     add(a,'bot'); chatHist.push({role:'assistant',content:a}); })
   .catch(function(err){ think.remove(); add('backend error: '+err.message,'bot'); })
   .finally(function(){ btn.disabled=false; q.focus(); });
}
document.getElementById('sendbtn').addEventListener('click', sendMsg);
document.getElementById('q').addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); sendMsg(); } });
refresh(); setInterval(refresh, 4000);
</script>
</body></html>
"""


# ─── HTTP server ──────────────────────────────────────────────────────────────
def _host_name(header):
    """'localhost:9300' -> 'localhost', '[::1]:9300' -> '::1'."""
    h = (header or "").strip().lower()
    if h.startswith("["):
        return h[1:].split("]", 1)[0]
    return h.split(":", 1)[0]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60          # seconds; a stalled client must not hold a thread forever

    def log_message(self, fmt, *args):
        sys.stderr.write("[req] " + (fmt % args) + "\n")

    def _allowed(self):
        """Gate for every request; sends the error itself and returns False when refused.
        Without a token, only requests addressed to this machine get in (Host: localhost,
        127.0.0.1 or [::1]), which also stops DNS-rebinding pages. With COPILOT_TOKEN set,
        the token must arrive as ?token=, as the X-Lab-Token header, or as the cookie the
        page sets after the first visit."""
        self._set_cookie = False
        if not TOKEN:
            if _host_name(self.headers.get("Host")) in LOCAL_NAMES:
                return True
            self._send(403, json.dumps({"error": "this lab only answers on localhost"}))
            return False
        if self.path.split("?", 1)[0] == "/healthz":
            return True
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        cookies = http.cookies.SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except http.cookies.CookieError:
            pass
        from_query = (q.get("token") or [""])[0]
        given = (from_query or self.headers.get("X-Lab-Token", "")
                 or (cookies["lab_token"].value if "lab_token" in cookies else ""))
        if hmac.compare_digest(given.encode("utf-8"), TOKEN.encode("utf-8")):
            self._set_cookie = bool(from_query)
            return True
        self._send(401, json.dumps({"error": "open the page once as /?token=<COPILOT_TOKEN>"}))
        return False

    def _read_json(self):
        """The POST body as a dict, or None once an error has been sent. Requiring
        application/json also blocks other web pages: a browser can't send it to another
        site without a CORS preflight, and this server never approves one."""
        ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self._send(415, json.dumps({"error": "send Content-Type: application/json"}))
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._send(413, json.dumps({"error": "missing, bad or too large Content-Length"}))
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        return payload if isinstance(payload, dict) else {}

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("X-Content-Type-Options", "nosniff")
        if getattr(self, "_set_cookie", False):
            self.send_header("Set-Cookie", "lab_token=%s; HttpOnly; SameSite=Strict; Path=/" % TOKEN)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.close_connection = True   # avoid HTTP/1.1 keep-alive read-blocking in the browser
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._allowed():
            return
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            # Command-center UI lives in ui.html next to this file (re-read every
            # request so edits show on refresh). Falls back to the built-in page.
            ui = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")
            try:
                with open(ui, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/studio":
            # Second UI (Operations view) — built alongside the live ui.html so the
            # working demo is never at risk. Flip "/" to this once it reaches parity.
            st = os.path.join(os.path.dirname(os.path.abspath(__file__)), "studio.html")
            try:
                with open(st, "r", encoding="utf-8") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(404, "studio.html not found", "text/plain")
        if path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if path == "/api/snapshot":
            # ONE authoritative, revision-stamped view. `?since=<rev>` long-polls.
            # Reading it performs NO I/O — the sampler thread owns all collection.
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                since = int(q.get("since", [""])[0])
            except (TypeError, ValueError):
                since = None
            sections = (q.get("section") or q.get("sections") or [None])[0]
            wait = (q.get("wait", ["1"])[0].lower() not in ("0", "false", "no"))
            return self._send(200, json.dumps(
                snapshot_payload(since=since, sections=sections, wait=wait)))
        if path == "/api/nodes":
            return self._send(200, json.dumps(nodes_payload()))
        if path == "/api/context":
            return self._send(200, json.dumps(build_context()))
        if path == "/api/topology":
            return self._send(200, json.dumps(build_topology()))
        if path == "/api/overlay":
            return self._send(200, json.dumps(build_overlay()))
        if path == "/api/audit":
            return self._send(200, json.dumps(build_audit()))
        if path.startswith("/api/switch/"):     # per-switch drilldown (prefix match)
            sid = urllib.parse.unquote(path[len("/api/switch/"):])
            with _fab_lock:
                k = KIND.get(sid)
            if k not in ("spine", "leaf", "access"):
                return self._send(404, json.dumps({"error": "unknown switch"}))
            return self._send(200, json.dumps(build_switch_detail(sid)))
        if path == "/api/fleet":
            return self._send(200, json.dumps(build_fleet()))
        if path == "/api/features":
            return self._send(200, json.dumps(build_features()))
        if path == "/api/gpu":
            return self._send(200, json.dumps(build_gpu()))
        if path == "/api/rtt":
            return self._send(200, json.dumps(build_rtt()))
        if path == "/api/scenarios":
            return self._send(200, json.dumps(scenarios_public()))
        if path == "/api/agent":
            return self._send(200, json.dumps(agent_state()))
        if path == "/api/anomalies":
            return self._send(200, json.dumps(anomalies_payload()))
        if path == "/api/fabric/limits":
            return self._send(200, json.dumps(fabric_limits()))
        if path == "/api/fabric/hardware":
            return self._send(200, json.dumps(fabric_hardware()))
        if path == "/api/fabric/status":
            return self._send(200, json.dumps(fabric_status()))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not self._allowed():
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            return self._post(self.path.split("?", 1)[0], payload)
        except (TypeError, ValueError, KeyError, AttributeError, OverflowError) as e:
            # a field of the wrong type (e.g. {"id": []}) used to drop the connection unanswered
            print("bad request %s: %r" % (self.path, e), file=sys.stderr)
            return self._send(400, json.dumps({"error": "bad request (%s)" % type(e).__name__}))

    def _post(self, path, payload):
        if path == "/api/overload":
            return self._send(200, json.dumps(overload_control(payload.get("action", ""))))
        if path == "/api/scenario":
            return self._send(200, json.dumps(
                scenario_control(payload.get("id", ""), payload.get("action", ""))))
        if path == "/api/traffic":
            return self._send(200, json.dumps(traffic_control(
                payload.get("pattern"), payload.get("src"), payload.get("dst"),
                payload.get("action", "start"), payload.get("rate", "high"))))
        if path in ("/api/fabric/preview", "/api/fabric/apply"):
            fn = fabric_preview if path.endswith("preview") else fabric_apply
            return self._send(200, json.dumps(
                fn(payload.get("spines"), payload.get("leaves"), payload.get("hosts"),
                   payload.get("leaf_model"), payload.get("spine_model"),
                   payload.get("mode", "ai"))))
        if path == "/api/agent":
            enabled = bool(payload.get("enabled"))
            _agent_set_enabled(enabled)
            return self._send(200, json.dumps(
                {"ok": True, "enabled": _agent["enabled"],
                 "msg": "auto-heal agent " + ("enabled" if enabled else "disabled")}))
        if path != "/api/chat":
            return self._send(404, json.dumps({"error": "not found"}))
        question = payload.get("question", "")
        history = payload.get("history", [])
        ctx = with_wiring(build_context())
        try:
            ans = answer(question, ctx, history)
        except Exception as e:  # belt-and-braces; answer() already guards
            print("chat error:", e, file=sys.stderr)
            ans = offline_answer(question, ctx) + "  (offline mode)"
        return self._send(200, json.dumps({"answer": ans}))


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    # Check the bind address before anything starts (no threads, no traffic).
    if ":" in BIND:
        sys.exit("COPILOT_BIND=%s: the Command Center serves IPv4 only. Use 127.0.0.1 (the default), "
                 "or 0.0.0.0 together with COPILOT_TOKEN." % BIND)
    if BIND not in LOCAL_NAMES and not TOKEN:
        sys.exit("COPILOT_BIND=%s would let anyone on the network drive this lab. Also set "
                 "COPILOT_TOKEN to a long random string, or keep the default and use an SSH tunnel."
                 % BIND)
    # the ACT agent daemon — started once, gated by _agent["enabled"] (default OFF)
    threading.Thread(target=_agent_loop, name="act-agent", daemon=True).start()
    # the ONE snapshot sampler: builds a consistent, revision-stamped view on the
    # server clock. It also owns the anomaly EWMA tick, so the baselines keep
    # learning when the browser tab is closed (they used to stop dead).
    if SNAPSHOT_ENABLED:
        threading.Thread(target=_snapshot_loop, name="snapshot", daemon=True).start()
    # one-shot best-effort ambient bootstrap so the baseline hum is live on boot; a
    # slow/failed SSH must never block serve_forever (wrapped + daemonized).
    def _ambient_bootstrap():
        try:
            ambient_control("start")
        except Exception as e:
            print("ambient bootstrap error:", e, file=sys.stderr)
    threading.Thread(target=_ambient_bootstrap, name="ambient-boot", daemon=True).start()
    # one-shot orphan sweep: kill any generated flows (-i 0.003) left over from a prior
    # session so a fresh Command Center start sits at the ambient baseline, not silently
    # hot with the buttons reading idle. Daemonized so slow SSH never blocks startup.
    def _orphan_sweep():
        try:
            k = _kill_all_generated()
            print("startup: swept generated-traffic orphans on %d switch(es)" % k, file=sys.stderr)
        except Exception as e:
            print("startup orphan sweep error:", e, file=sys.stderr)
        try:
            _write_gpu_state()     # publish a clean (training-off) state for the exporter
        except Exception as e:
            print("startup gpu_state error:", e, file=sys.stderr)
    threading.Thread(target=_orphan_sweep, name="orphan-sweep", daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    print("Energy Lab Command Center on %s:%d%s  (fabric=%s %s, brain=%s, prom=%s, exec=%s, configure=%s)"
          % (BIND, PORT, " (token required)" if TOKEN else "", FABRIC_NAME, FABRIC_PATH, BRAIN,
             PROM_URL, EXEC.get("mode", "docker"), CONFIGURE_SH))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
