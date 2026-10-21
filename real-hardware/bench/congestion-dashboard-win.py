#!/usr/bin/env python3
"""
Energy Lab - Live Congestion Control Room
Polls the two-switch congestion fabric over SSH (via ~/.ssh/config aliases
swa/swb/pc1/pc2) and serves a live dark mission-control dashboard.

Path:  PC2 (100G) -> swa -> [100G] -> swb -> (25G bottleneck) -> PC1
Shows: throughput, congestion drops, PFC pause frames, ECN marks, queue
       watermark, buffer, PSU power and ASIC/board temperature - live.

Run:   python3 congestion-dashboard-win.py   then open http://localhost:8808
       The page is congestion-dashboard.html, next to this file.

Trusted machines only. The buttons start and stop line-rate traffic on the bench,
and there is no login. The server listens on 127.0.0.1 unless BENCH_BIND says
otherwise (for example BENCH_BIND=0.0.0.0 on a closed lab LAN). Actions are
POST-only and need a JSON body, so a link or form on another web page cannot
trigger them.
"""
import http.server, socketserver, json, re, subprocess, threading, time, os
try:
    import agent          # the bench agent; optional, dashboard runs fine without it
except Exception as _e:
    agent = None
    print(f'[bench] agent unavailable: {_e}')
try:
    import gnmi_read      # STAGE 1: platform sensors over real gNMI, SSH as fallback
except Exception as _e:
    gnmi_read = None
    print(f'[bench] gnmi_read unavailable, staying on SSH: {_e}')

PORT = 8808
BIND = os.environ.get("BENCH_BIND", "").strip() or "127.0.0.1"
state = {"t": 0, "phase": "idle", "swa": {}, "swb": {}, "pc2": {}, "pc1": {}, "hist": []}
_prev = {}

# SSH destination per bench role, from BENCH_SWA / BENCH_SWB / BENCH_PC1 / BENCH_PC2
# (user@host or a ~/.ssh/config alias; see .env.example). Unset or empty means the
# ~/.ssh/config alias of the same name, as described in the header.
HOSTS = {h: (os.environ.get("BENCH_" + h.upper()) or h) for h in ("swa", "swb", "pc1", "pc2")}
def ssh(host, cmd, timeout=12):
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                            "-o", "ConnectTimeout=8", HOSTS.get(host, host), cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""

# One remote script per switch: emit KEY=VAL lines for every counter we show.
SW_POLL = r'''
NM=$(sudo sonic-db-cli COUNTERS_DB hget COUNTERS_PORT_NAME_MAP __PORT__ 2>/dev/null)
ING=$(sudo sonic-db-cli COUNTERS_DB hget COUNTERS_PORT_NAME_MAP __ING__ 2>/dev/null)
Q3=$(sudo sonic-db-cli COUNTERS_DB hget COUNTERS_QUEUE_NAME_MAP __PORT__:3 2>/dev/null)
g(){ sudo sonic-db-cli COUNTERS_DB hget COUNTERS:$1 $2 2>/dev/null; }
echo "out_oct=$(g $NM SAI_PORT_STAT_IF_OUT_OCTETS)"
echo "out_disc=$(g $NM SAI_PORT_STAT_IF_OUT_DISCARDS)"
echo "in_oct=$(g $ING SAI_PORT_STAT_IF_IN_OCTETS)"
echo "eg_in_oct=$(g $PORT SAI_PORT_STAT_IF_IN_OCTETS)"
echo "ing_out_oct=$(g $ING SAI_PORT_STAT_IF_OUT_OCTETS)"
echo "pfc3_tx=$(g $ING SAI_PORT_STAT_PFC_3_TX_PKTS)"
echo "pfc3_rx=$(g $ING SAI_PORT_STAT_PFC_3_RX_PKTS)"
echo "pfc4_tx=$(g $ING SAI_PORT_STAT_PFC_4_TX_PKTS)"
echo "q3_pkts=$(g $Q3 SAI_QUEUE_STAT_PACKETS)"
echo "q3_ecn=$(g $Q3 SAI_QUEUE_STAT_WRED_ECN_MARKED_PACKETS)"
echo "q3_drop=$(g $Q3 SAI_QUEUE_STAT_DROPPED_PACKETS)"
echo "q3_wm=$(sudo sonic-db-cli COUNTERS_DB hget COUNTERS:$Q3 SAI_QUEUE_STAT_SHARED_WATERMARK_BYTES 2>/dev/null)"
echo "psu1=$(sudo sonic-db-cli STATE_DB hget 'PSU_INFO|PSU1' power 2>/dev/null)"
echo "psu2=$(sudo sonic-db-cli STATE_DB hget 'PSU_INFO|PSU2' power 2>/dev/null)"
echo "temp=$(sudo sonic-db-cli STATE_DB hget 'TEMPERATURE_INFO|ASIC On-board Rear' temperature 2>/dev/null)"
echo "temp_fr=$(sudo sonic-db-cli STATE_DB hget 'TEMPERATURE_INFO|System Front Right' temperature 2>/dev/null)"
echo "oper=$(sudo sonic-db-cli APPL_DB hget PORT_TABLE:__PORT__ oper_status)"
'''

# Platform sensors in ONE python call per switch. Doing this as ~30 separate
# sonic-db-cli invocations would not finish inside the 2 s poll interval.
# Everything here is REAL and reads over gNMI too; the fan quirks are the platform's,
# not ours: speed is a PERCENTAGE (not RPM), speed_target is 0, and every fan reports
# status False / is_over_speed True while spinning normally.
PLAT_POLL = r'''sudo python3 -c "
import subprocess, json
def cli(*a):
    return subprocess.run(['sonic-db-cli','STATE_DB']+list(a),
                          capture_output=True, text=True).stdout.strip()
out = {'psu': {}, 'temp': {}, 'fan': {}, 'thr': {}, 'rpm': {}}
for k in cli('keys','PSU_INFO|*').splitlines():
    if k: out['psu'][k.split('|',1)[1]] = cli('hget',k,'power')
for k in cli('keys','TEMPERATURE_INFO|*').splitlines():
    if not k: continue
    n = k.split('|',1)[1]
    out['temp'][n] = cli('hget',k,'temperature')
    out['thr'][n]  = [cli('hget',k,'high_threshold'), cli('hget',k,'critical_high_threshold')]
for k in cli('keys','FAN_INFO|*').splitlines():
    if not k: continue
    n = k.split('|',1)[1]
    out['fan'][n] = [cli('hget',k,'speed'), cli('hget',k,'status'),
                     cli('hget',k,'speed_target')]
# RAW RPM. STATE_DB only ever carries the percentage — the SMF FPGA reports true
# RPM and the Dell plugin divides it by MAX_Z9100_FAN_SPEED (16000) before SONiC
# sees it. Reading sysfs directly is the only way to show the real number, and it
# is worth showing: 'this platform exposes no RPM' was a claim we had to retract.
import glob
for f in sorted(glob.glob('/sys/devices/platform/SMF.512/hwmon/hwmon*/fan*_input')):
    try:
        idx = f.rsplit('/',1)[1].replace('fan','').replace('_input','')
        out['rpm'][idx] = int(open(f).read().strip())
    except Exception:
        pass
print(json.dumps(out))
"'''

RPM_POLL = (r"""cat /sys/devices/platform/SMF.512/hwmon/hwmon*/fan*_input 2>/dev/null | tr '\n' ' '; """
            r"""echo; ls /sys/devices/platform/SMF.512/hwmon/hwmon*/fan*_input 2>/dev/null | tr '\n' ' '""")


def poll_rpm(host):
    """Raw fan RPM straight from the SMF FPGA's hwmon counters.

    gNMI cannot supply this and never will: STATE_DB only ever holds the
    percentage, because Dell's plugin divides the FPGA's RPM by
    MAX_Z9100_FAN_SPEED (16000) before SONiC sees it. So the real number needs
    one small SSH read alongside the gNMI platform call. Worth it — "this
    platform exposes no RPM" was a claim we had to retract, and the number is
    sitting right there.
    """
    raw = ssh(host, RPM_POLL, timeout=10) or ""
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if len(lines) < 2:
        return {}
    vals, paths = lines[-2].split(), lines[-1].split()
    out = {}
    for pth, v in zip(paths, vals):
        try:
            idx = pth.rsplit("/", 1)[1].replace("fan", "").replace("_input", "")
            out[idx] = int(v)
        except Exception:
            continue
    return out


def poll_platform(host):
    """Real PSU watts, all 8 temperatures (with thresholds) and all 12 fans."""
    raw = ssh(host, PLAT_POLL, timeout=14)
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        def f(v):
            try: return float(v)
            except Exception: return None
        psu = {k: f(v) for k, v in d.get("psu", {}).items()}
        temp = {k: f(v) for k, v in d.get("temp", {}).items()}
        fan = {k: (f(v[0]), v[1], f(v[2]) if len(v) > 2 else None)
               for k, v in d.get("fan", {}).items()}
        vals = [v[0] for v in fan.values() if v[0] is not None]
        # status now MEANS something. It used to be False on every healthy fan
        # because Dell's plugin hardcoded get_target_speed() to 0 and thermalctld
        # failed its 20% tolerance check every cycle; we patched that to return the
        # actual speed (the fix Dell already shipped on the sibling S6100), so a
        # False here is now a real fault worth looking at.
        bad = sum(1 for v in fan.values() if str(v[1]).lower() != "true")
        rpm = {k: v for k, v in (d.get("rpm") or {}).items()}
        return {
            "psu": psu,
            "psu_total": round(sum(v for v in psu.values() if v is not None), 1),
            "rpm": rpm,
            "temp": temp,
            "thr": d.get("thr", {}),
            "fan": {k: v[0] for k, v in fan.items()},
            "fan_min": min(vals, default=None),
            "fan_max": max(vals, default=None),
            "fan_n": len(fan),
            "fan_bad": bad,
        }
    return {}

def parse_kv(txt):
    d = {}
    for line in txt.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            v = v.strip()
            try: d[k] = float(v) if ("." in v) else int(v)
            except: d[k] = v if v else 0
    return d

def rate(host, key, val, now):
    pk = f"{host}:{key}"; pv = _prev.get(pk)
    _prev[pk] = (now, val)
    if pv and now > pv[0] and isinstance(val, (int, float)):
        return max(0.0, (val - pv[1]) / (now - pv[0]))
    return 0.0

def poll_loop():
    while True:
        now = time.time()
        # swb = bottleneck: egress Ethernet8 (25G to PC1), ingress Ethernet0 (from swa)
        # STAGE 2: counters over gNMI, SSH as the automatic fallback. Same Redis
        # data either way, so nothing about the numbers changes — only how they
        # travel. A gNMI failure must never interrupt a live run.
        if gnmi_read: gnmi_read.reset_stats()
        b = gnmi_read.counters("swb", "Ethernet8", "Ethernet0") if gnmi_read else None
        if not b:
            b = parse_kv(ssh("swb", SW_POLL.replace("__PORT__", "Ethernet8").replace("__ING__", "Ethernet0")))
            if b: b["via"] = "SSH"
        a = gnmi_read.counters("swa", "Ethernet8", "Ethernet0") if gnmi_read else None
        if not a:
            a = parse_kv(ssh("swa", SW_POLL.replace("__PORT__", "Ethernet8").replace("__ING__", "Ethernet0")))
            if a: a["via"] = "SSH"
        if b:
            b["out_bps"] = rate("swb", "out_oct", b.get("out_oct", 0), now) * 8
            b["in_bps"] = rate("swb", "in_oct", b.get("in_oct", 0), now) * 8
            # Always compute the reverse pair too, so switching modes never shows
            # a gap while a rate re-seeds from its first sample.
            b["rev_in_bps"] = rate("swb", "eg_in_oct", b.get("eg_in_oct", 0), now) * 8
            b["rev_out_bps"] = rate("swb", "ing_out_oct", b.get("ing_out_oct", 0), now) * 8
            if state.get("phase") == "reverse":
                # pc1 -> pc2 enters swb on Ethernet8 and leaves on Ethernet0, so
                # the normal pair reads zero. Swap them at the source rather than
                # teaching the chart about direction.
                b["in_bps"], b["out_bps"] = b["rev_in_bps"], b["rev_out_bps"]
            b["disc_rate"] = rate("swb", "out_disc", b.get("out_disc", 0), now)
            b["pfc3tx_rate"] = rate("swb", "pfc3_tx", b.get("pfc3_tx", 0), now)
            # ECN marks: SONiC/SAI doesn't collect this counter, so read the
            # Broadcom ASIC counter directly (EGR_ECN_COUNTER on xe8=Ethernet8).
            # Prefer the standard SAI counter (available since the
            # WRED_ECN_QUEUE_STAT group was enabled); fall back to the Broadcom
            # SDK counter the poller writes to /tmp/ecn_count.
            ecn_total = int(b.get("q3_ecn") or 0)
            if not ecn_total:
                ecn_raw = ssh("swb", "cat /tmp/ecn_count 2>/dev/null", timeout=8)
                try: ecn_total = int((ecn_raw or "").strip() or 0)
                except Exception: ecn_total = 0
            b["ecn_total"] = ecn_total
            b["ecn_rate"] = rate("swb", "ecn_total", ecn_total, now)
            state["swb"] = b
        if a:
            a["out_bps"] = rate("swa", "out_oct", a.get("out_oct", 0), now) * 8
            a["pfc3rx_rate"] = rate("swa", "pfc3_rx", a.get("pfc3_rx", 0), now)
            a["pfc3tx_rate"] = rate("swa", "pfc3_tx", a.get("pfc3_tx", 0), now)
            state["swa"] = a
        # hosts
        HW1="/sys/class/infiniband/rocep1s0/ports/1/hw_counters"
        HW2="/sys/class/infiniband/rocep3s0/ports/1/hw_counters"
        p2 = parse_kv(ssh("pc2", "echo tx=$(cat /sys/class/net/ens5np0/statistics/tx_bytes); echo car=$(cat /sys/class/net/ens5np0/carrier); echo cnphandled=$(cat %s/rp_cnp_handled 2>/dev/null)" % HW2))
        p1 = parse_kv(ssh("pc1", "echo rx=$(cat /sys/class/net/ens4np0/statistics/rx_bytes); echo car=$(cat /sys/class/net/ens4np0/carrier); echo ecnmark=$(cat %s/np_ecn_marked_roce_packets 2>/dev/null); echo cnpsent=$(cat %s/np_cnp_sent 2>/dev/null)" % (HW1, HW1)))
        if p2:
            p2["tx_bps"] = rate("pc2", "tx", p2.get("tx", 0), now) * 8
            p2["cnphandled_rate"] = rate("pc2", "cnphandled", p2.get("cnphandled", 0), now)
            state["pc2"] = p2
        if p1:
            p1["rx_bps"] = rate("pc1", "rx", p1.get("rx", 0), now) * 8
            p1["ecnmark_rate"] = rate("pc1", "ecnmark", p1.get("ecnmark", 0), now)
            p1["cnpsent_rate"] = rate("pc1", "cnpsent", p1.get("cnpsent", 0), now)
            state["pc1"] = p1
        # Platform sensors on BOTH switches, every 3rd sweep (~6 s): they change slowly
        # and psud only refreshes PSU_INFO every 3 s, so polling faster buys nothing.
        state["_pc"] = state.get("_pc", 0) + 1
        if state["_pc"] % 3 == 1:
            if gnmi_read: gnmi_read.reset_stats()
            for h in ("swa", "swb"):
                # gNMI is the point of this dashboard, so it goes first. If anything at
                # all goes wrong we fall back to SSH silently — a transport problem
                # must never take the demo down mid-run.
                p = gnmi_read.platform(h) if gnmi_read else None
                if not p:
                    p = poll_platform(h)
                    if p: p["via"] = "SSH"
                if p:
                    # RPM is not in STATE_DB, so it cannot come over gNMI. One
                    # extra small read merges it in without disturbing the rest.
                    if not p.get("rpm"):
                        try:
                            p["rpm"] = poll_rpm(h)
                        except Exception:
                            p["rpm"] = {}
                    state.setdefault("plat", {})[h] = p
        if gnmi_read:
            state["transport"] = {"gnmi": gnmi_read.stats["gnmi"],
                                  "fallback": gnmi_read.stats["fallback"],
                                  "err": gnmi_read.stats["last_error"],
                                  "counters_via": (b or {}).get("via", "?")}
        state["t"] = now
        # clamp the ECN-rate baseline transient (counter accumulates across restarts)
        er = state["swb"].get("ecn_rate", 0)
        if er and er > 500000: state["swb"]["ecn_rate"] = 0
        # honest state: is traffic ACTUALLY flowing? (offered > 1 Gbps at swb)
        flowing = state["swb"].get("in_bps", 0) > 1e9
        state["flowing"] = flowing
        if state["phase"] != "idle":
            if flowing: state["_last_flow"] = now
            elif now - state.get("_last_flow", now) > 12:
                state["phase"] = "idle"   # commanded flow ended / never started -> reset
        # history for the chart (offered vs delivered)
        state["hist"].append({"t": now, "off": state["swb"].get("in_bps", 0)/1e9,
                              "del": state["swb"].get("out_bps", 0)/1e9,
                              "drop": state["swb"].get("disc_rate", 0)})
        state["hist"] = state["hist"][-60:]
        time.sleep(2)

def _killall():
    # Kill the bursty-loop wrappers FIRST (tagged BENCHBURST) so they cannot respawn
    # ib_write_bw, then kill the generators themselves.
    #
    # THE BRACKET IS LOAD-BEARING. `pkill -f BENCHBURST` matches the FULL command line,
    # and this very command -- running inside the SSH session's own shell -- contains
    # the string "BENCHBURST". Without the bracket, pkill kills its own parent shell and
    # the two kills after it never run, so Stop silently leaves traffic flowing.
    # "[B]ENCHBURST" is a regex matching "BENCHBURST", but the literal text in our own
    # cmdline is "[B]ENCHBURST", which the regex does not match. So we skip ourselves.
    for h in ("pc2", "pc1"):
        ssh(h, "sudo pkill -9 -f '[B]ENCHBURST' 2>/dev/null; "
               "sudo pkill -9 -x iperf3 2>/dev/null; "
               "sudo pkill -9 -x ib_write_bw 2>/dev/null; "
               "sleep 0.5; "
               "echo left=$(pgrep -x -c ib_write_bw)", 10)
_PC2_IP = {"v": None}


def _pc2_rdma_ip():
    """PC2's address on the RoCE subnet, discovered once and cached.

    Only pc1's 10.100.1.1 is written down anywhere on this bench. Guessing the
    peer address is exactly the sort of thing that works in rehearsal and fails
    on stage, so ask the host.
    """
    if _PC2_IP["v"]:
        return _PC2_IP["v"]
    out = ssh("pc2", "ip -4 -o addr show ens5np0", 10) or ""
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    if m:
        _PC2_IP["v"] = m.group(1)
    return _PC2_IP["v"]


def _start_baseline():
    # flow A: steady ~12G rate-limited RoCE (port 18515) -> healthy, under the 25G wall
    ssh("pc1", "nohup ib_write_bw -R -d rocep1s0 -x 3 -p 18515 --report_gbits >/tmp/a.log 2>&1 & echo ok", 8)
    time.sleep(2)
    ssh("pc2", "nohup ib_write_bw -R -d rocep3s0 -x 3 -p 18515 --tos=12 --report_gbits "
               "--rate_limit=12 --rate_units=g --rate_limit_type=SW -D 86400 10.100.1.1 >/tmp/ac.log 2>&1 & echo ok", 8)
def run_action(a):
    if a == "baseline":
        _killall(); time.sleep(1)
        _start_baseline()
        state["phase"] = "baseline"; state["_last_flow"] = time.time()
    elif a == "inject":
        # steady baseline + a full-rate aggressor (port 18516): demand > 25G ->
        # ECN/PFC surge, DCQCN throttles both senders, stays lossless (0 drops).
        _killall(); time.sleep(1)
        _start_baseline()
        ssh("pc1", "nohup ib_write_bw -R -d rocep1s0 -x 3 -p 18516 --report_gbits >/tmp/b.log 2>&1 & echo ok", 8)
        time.sleep(2)
        ssh("pc2", "nohup ib_write_bw -R -d rocep3s0 -x 3 -p 18516 --tos=12 --report_gbits -D 86400 10.100.1.1 >/tmp/bc.log 2>&1 & echo ok", 8)
        state["phase"] = "inject"; state["_last_flow"] = time.time()
    elif a == "burst":
        # bursty RoCE: ~6s hard burst, ~5s quiet, on repeat — models real DC traffic
        # (a GPU all-reduce fires, then the fabric goes idle). PFC/ECN spike during
        # each burst and fall to 0 in the gaps. BENCHBURST tag lets _killall stop it.
        _killall(); time.sleep(1)
        ssh("pc1", "nohup bash -c 'BENCHBURST=1; while true; do ib_write_bw -R -d rocep1s0 -x 3 "
                   "--report_gbits >/tmp/bl.log 2>&1; sleep 0.3; done' >/tmp/bloop.log 2>&1 & echo ok", 8)
        time.sleep(2)
        ssh("pc2", "nohup bash -c 'BENCHBURST=1; while true; do ib_write_bw -R -d rocep3s0 -x 3 --tos=12 "
                   "--report_gbits -D 6 10.100.1.1 >/tmp/bc.log 2>&1; sleep 5; done' >/tmp/bcloop.log 2>&1 & echo ok", 8)
        state["phase"] = "burst"; state["_last_flow"] = time.time()
    elif a == "reverse":
        # HEALTHY REFERENCE — the control for the whole bench.
        # Same two NICs, same RoCE, same bursty software pacing. The only
        # difference is that the path never narrows: pc1 is 25G and pc2 is 100G,
        # so 25 into 100 has nothing to queue behind. If PFC and ECN read zero
        # here while the forward direction reads thousands/s, the speed step is
        # the cause and the traffic is not.
        ip = _pc2_rdma_ip()
        if not ip:
            state["phase"] = "idle"
            return
        _killall(); time.sleep(1)
        ssh("pc2", "nohup ib_write_bw -R -d rocep3s0 -x 3 -p 18517 --report_gbits >/tmp/rv.log 2>&1 & echo ok", 8)
        time.sleep(2)
        ssh("pc1", "nohup ib_write_bw -R -d rocep1s0 -x 3 -p 18517 --tos=12 --report_gbits "
                   "-D 86400 %s >/tmp/rvc.log 2>&1 & echo ok" % ip, 8)
        state["phase"] = "reverse"; state["_last_flow"] = time.time()
    elif a == "congest":
        _killall(); time.sleep(1)
        ssh("pc1", "nohup iperf3 -s -1 >/tmp/ip.log 2>&1 & echo ok", 8)
        time.sleep(1)
        ssh("pc2", "nohup iperf3 -c 10.100.1.1 -u -b 80G -P 8 -l 8000 -t 86400 >/tmp/ipc.log 2>&1 & echo ok", 8)
        state["phase"] = "congesting"; state["_last_flow"] = time.time()
    elif a == "roce":
        _killall(); time.sleep(1)
        # single QP: sustains ~25G at the bottleneck with heavy ECN/PFC (DCQCN keeps it
        # lossless near line rate). -q8 overloads 8x and DCQCN collapses it to ~0.1G.
        ssh("pc1", "nohup ib_write_bw -R -d rocep1s0 -x 3 --report_gbits >/tmp/r.log 2>&1 & echo ok", 8)
        time.sleep(2)
        ssh("pc2", "nohup ib_write_bw -R -d rocep3s0 -x 3 --tos=12 --report_gbits -D 86400 10.100.1.1 >/tmp/rc.log 2>&1 & echo ok", 8)
        state["phase"] = "roce"; state["_last_flow"] = time.time()
    elif a == "stop":
        _killall()
        time.sleep(1)
        _killall()          # second sweep: catches a generator mid-respawn
        state["phase"] = "idle"

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, ctype, body):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store"); self.end_headers()
        self.wfile.write(body.encode() if isinstance(body, str) else body)
    def _json_request(self):
        # A cross-site form can only send form or text content types; a JSON
        # content type from another origin needs a CORS preflight, which this
        # server never answers. That keeps other web pages from driving the bench.
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def do_POST(self):
        if self.path.startswith("/action/"):
            if not self._json_request():
                self._send(415, "application/json", '{"error":"send Content-Type: application/json"}'); return
            n = int(self.headers.get("Content-Length") or 0)
            if n: self.rfile.read(n)
            run_action(self.path.split("/")[-1]); self._send(200, "application/json", '{"ok":1}'); return
        if self.path != "/ask":
            self._send(404, "application/json", '{"error":"not found"}'); return
        if not self._json_request():
            self._send(415, "application/json", '{"error":"send Content-Type: application/json"}'); return
        if agent is None:
            self._send(200, "application/json",
                       json.dumps({"error": "agent module failed to load on the server"})); return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            q = json.loads(self.rfile.read(n) or b"{}").get("q", "")
        except Exception:
            self._send(200, "application/json", '{"error":"bad request"}'); return
        try:
            out = agent.ask(q, state, ssh)
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {e}"}
        self._send(200, "application/json", json.dumps(out))

    def do_GET(self):
        if self.path == "/brain":
            st = agent.brain_status() if agent else {"ok": False, "label": "agent not loaded",
                                                     "detail": "import failed"}
            self._send(200, "application/json", json.dumps(st)); return
        if self.path == "/data":
            self._send(200, "application/json", json.dumps(state))
        elif self.path.startswith("/action/"):
            self._send(405, "application/json", '{"error":"actions are POST-only"}')
        else:
            self._send(200, "text/html; charset=utf-8", PAGE)

# ---- HTML (congestion-dashboard.html, next to this file) --------------------
PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "congestion-dashboard.html"), encoding="utf-8").read() \
       if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "congestion-dashboard.html")) else "<h1>page missing</h1>"

if __name__ == "__main__":
    threading.Thread(target=poll_loop, daemon=True).start()
    _adv = os.environ.get("BENCH_ADVERTISE_HOST", "").strip()   # optional extra address, e.g. a VPN IP
    _shown = "localhost" if BIND in ("127.0.0.1", "localhost") else "<this-host>"
    print(f"[bench] Congestion Control Room on Windows -> http://{_shown}:{PORT}  (listening on {BIND})"
          + (f"  (also http://{_adv}:{PORT})" if _adv else ""))
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer((BIND, PORT), H) as httpd:
        httpd.serve_forever()
