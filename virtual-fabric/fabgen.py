#!/usr/bin/env python3
"""fabgen.py — the fabric generator for Energy Lab for SONiC.

Generates a parameterized SONiC leaf-spine data center from three numbers
(spines / leaves / hosts-per-leaf) into ONE source of truth — fabric.json —
plus everything needed to deploy it:

    build/
      fabric.json           source of truth (exporter + copilot consume this)
      <name>.clab.yaml      containerlab topology (the SONiC switch image named in fabric.json)
      configure-fabric.sh   self-contained bash (no jq): cables up, bgpd on,
                            interface IPs, eBGP+ECMP via vtysh, host IP/routes
      prometheus.yml        Prometheus scrape config for the collector

Conventions (defined here only; the collector and the Command Center read them from fabric.json):
  * clab eth-index k on a node  <->  SONiC port Ethernet<(k-1)*4>
  * spine i (1-based): ASN 65000+i, one port per leaf (its j-th port -> leaf j)
  * leaf  j (1-based): ASN 65100+j, S uplink ports THEN H host ports
  * leaf j uplink i <-> spine i port j (full mesh)
  * /31 p2p links allocated sequentially from 10.0.0.0/16, spine side gets the
    even/.0 address, leaf side the odd/.1
  * host numbering is GLOBAL: leaf j's hosts are host((j-1)*H+1)..host(j*H),
    subnet 10.10.<j>.0/24, gw .1 on the leaf, hosts .10, .11, ...
  * eBGP everywhere: 'no bgp ebgp-requires-policy', redistribute connected;
    leaves add 'bgp bestpath as-path multipath-relax' + 'maximum-paths S'

CLI:
    python3 fabgen.py --spines 2 --leaves 2 --hosts-per-leaf 1 \
                      --name lab --exec-mode docker --out ./build
    python3 fabgen.py --validate ./build

stdlib only — no jinja, no pyyaml, no jq at runtime for the generated script.
"""
import argparse
import json
import os
import re
import string
import subprocess
import sys

# The SONiC switch image (ADR-0011). This is the one place that names it: fabgen writes it
# into fabric.json ("switch_image"), the containerlab topology uses it, and run.sh reads it
# from fabric.json. By default it is the official community SONiC virtual switch from the
# newest successful build of SONIC_BRANCH, which run.sh downloads from SONiC's build
# pipeline on the first run and tags locally as docker-sonic-vs:<branch>.
#   LAB_SONIC_BRANCH  another release branch, for example 202511 (master is untested with this
#                     lab: it has bgpcfgd/lldpd changes that 202605 does not)
#   LAB_SONIC_IMAGE   any image Docker already has or can pull; run.sh downloads nothing
DEFAULT_SONIC_BRANCH = "202605"
HOST_IMAGE = "alpine:3.20"


def switch_image(image=None, env=None):
    """{"image": tag, "official_branch": branch or None} from --sonic-image or the
    LAB_SONIC_IMAGE / LAB_SONIC_BRANCH settings. official_branch is None when the user
    names their own image, so run.sh knows not to download one."""
    env = os.environ if env is None else env
    image = (image or env.get("LAB_SONIC_IMAGE") or "").strip()
    if image:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image):
            raise ValueError("LAB_SONIC_IMAGE %r is not a Docker image name" % image)
        return {"image": image, "official_branch": None}
    branch = (env.get("LAB_SONIC_BRANCH") or "").strip() or DEFAULT_SONIC_BRANCH
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", branch):
        raise ValueError("LAB_SONIC_BRANCH %r is not a SONiC branch name" % branch)
    return {"image": "docker-sonic-vs:%s" % branch, "official_branch": branch}

# Resource-realism ceilings (each sonic-vs switch needs about 1.5 GB of RAM).
MAX_SPINES, MAX_LEAVES, MAX_HOSTS_PER_LEAF = 8, 16, 4

# ─── modeled hardware catalog ───────────────────────────────────────────────────
# HONESTY: the emulated switch is ALWAYS docker-sonic-vs. A "hardware" pick is a
# MODELED label + capacity input (radix / port speed / ASIC), NOT a different
# container. It annotates fabric.json and drives the capacity math in the UI —
# nothing about the actual deployment (image, config, wiring) changes.
HARDWARE = [
    {"id": "th3-32x400", "vendor": "Edgecore", "product": "AS9716-32D", "asic": "Broadcom Tomahawk 3", "bw_tbps": 12.8, "radix": 32, "port_speed_gbps": 400, "buffer": "shallow", "role": "any"},
    {"id": "th4-64x400", "vendor": "Edgecore", "product": "AS9736-64D", "asic": "Broadcom Tomahawk 4", "bw_tbps": 25.6, "radix": 64, "port_speed_gbps": 400, "buffer": "shallow", "role": "any"},
    {"id": "th5-64x800", "vendor": "Celestica", "product": "DS5000", "asic": "Broadcom Tomahawk 5", "bw_tbps": 51.2, "radix": 64, "port_speed_gbps": 800, "buffer": "shallow", "role": "any"},
    {"id": "th6-64x1600", "vendor": "(ref)", "product": "Tomahawk 6 platform", "asic": "Broadcom Tomahawk 6", "bw_tbps": 102.4, "radix": 64, "port_speed_gbps": 1600, "buffer": "shallow", "role": "any"},
    {"id": "spectrum4-sn5600", "vendor": "NVIDIA", "product": "SN5600", "asic": "Spectrum-4", "bw_tbps": 51.2, "radix": 64, "port_speed_gbps": 800, "buffer": "shallow", "role": "any"},
    {"id": "jericho2c-deepbuf", "vendor": "Arista", "product": "7280R3", "asic": "Broadcom Jericho2c+", "bw_tbps": 9.6, "radix": 24, "port_speed_gbps": 400, "buffer": "deep", "role": "edge"},
]
HW_BY_ID = {m["id"]: m for m in HARDWARE}
DEFAULT_LEAF_MODEL = "th4-64x400"      # AI leaf-spine default leaf
DEFAULT_SPINE_MODEL = "th5-64x800"     # AI leaf-spine default spine
DEFAULT_BREAKOUT = 1


def hw_label(m):
    """Short display label '<vendor> <product> · <radix>×<speed>G · <asic>'."""
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


def radix_errors(S, L, H, Rl, Rs, breakout, leaf_id="leaf model", spine_id="spine model"):
    """Radix feasibility of an S×L fabric with H hosts/leaf on given radixes.
    Returns a list of clear, number-carrying error strings (empty = feasible).
    Rl/Rs None => that radix is unknown and its checks are skipped."""
    errs = []
    if Rs is not None and L > Rs:
        errs.append("leaves L=%d exceeds spine radix Rs=%d (%s) — a 2-tier spine needs one port per leaf; go 3-tier"
                    % (L, Rs, spine_id))
    if Rl is not None and S > Rl:
        errs.append("spines S=%d exceeds leaf radix Rl=%d (%s) — each leaf needs one uplink port per spine"
                    % (S, Rl, leaf_id))
    if Rl is not None:
        max_hosts = (Rl - S) * breakout
        if H > max_hosts:
            errs.append("hosts/leaf H=%d exceeds leaf downlink capacity (Rl-S)*breakout=(%d-%d)*%d=%d (%s)"
                        % (H, Rl, S, breakout, max_hosts, leaf_id))
    return errs

# Prometheus config for the lab. honor_labels: true is REQUIRED — the exporter stamps
# its own 'instance' label on sonic_* metrics and Prometheus must not clobber it.
PROM_TMPL = """\
# prometheus.yml — GENERATED by fabgen.py for lab "$name" (do not edit)
global:
  scrape_interval: 10s   # longer than the collector's 8 s sweep, so every scrape sees a fresh value

scrape_configs:
  - job_name: sonic-fabric
    honor_labels: true    # keep the exporter's own instance label on sonic_* metrics
    static_configs:
      - targets: ["127.0.0.1:9200"]
"""


# ─── model ─────────────────────────────────────────────────────────────────────

def ethport(k):
    """clab eth-index k (1-based) -> SONiC port name."""
    return "Ethernet%d" % ((k - 1) * 4)


def p2p_addr(offset):
    """offset-th address (0-based) inside 10.0.0.0/16."""
    if offset >= 65536:
        raise ValueError("ran out of 10.0.0.0/16 p2p space")
    return "10.0.%d.%d" % (offset // 256, offset % 256)


def build_fabric(name, spines, leaves, hosts_per_leaf, exec_mode,
                 leaf_model=DEFAULT_LEAF_MODEL, spine_model=DEFAULT_SPINE_MODEL,
                 breakout=DEFAULT_BREAKOUT, sonic_image=None):
    """Compute the whole fabric as one dict — the single source of truth.

    leaf_model/spine_model are MODELED hardware ids (see HARDWARE): each switch
    node records its "hw" id and a top-level "hardware" block carries the full
    model dicts + breakout. Hosts and the deployed image are unaffected.
    sonic_image overrides the switch image settings (see switch_image)."""
    S, L, H = spines, leaves, hosts_per_leaf
    nodes, links = [], []

    for i in range(1, S + 1):
        nodes.append({"id": "spine%d" % i, "kind": "spine", "asn": 65000 + i,
                      "hw": spine_model,
                      "ports": [ethport(j) for j in range(1, L + 1)]})
    for j in range(1, L + 1):
        nodes.append({"id": "leaf%d" % j, "kind": "leaf", "asn": 65100 + j,
                      "hw": leaf_model,
                      "ports": [ethport(k) for k in range(1, S + H + 1)]})
    for j in range(1, L + 1):
        for h in range(1, H + 1):
            g = (j - 1) * H + h
            nodes.append({"id": "host%d" % g, "kind": "host",
                          "ip": "10.10.%d.%d" % (j, 9 + h),
                          "gw": "10.10.%d.1" % j})

    # fabric /31s, allocated sequentially spine-major (spine1's links first,
    # like the proven lab). Spine side = even/.0, leaf side = odd/.1.
    n31 = 0
    for i in range(1, S + 1):
        for j in range(1, L + 1):
            base = 2 * n31
            links.append({
                "id": "leaf%d-spine%d" % (j, i),
                "a": "leaf%d" % j, "ap": ethport(i),      # leaf j's i-th port
                "b": "spine%d" % i, "bp": ethport(j),     # spine i's j-th port
                "net": p2p_addr(base) + "/31",
                "aip": p2p_addr(base + 1),                # leaf  = .1 (odd)
                "bip": p2p_addr(base),                    # spine = .0 (even)
            })
            n31 += 1

    # host edges (same shape the copilot's topology API serves: net, no /31 ips)
    for j in range(1, L + 1):
        for h in range(1, H + 1):
            g = (j - 1) * H + h
            links.append({
                "id": "host%d-leaf%d" % (g, j),
                "a": "host%d" % g, "ap": "eth1",
                "b": "leaf%d" % j, "bp": ethport(S + h),  # host ports after uplinks
                "net": "10.10.%d.0/24" % j,
            })

    last_host = "host%d" % (L * H)
    return {
        "name": name,
        "params": {"spines": S, "leaves": L, "hosts_per_leaf": H},
        "hardware": {"leaf": hw_model(leaf_model), "spine": hw_model(spine_model),
                     "breakout": breakout},
        "exec": {"mode": exec_mode, "container_prefix": "clab-%s-" % name},
        "switch_image": switch_image(sonic_image),
        "nodes": nodes,
        "links": links,
        "overload": {"src_host": "host1",
                     "dst_ip": next(n["ip"] for n in nodes if n["id"] == last_host)},
    }


# ─── emitters ──────────────────────────────────────────────────────────────────

def emit_clab_yaml(fab):
    S = fab["params"]["spines"]
    L = fab["params"]["leaves"]
    H = fab["params"]["hosts_per_leaf"]
    out = [
        "name: %s" % fab["name"],
        "",
        "# %d-spine / %d-leaf SONiC fabric with %d host(s) per leaf — GENERATED by fabgen.py." % (S, L, H),
        "# eBGP underlay with ECMP; every leaf connects to every spine.",
        "#   sudo containerlab deploy -t %s.clab.yaml --reconfigure" % fab["name"],
        "",
        "topology:",
        "  kinds:",
        "    sonic-vs:",
        "      # community SONiC virtual switch; run.sh fetches it (see switch_image in fabric.json)",
        "      image: %s" % fab["switch_image"]["image"],
        "",
        "  nodes:",
    ]
    for n in fab["nodes"]:
        if n["kind"] == "host":
            out.append("    %s:  {kind: linux, image: %s}" % (n["id"], HOST_IMAGE))
        else:
            out.append("    %s: {kind: sonic-vs}" % n["id"])
    out += ["", "  links:",
            "    # leaf<j>:eth<i> <-> spine<i>:eth<j> (full mesh), then host edges"]
    for lk in fab["links"]:
        # convert SONiC port names back to clab eth indexes
        aep = lk["ap"] if lk["ap"].startswith("eth") else "eth%d" % (int(lk["ap"][8:]) // 4 + 1)
        bep = "eth%d" % (int(lk["bp"][8:]) // 4 + 1)
        out.append('    - endpoints: ["%s:%s", "%s:%s"]' % (lk["a"], aep, lk["b"], bep))
    return "\n".join(out) + "\n"


def emit_configure_sh(fab):
    """Self-contained bash — fabric.json data baked in as arrays, no jq."""
    S = fab["params"]["spines"]
    L = fab["params"]["leaves"]
    H = fab["params"]["hosts_per_leaf"]
    name = fab["name"]
    switches = [n for n in fab["nodes"] if n["kind"] in ("spine", "leaf")]
    hosts = [n for n in fab["nodes"] if n["kind"] == "host"]
    flinks = [l for l in fab["links"] if "aip" in l]

    if fab["exec"]["mode"] == "limactl":
        xfn = ('LIMA_VM="${LIMA_VM:-sonic}"\n'
               'X(){ limactl shell "$LIMA_VM" -- sudo docker exec "clab-%s-$1" "${@:2}"; }' % name)
    else:
        xfn = 'X(){ docker exec "clab-%s-$1" "${@:2}"; }' % name

    sh = ["#!/bin/bash",
          '# Configure the %d-spine / %d-leaf SONiC fabric "%s" — GENERATED by fabgen.py, do not edit.' % (S, L, name),
          "# eBGP underlay, unique ASN per node, /31 links, redistribute connected, ECMP on the leaves.",
          "# Idempotent enough to re-run after a fresh deploy.",
          "set -u",
          xfn,
          "",
          "SWITCHES=(%s)" % " ".join(n["id"] for n in switches),
          "HOSTS=(%s)" % " ".join(n["id"] for n in hosts),
          "",
          'echo "== 1. bring containerlab cables up (VS admin-down quirk) =="',
          "# node:eth-count pairs — every clab cable end must be set up by hand",
          "NODE_ETHS=("]
    for n in switches:
        sh.append('  "%s:%d"' % (n["id"], len(n["ports"])))
    sh += [")",
           'for e in "${NODE_ETHS[@]}"; do',
           '  n="${e%%:*}"; c="${e##*:}"',
           '  i=1; while [ "$i" -le "$c" ]; do X "$n" ip link set dev "eth$i" up 2>/dev/null; i=$((i+1)); done',
           "done",
           'for h in "${HOSTS[@]}"; do X "$h" ip link set dev eth1 up 2>/dev/null; done',
           "",
           'echo "== 2. enable + start bgpd on all switches (off by default in this image) =="',
           "# bgpd must start after zebra answers. A bgpd that starts first keeps every BGP next hop",
           "# 'invalid': sessions come up, but no routes cross that switch. So wait for zebra, then",
           "# restart bgpd (restart, not start, in case it is already running).",
           'for n in "${SWITCHES[@]}"; do',
           '  X "$n" sed -i "s/^bgpd=no/bgpd=yes/" /etc/frr/daemons',
           "  X \"$n\" sh -c 'i=0; until vtysh -d zebra -c \"show version\" >/dev/null 2>&1 || [ $i -ge 60 ]; do i=$((i+1)); sleep 1; done'",
           '  X "$n" supervisorctl restart bgpd >/dev/null 2>&1',
           "done",
           "",
           'echo "== 3. interface IPs =="',
           "# switch  port  cidr — spine side of each /31 is even/.0, leaf side .1",
           "IFIPS=("]
    for lk in flinks:
        plen = lk["net"].split("/")[1]
        sh.append('  "%s %s %s/%s"' % (lk["b"], lk["bp"], lk["bip"], plen))
        sh.append('  "%s %s %s/%s"' % (lk["a"], lk["ap"], lk["aip"], plen))
    for j in range(1, L + 1):
        leaf = next(n for n in switches if n["id"] == "leaf%d" % j)
        for h in range(1, H + 1):
            sh.append('  "leaf%d %s 10.10.%d.1/24"' % (j, leaf["ports"][S + h - 1], j))
    sh += [")",
           'for row in "${IFIPS[@]}"; do',
           "  set -- $row",
           '  X "$1" config interface ip add "$2" "$3"',
           '  X "$1" config interface startup "$2"',
           "done",
           "",
           "# BGP timers 3/9 s (keepalive/hold), as in FRR's datacenter profile. The virtual switches",
           "# don't pass a link's loss to BGP, so with FRR's default 60/180 s a dead spine or cut",
           "# cable kept drawing traffic for 3 minutes before the leaves rerouted.",
           'echo "== 4. eBGP (leaves peer EVERY spine; multipath-relax + maximum-paths %d = ECMP) =="' % S]
    for i in range(1, S + 1):
        nbrs = " ".join('-c "neighbor %s remote-as %d"' % (lk["aip"], 65100 + int(lk["a"][4:]))
                        for lk in flinks if lk["b"] == "spine%d" % i)
        sh.append('X spine%d vtysh -c "conf t" -c "router bgp %d" -c "bgp router-id 1.1.1.%d" '
                  '-c "no bgp ebgp-requires-policy" -c "timers bgp 3 9" \\' % (i, 65000 + i, i))
        sh.append("  %s \\" % nbrs)
        sh.append('  -c "address-family ipv4 unicast" -c "redistribute connected"')
    for j in range(1, L + 1):
        nbrs = " ".join('-c "neighbor %s remote-as %d"' % (lk["bip"], 65000 + int(lk["b"][5:]))
                        for lk in flinks if lk["a"] == "leaf%d" % j)
        sh.append('X leaf%d vtysh -c "conf t" -c "router bgp %d" -c "bgp router-id 1.1.1.%d" '
                  '-c "no bgp ebgp-requires-policy" -c "timers bgp 3 9" \\' % (j, 65100 + j, 100 + j))
        sh.append('  -c "bgp bestpath as-path multipath-relax" \\')
        sh.append("  %s \\" % nbrs)
        sh.append('  -c "address-family ipv4 unicast" -c "redistribute connected" -c "maximum-paths %d"' % S)
    sh += ["",
           'echo "== 5. hosts =="']
    for n in hosts:
        sh.append('X %s ip addr add %s/24 dev eth1 2>/dev/null; X %s ip route replace default via %s'
                  % (n["id"], n["ip"], n["id"], n["gw"]))
    sh += ["", 'echo "== done =="', ""]
    return "\n".join(sh)


def emit_prometheus_yml(fab):
    return string.Template(PROM_TMPL).safe_substitute(
        name=fab["name"], **{k: str(v) for k, v in fab["params"].items()})


# ─── validation ────────────────────────────────────────────────────────────────

def validate_build(build_dir):
    """Consistency-check an emitted build dir. Returns a list of problems."""
    errs = []
    fj = os.path.join(build_dir, "fabric.json")
    if not os.path.isfile(fj):
        return ["%s: fabric.json not found" % build_dir]
    try:
        with open(fj) as f:
            fab = json.load(f)
    except (ValueError, OSError) as e:
        return ["fabric.json unreadable: %s" % e]

    try:
        S = int(fab["params"]["spines"])
        L = int(fab["params"]["leaves"])
        H = int(fab["params"]["hosts_per_leaf"])
        nodes = {n["id"]: n for n in fab["nodes"]}
        links = fab["links"]
    except (KeyError, TypeError, ValueError) as e:
        return ["fabric.json missing required structure: %s" % e]

    # modeled-hardware radix feasibility (uses the hardware block if present;
    # skips cleanly for pre-hardware fabric.json so old builds still validate)
    hw = fab.get("hardware") or {}
    lm = hw.get("leaf") or {}
    sm = hw.get("spine") or {}
    bo = hw.get("breakout", 1) or 1
    for e in radix_errors(S, L, H, lm.get("radix"), sm.get("radix"), bo,
                          lm.get("id", "leaf model"), sm.get("id", "spine model")):
        errs.append(e)
    # each spine/leaf node must carry an "hw" id when a hardware block is present
    if lm or sm:
        for n in fab["nodes"]:
            if n.get("kind") in ("spine", "leaf") and not n.get("hw"):
                errs.append("node %s missing 'hw' model id" % n.get("id"))

    # node counts
    kinds = {"spine": S, "leaf": L, "host": L * H}
    for kind, want in kinds.items():
        got = sum(1 for n in fab["nodes"] if n["kind"] == kind)
        if got != want:
            errs.append("expected %d %s nodes, found %d" % (want, kind, got))

    # ASN uniqueness
    asns = [n["asn"] for n in fab["nodes"] if "asn" in n]
    if len(asns) != len(set(asns)):
        errs.append("duplicate ASNs: %s" % sorted(a for a in set(asns) if asns.count(a) > 1))

    # IP uniqueness (p2p ends + host ips + leaf gateways)
    ips = [ip for lk in links for ip in (lk.get("aip"), lk.get("bip")) if ip]
    ips += [n["ip"] for n in fab["nodes"] if n["kind"] == "host"]
    ips += sorted({n["gw"] for n in fab["nodes"] if n["kind"] == "host"})
    dups = sorted(ip for ip in set(ips) if ips.count(ip) > 1)
    if dups:
        errs.append("duplicate IPs: %s" % dups)
    nets = [lk["net"] for lk in links if "aip" in lk]
    if len(nets) != len(set(nets)):
        errs.append("duplicate /31 nets")

    # every node referenced in links exists; ports belong to the node
    for lk in links:
        for end, port in (("a", "ap"), ("b", "bp")):
            nid = lk[end]
            if nid not in nodes:
                errs.append("link %s references unknown node %s" % (lk["id"], nid))
            elif nodes[nid]["kind"] != "host" and lk[port] not in nodes[nid]["ports"]:
                errs.append("link %s: port %s not in %s's port list" % (lk["id"], lk[port], nid))

    # port symmetry: leaf j uplink i <-> spine i port j; spine even/.0, leaf odd
    for i in range(1, S + 1):
        for j in range(1, L + 1):
            lk = next((l for l in links
                       if l.get("a") == "leaf%d" % j and l.get("b") == "spine%d" % i), None)
            if lk is None:
                errs.append("missing fabric link leaf%d<->spine%d" % (j, i))
                continue
            if lk["ap"] != ethport(i):
                errs.append("%s: leaf%d uplink to spine%d should be %s, got %s"
                            % (lk["id"], j, i, ethport(i), lk["ap"]))
            if lk["bp"] != ethport(j):
                errs.append("%s: spine%d port to leaf%d should be %s, got %s"
                            % (lk["id"], i, j, ethport(j), lk["bp"]))
            try:
                bo = [int(x) for x in lk["bip"].split(".")]
                ao = [int(x) for x in lk["aip"].split(".")]
                if bo[3] % 2 != 0:
                    errs.append("%s: spine side %s is not the even /31 address" % (lk["id"], lk["bip"]))
                if (ao[2] * 256 + ao[3]) != (bo[2] * 256 + bo[3]) + 1 or ao[:2] != bo[:2]:
                    errs.append("%s: %s/%s are not a /31 pair" % (lk["id"], lk["bip"], lk["aip"]))
                if not lk["net"].startswith(lk["bip"] + "/"):
                    errs.append("%s: net %s does not start at spine address %s"
                                % (lk["id"], lk["net"], lk["bip"]))
            except (ValueError, IndexError):
                errs.append("%s: malformed p2p addresses" % lk["id"])

    # overload block sanity
    ov = fab.get("overload", {})
    if ov.get("src_host") not in nodes:
        errs.append("overload.src_host %r is not a node" % ov.get("src_host"))
    if ov.get("dst_ip") not in {n["ip"] for n in fab["nodes"] if n["kind"] == "host"}:
        errs.append("overload.dst_ip %r is not a host IP" % ov.get("dst_ip"))
    if fab.get("exec", {}).get("mode") not in ("docker", "limactl"):
        errs.append("exec.mode must be docker|limactl")
    if fab.get("exec", {}).get("container_prefix") != "clab-%s-" % fab.get("name"):
        errs.append("exec.container_prefix does not match lab name")

    # clab yaml — naive structural check (no pyyaml): node + endpoint counts
    cy = os.path.join(build_dir, "%s.clab.yaml" % fab["name"])
    if not os.path.isfile(cy):
        errs.append("missing %s.clab.yaml" % fab["name"])
    else:
        with open(cy) as f:
            ytxt = f.read()
        ynodes = re.findall(r"^    ([A-Za-z0-9_-]+): *\{kind:", ytxt, re.M)
        yeps = re.findall(r'^    - endpoints: \["([^:]+):(eth\d+)", "([^:]+):(eth\d+)"\]', ytxt, re.M)
        if len(ynodes) != len(fab["nodes"]):
            errs.append("clab yaml has %d nodes, fabric.json has %d" % (len(ynodes), len(fab["nodes"])))
        if set(ynodes) != set(nodes):
            errs.append("clab yaml node names differ from fabric.json")
        if len(yeps) != len(links):
            errs.append("clab yaml has %d links, fabric.json has %d" % (len(yeps), len(links)))
        if not re.match(r"^name: %s\s*$" % re.escape(fab["name"]), ytxt.splitlines()[0]):
            errs.append("clab yaml name != fabric.json name")
        want = (fab.get("switch_image") or {}).get("image")
        yimg = re.findall(r"^    sonic-vs:\n(?:      #.*\n)*      image: (\S+)$", ytxt, re.M)
        if want and yimg != [want]:
            errs.append("clab yaml switch image %s != fabric.json switch_image %s"
                        % (yimg[0] if yimg else "(none)", want))

    # configure-fabric.sh present + syntactically valid bash
    cf = os.path.join(build_dir, "configure-fabric.sh")
    if not os.path.isfile(cf):
        errs.append("missing configure-fabric.sh")
    else:
        try:
            r = subprocess.run(["bash", "-n", cf], capture_output=True, text=True)
            if r.returncode != 0:
                errs.append("configure-fabric.sh fails bash -n: %s" % r.stderr.strip())
        except OSError:
            pass  # no bash on PATH — skip, other checks still ran

    if not os.path.isfile(os.path.join(build_dir, "prometheus.yml")):
        errs.append("missing prometheus.yml")
    return errs


# ─── main ──────────────────────────────────────────────────────────────────────

def summary(fab, out_dir):
    S = fab["params"]["spines"]
    L = fab["params"]["leaves"]
    H = fab["params"]["hosts_per_leaf"]
    n31 = S * L
    ram = 1.5 * (S + L) + 0.05 * (L * H)
    rows = [
        ("lab name", fab["name"]),
        ("spines", "%d  (ASN %d..%d)" % (S, 65001, 65000 + S)),
        ("leaves", "%d  (ASN %d..%d)" % (L, 65101, 65100 + L)),
        ("hosts", "%d  (%d per leaf)" % (L * H, H)),
        ("leaf hw (modeled)", (fab.get("hardware", {}).get("leaf") or {}).get("label", "—")),
        ("spine hw (modeled)", (fab.get("hardware", {}).get("spine") or {}).get("label", "—")),
        ("total nodes", str(len(fab["nodes"]))),
        ("fabric /31 links", str(n31)),
        ("host links", str(L * H)),
        ("total links", str(len(fab["links"]))),
        ("est. RAM", "~%.1f GB  (1.5/sonic-vs + 0.05/host)" % ram),
        ("switch image", fab["switch_image"]["image"]),
        ("exec mode", fab["exec"]["mode"]),
        ("out dir", out_dir),
    ]
    w = max(len(r[0]) for r in rows)
    print("== %s build summary ==" % fab["name"])
    for k, v in rows:
        print("  %-*s  %s" % (w, k, v))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Generate (or validate) a parameterized SONiC leaf-spine DC lab.")
    ap.add_argument("--spines", type=int, default=2)
    ap.add_argument("--leaves", type=int, default=2)
    ap.add_argument("--hosts-per-leaf", type=int, default=1)
    ap.add_argument("--name", default="lab")
    ap.add_argument("--exec-mode", choices=["docker", "limactl"], default="docker")
    ap.add_argument("--leaf-model", default=DEFAULT_LEAF_MODEL,
                    help="modeled leaf hardware id (see --list-hardware); default %s" % DEFAULT_LEAF_MODEL)
    ap.add_argument("--spine-model", default=DEFAULT_SPINE_MODEL,
                    help="modeled spine hardware id (see --list-hardware); default %s" % DEFAULT_SPINE_MODEL)
    ap.add_argument("--list-hardware", action="store_true",
                    help="print the modeled hardware catalog and exit")
    ap.add_argument("--sonic-image", metavar="IMAGE",
                    help="switch image for the topology; default from LAB_SONIC_IMAGE, else "
                         "docker-sonic-vs:<LAB_SONIC_BRANCH or %s>" % DEFAULT_SONIC_BRANCH)
    ap.add_argument("--out", default="./build")
    ap.add_argument("--validate", metavar="BUILD_DIR",
                    help="validate an emitted build dir instead of generating")
    args = ap.parse_args(argv)

    if args.list_hardware:
        for m in HARDWARE:
            print("%-18s %s" % (m["id"], hw_label(m)))
        return 0

    if args.validate:
        errs = validate_build(args.validate)
        if errs:
            for e in errs:
                print("FAIL: %s" % e, file=sys.stderr)
            return 1
        print("OK: %s is consistent" % args.validate)
        return 0

    # sanity limits — sonic-vs is heavy; keep the lab deployable on one VM
    if not 1 <= args.spines <= MAX_SPINES:
        ap.error("--spines must be 1..%d (each sonic-vs needs ~1.5 GB RAM)" % MAX_SPINES)
    if not 1 <= args.leaves <= MAX_LEAVES:
        ap.error("--leaves must be 1..%d (each sonic-vs needs ~1.5 GB RAM)" % MAX_LEAVES)
    if not 1 <= args.hosts_per_leaf <= MAX_HOSTS_PER_LEAF:
        ap.error("--hosts-per-leaf must be 1..%d" % MAX_HOSTS_PER_LEAF)
    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.name):
        ap.error("--name must be lowercase alphanumeric/hyphen (used in container names)")

    # modeled hardware: validate ids, then enforce radix (with the exact numbers)
    if args.leaf_model not in HW_BY_ID:
        ap.error("--leaf-model must be one of: %s" % ", ".join(HW_BY_ID))
    if args.spine_model not in HW_BY_ID:
        ap.error("--spine-model must be one of: %s" % ", ".join(HW_BY_ID))
    rerrs = radix_errors(args.spines, args.leaves, args.hosts_per_leaf,
                         HW_BY_ID[args.leaf_model]["radix"],
                         HW_BY_ID[args.spine_model]["radix"],
                         DEFAULT_BREAKOUT, args.leaf_model, args.spine_model)
    if rerrs:
        ap.error("; ".join(rerrs))

    try:
        fab = build_fabric(args.name, args.spines, args.leaves,
                           args.hosts_per_leaf, args.exec_mode,
                           args.leaf_model, args.spine_model, DEFAULT_BREAKOUT,
                           args.sonic_image)
    except ValueError as e:
        ap.error(str(e))
    out = args.out
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "fabric.json"), "w") as f:
        json.dump(fab, f, indent=2)
        f.write("\n")
    with open(os.path.join(out, "%s.clab.yaml" % fab["name"]), "w") as f:
        f.write(emit_clab_yaml(fab))
    cf = os.path.join(out, "configure-fabric.sh")
    with open(cf, "w") as f:
        f.write(emit_configure_sh(fab))
    os.chmod(cf, 0o755)
    with open(os.path.join(out, "prometheus.yml"), "w") as f:
        f.write(emit_prometheus_yml(fab))

    summary(fab, out)
    errs = validate_build(out)
    if errs:
        for e in errs:
            print("FAIL: %s" % e, file=sys.stderr)
        return 1
    print("validated: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
