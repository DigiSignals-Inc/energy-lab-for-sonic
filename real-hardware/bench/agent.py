#!/usr/bin/env python3
"""
The bench agent — answers questions about the live two-switch setup.

Grounding is three layers, in order of authority:

  1. LIVE TELEMETRY   the same snapshot the dashboard is rendering, injected fresh
                      on every question. This is what makes "why is PFC high right
                      now" answerable rather than guessed.
  2. BENCH KNOWLEDGE  bench-knowledge.md — the topology, the config, the findings,
                      the measured results, and an explicit list of things that do
                      not exist on this hardware.
  3. LOOKUPS          a small whitelist of read-only queries the model may request
                      when the snapshot does not carry the answer. Nothing here can
                      change state; every command is a read.

The brain is pluggable. It reads .agent.env next to this file:

    AGENT_PROVIDER=openai-compatible | azure-openai
    AGENT_BASE_URL=...
    AGENT_API_KEY=...
    AGENT_MODEL=name[,fallback,...]
    AGENT_DEPLOYMENT=...        # azure-openai only
    AGENT_API_VERSION=...       # azure-openai only

Any OpenAI-compatible endpoint works (for example Gemini's). For Azure OpenAI set
AGENT_PROVIDER=azure-openai, AGENT_BASE_URL=https://<your-resource>.openai.azure.com
and AGENT_DEPLOYMENT to a model deployment you created; a resource with no
deployment answers 404 DeploymentNotFound. AGENT_* environment variables override
the file (see .env.example). Switching is a config edit, not a code change.
"""
import json, os, re, subprocess, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
KNOWLEDGE_PATH = os.path.join(HERE, "bench-knowledge.md")
ENV_PATH = os.path.join(HERE, ".agent.env")

# ---------------------------------------------------------------- brain config

def _load_env():
    cfg = {}
    for path in (ENV_PATH, os.path.join(HERE, "agent.env")):
        if not os.path.exists(path):
            continue
        for line in open(path, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = re.sub(r"^export\s+", "", line)
            if "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
        break
    for k, v in os.environ.items():
        if k.startswith("AGENT_"):
            cfg[k] = v
    return cfg

CFG = _load_env()


def brain_status():
    """What the UI shows in the panel header, with no secret in it."""
    prov = CFG.get("AGENT_PROVIDER", "")
    if not CFG.get("AGENT_API_KEY"):
        return {"ok": False, "label": "no brain configured",
                "detail": "Create real-hardware/bench/.agent.env (or set AGENT_* variables; see .env.example)."}
    model = (CFG.get("AGENT_DEPLOYMENT") if prov == "azure-openai"
             else (CFG.get("AGENT_MODEL", "").split(",")[0]))
    host = re.sub(r"^https?://", "", CFG.get("AGENT_BASE_URL", "")).split("/")[0]
    return {"ok": True, "label": model or "?", "detail": host}

# ---------------------------------------------------------------- read-only lookups

# Every entry is a READ. Nothing here writes, restarts, or generates traffic — the
# agent must never be able to disturb a live demo.
LOOKUPS = {
    "switch_version":  ("sw", "show version 2>/dev/null | head -12"),
    "port_status":     ("sw", "show interfaces status 2>/dev/null | head -14"),
    "port_counters":   ("sw", "show interfaces counters 2>/dev/null | head -14"),
    "pfc_counters":    ("sw", "show pfc counters 2>/dev/null | head -14"),
    "queue_counters":  ("sw", "show queue counters Ethernet8 2>/dev/null | head -16"),
    "wred_profile":    ("sw", "sudo sonic-db-cli CONFIG_DB hgetall 'WRED_PROFILE|AZURE_LOSSLESS'"),
    "buffer_profile":  ("sw", "sudo sonic-db-cli CONFIG_DB hgetall 'BUFFER_PROFILE|pg_lossless_100000_40m_profile'"),
    "qos_maps":        ("sw", "sudo sonic-db-cli CONFIG_DB keys 'PORT_QOS_MAP|*' 2>/dev/null | head -8"),
    "counterpoll":     ("sw", "sudo counterpoll show 2>/dev/null | head -16"),
    "platform_temp":   ("sw", "show platform temperature 2>/dev/null | head -12"),
    "platform_fan":    ("sw", "show platform fan 2>/dev/null | head -14"),
    "platform_psu":    ("sw", "show platform psustatus 2>/dev/null | head -8"),
    "environment":     ("sw", "show environment 2>/dev/null | head -24"),
    "bgp_summary":     ("sw", "show ip bgp summary 2>/dev/null | head -12"),
    "routes":          ("sw", "show ip route 2>/dev/null | head -14"),
    "gnmi_state":      ("sw", "docker exec gnmi ps -ef 2>/dev/null | head -4"),
    "running_flows":   ("host", "ps -eo pid,args | grep -E 'ib_write_bw|iperf3' | grep -v grep | head -6"),
    "nic_info":        ("host", "ibv_devinfo 2>/dev/null | head -14"),
    "cc_params":       ("host", "sudo bash -c 'cd /sys/kernel/debug/mlx5/*/cc_params 2>/dev/null && for f in *; do printf \"%s=%s \" \"$f\" \"$(cat $f)\"; done'"),
    "rdma_counters":   ("host", "for f in /sys/class/infiniband/*/ports/1/hw_counters/{np_ecn_marked_roce_packets,np_cnp_sent,rp_cnp_handled}; do [ -f $f ] && echo \"$(basename $f)=$(cat $f)\"; done"),
    "link_state":      ("host", "ip -br link show 2>/dev/null | head -8; ethtool ens5np0 2>/dev/null | grep -E 'Speed|Link det' || ethtool ens4np0 2>/dev/null | grep -E 'Speed|Link det'"),
}

SW_HOSTS = {"swa", "swb"}
HOST_HOSTS = {"pc1", "pc2"}


def run_lookup(ssh, name, host):
    """Run one whitelisted read. Returns (text, error)."""
    if name not in LOOKUPS:
        return None, f"unknown lookup '{name}'"
    kind, cmd = LOOKUPS[name]
    if kind == "sw" and host not in SW_HOSTS:
        return None, f"'{name}' is a switch lookup; host must be swa or swb"
    if kind == "host" and host not in HOST_HOSTS:
        return None, f"'{name}' is a host lookup; host must be pc1 or pc2"
    out = ssh(host, cmd, timeout=18) or ""
    out = "\n".join(l for l in out.splitlines()
                    if l.strip() and "Debian GNU" not in l and not l.strip().startswith("\\"))
    return (out[:2600] or "(no output)"), None

# ---------------------------------------------------------------- context

def _fmt_live(state):
    b = state.get("swb") or {}
    a = state.get("swa") or {}
    p1 = state.get("pc1") or {}
    p2 = state.get("pc2") or {}
    plat = state.get("plat") or {}
    L = []
    L.append(f"mode button last pressed: {state.get('phase', 'idle')}"
             f"   traffic actually flowing: {bool(state.get('flowing'))}")
    L.append("")
    L.append("AT THE CONGESTED EXIT (swb):")
    L.append(f"  offered  (Ethernet0 in) : {b.get('in_bps', 0)/1e9:.1f} Gb/s")
    L.append(f"  delivered(Ethernet8 out): {b.get('out_bps', 0)/1e9:.1f} Gb/s")
    L.append(f"  drops                   : {round(b.get('disc_rate', 0)):,} /s")
    L.append(f"  PFC pause tx (prio 3)   : {round(b.get('pfc3tx_rate', 0)):,} /s")
    L.append(f"  ECN marks               : {round(b.get('ecn_rate', 0)):,} /s"
             f"   (lifetime {int(b.get('ecn_total', 0)):,})")
    L.append(f"  queue-3 watermark       : {b.get('q3_wm', 0)} bytes")
    L.append("")
    L.append("HOST RDMA COUNTERS:")
    L.append(f"  pc1 ECN-marked received : {round(p1.get('ecnmark_rate', 0)):,} /s")
    L.append(f"  pc1 CNP sent            : {round(p1.get('cnpsent_rate', 0)):,} /s")
    L.append(f"  pc2 CNP handled         : {round(p2.get('cnphandled_rate', 0)):,} /s")
    L.append(f"  pc2 NIC tx / pc1 NIC rx : {p2.get('tx_bps', 0)/1e9:.1f} / {p1.get('rx_bps', 0)/1e9:.1f} Gb/s")
    for sw in ("swa", "swb"):
        pl = plat.get(sw)
        if not pl:
            continue
        T = pl.get("temp") or {}
        hot = sorted(((v, k) for k, v in T.items() if v is not None), reverse=True)[:3]
        L.append("")
        L.append(f"PLATFORM {sw}:")
        L.append("  PSU " + " ".join(f"{k}={v} W" for k, v in sorted((pl.get('psu') or {}).items()))
                 + f"  total {pl.get('psu_total')} W")
        L.append("  hottest sensors: " + ", ".join(f"{k} {v:.1f}C" for v, k in hot))
        L.append(f"  fans: {pl.get('fan_n')} at {pl.get('fan_min')}-{pl.get('fan_max')}% "
                 f"({pl.get('fan_bad')} reporting Not OK — a false status on this platform)")
    L.append("")
    L.append(f"swa egress toward swb: {a.get('out_bps', 0)/1e9:.1f} Gb/s")
    hist = state.get("hist") or []
    if len(hist) > 4:
        recent = hist[-12:]
        L.append("recent offered/delivered Gb/s (oldest→newest): "
                 + " ".join(f"{h['off']:.0f}/{h['del']:.0f}" for h in recent))
    return "\n".join(L)


SYSTEM = """You are the bench engineer for a live two-switch SONiC congestion lab. You answer
questions about THIS setup, for the people who built it and for engineers learning SONiC.

You are given, on every question:
  1. A LIVE SNAPSHOT of the bench, read seconds ago.
  2. A KNOWLEDGE BASE describing the hardware, the configuration, the findings and the
     measured results.
You may also request read-only LOOKUPS (listed below) when neither carries the answer.

HOW TO ANSWER
- Lead with the answer. No preamble, no restating the question.
- Use the live numbers when the question is about right now. Quote them.
- Be concrete: name the actual port, counter, file or command.
- Short. Two or three sentences for a simple question. Structure only if it genuinely helps.
- You are talking to engineers who know spine-leaf, RDMA, RoCE, ECMP, queues and SAI. Do not
  explain those. Do explain anything specific to THIS bench.

HONESTY RULES — these matter more than being helpful
- If it is not in the snapshot, the knowledge base, or a lookup you ran, say you do not know.
  Never infer a number.
- Never claim congestion raises power or temperature. It was measured and it does not, at this
  load. If asked, give the measured null and the reason.
- Say "read over gNMI", never "streamed" — Subscribe is unproven here.
- Fan speed is a percentage, not RPM. The "Not OK" fan status is false on this platform.
- "ASIC On-board Rear" is a board sensor, not a die sensor.
- The bench runs ConnectX firmware DCQCN, not the 2015 paper's algorithm.
- Distinguish the real bench from the emulated fabric. Fabric power and heat are modelled.
- AVERAGE IS NOT INSTANTANEOUS. Every rate in the snapshot is an average over one poll interval.
  This bench's traffic is bursty by construction — a 100G NIC software-paced to a lower average
  sends line-rate bursts with gaps. So a 12 Gb/s average can fill a 25 Gb/s queue, and the same
  flow can read 8 Gb/s and 22 Gb/s in consecutive windows. Never reason as if the rate were
  steady, and never call a link oversubscribed from an average that is below the line rate.
- DO NOT EXPLAIN A CAUSE YOU HAVE NOT CHECKED. Reporting a counter is not the same as knowing
  why it moved. If asked "why", either give the mechanism the knowledge base documents, or run a
  lookup, or say which of two explanations you cannot yet separate. A fluent guess about
  causation is the worst answer you can give here — it is the failure mode this bench exists to
  study.
- The queue watermark is the tell-tale for burstiness: a deep watermark under a modest average
  means lumps, not sustained overload. Use it before attributing anything to oversubscription.

REQUESTING A LOOKUP
If you need data you do not have, reply with ONLY this line and nothing else:
LOOKUP: <name> <host>
for example:  LOOKUP: pfc_counters swb
You will be given the output and asked again. Use this at most twice per question, and only
when it will actually change your answer."""


def build_messages(question, state, knowledge, lookups_done):
    ctx = [
        "=== LIVE SNAPSHOT (read seconds ago) ===",
        _fmt_live(state),
        "",
        "=== AVAILABLE LOOKUPS ===",
        ", ".join(sorted(LOOKUPS)),
        "  switch lookups take swa|swb, host lookups take pc1|pc2",
        "",
        "=== BENCH KNOWLEDGE ===",
        knowledge,
    ]
    if lookups_done:
        ctx.append("")
        ctx.append("=== LOOKUPS YOU REQUESTED ===")
        for name, host, out in lookups_done:
            ctx.append(f"--- {name} on {host} ---\n{out}")
    return [{"role": "system", "content": SYSTEM + "\n\n" + "\n".join(ctx)},
            {"role": "user", "content": question}]

# ---------------------------------------------------------------- the call

def _post(url, headers, payload, timeout=70):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _chat(messages):
    """Return (text, error). Tries each model in AGENT_MODEL until one answers."""
    prov = CFG.get("AGENT_PROVIDER", "openai-compatible")
    key = CFG.get("AGENT_API_KEY", "")
    if not key:
        return None, "No brain configured. Create real-hardware/bench/.agent.env (or set AGENT_* variables; see .env.example)."
    last = None
    if prov == "azure-openai":
        base = CFG.get("AGENT_BASE_URL", "").rstrip("/")
        dep = CFG.get("AGENT_DEPLOYMENT", "")
        ver = CFG.get("AGENT_API_VERSION", "2024-10-21")
        url = f"{base}/openai/deployments/{dep}/chat/completions?api-version={ver}"
        try:
            d = _post(url, {"api-key": key},
                      {"messages": messages, "max_tokens": 700, "temperature": 0.2})
            return d["choices"][0]["message"]["content"].strip(), None
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            if "DeploymentNotFound" in body:
                return None, ("The Azure OpenAI resource has no model deployment. The keys are "
                              "valid but nothing is deployed — create one in the portal, then "
                              "set AGENT_DEPLOYMENT in real-hardware/bench/.agent.env.")
            return None, f"Azure OpenAI HTTP {e.code}: {body}"
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"
    base = CFG.get("AGENT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    for model in [m.strip() for m in CFG.get("AGENT_MODEL", "").split(",") if m.strip()]:
        try:
            d = _post(base + "/chat/completions", {"Authorization": "Bearer " + key},
                      {"model": model, "messages": messages,
                       "max_tokens": 700, "temperature": 0.2})
            return d["choices"][0]["message"]["content"].strip(), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} on {model}: {e.read().decode()[:140]}"
        except Exception as e:
            last = f"{type(e).__name__} on {model}: {e}"
    return None, last or "No model in AGENT_MODEL answered."


def ask(question, state, ssh):
    """Answer one question. Returns {answer|error, lookups:[...]}."""
    question = (question or "").strip()
    if not question:
        return {"error": "Ask something."}
    if len(question) > 1500:
        question = question[:1500]
    try:
        knowledge = open(KNOWLEDGE_PATH, encoding="utf-8").read()
    except Exception:
        knowledge = "(bench-knowledge.md missing — say so rather than guessing)"

    done = []
    for _ in range(3):                       # at most two lookup rounds, then answer
        text, err = _chat(build_messages(question, state, knowledge, done))
        if err:
            return {"error": err, "lookups": [d[0] for d in done]}
        m = re.match(r"^\s*LOOKUP:\s*([a-z_]+)\s+(\w+)\s*$", text, re.I | re.M)
        if not m or len(done) >= 2:
            return {"answer": re.sub(r"^\s*LOOKUP:.*$", "", text, flags=re.M).strip(),
                    "lookups": [f"{d[0]} on {d[1]}" for d in done]}
        name, host = m.group(1).lower(), m.group(2).lower()
        out, lerr = run_lookup(ssh, name, host)
        done.append((name, host, out if out else f"(lookup failed: {lerr})"))
    return {"answer": text, "lookups": [f"{d[0]} on {d[1]}" for d in done]}
