#!/usr/bin/env python3
"""
gnmi-probe-hw.py — pull Power & Thermals over gNMI from a physical SONiC switch.

Checks, on real hardware, that power and thermals can be read over gNMI.
Community SONiC ships the telemetry agent as:
    /usr/sbin/telemetry --noTLS --port 8080 --allow_no_client_auth
so no TLS/cert setup is needed for a lab probe (insecure=True below).

Prereq on the switch (Dell Z9100 community SONiC): thermalctld is DISABLED by default
(pmon_daemon_control.json -> "skip_thermalctld": true) so TEMPERATURE_INFO is empty and
temp is ABSENT over gNMI. Enable it once to expose thermals:
    sudo sed -i 's/"skip_thermalctld": true/"skip_thermalctld": false/' \
      /usr/share/sonic/device/$(show platform summary|awk '/Platform/{print $2}')/pmon_daemon_control.json
    sudo systemctl restart pmon

Usage: gnmi-probe-hw.py <host> [port]   (port defaults to 8080)
Login: GNMI_USER / SW_PW from the environment (see .env.example).
Requires: pip install pygnmi
"""
import os, sys, json
from pygnmi.client import gNMIclient

HOST = sys.argv[1] if len(sys.argv) > 1 else sys.exit('usage: gnmi-probe-hw.py <host> [port]')
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
# SONiC's factory-default login unless overridden.
AUTH = dict(username=os.environ.get("GNMI_USER", "admin"),
            password=os.environ.get("SW_PW", "YourPaSsWoRd"))
T = (HOST, PORT)

def main():
    with gNMIclient(target=T, insecure=True, **AUTH) as gc:
        cap = gc.capabilities()
        print(f"gNMI up @ {HOST}:{PORT} — {len(cap.get('supported_models',[]))} models\n")

        print("THERMALS over gNMI (STATE_DB/TEMPERATURE_INFO):")
        r = gc.get(path=['TEMPERATURE_INFO'], target='STATE_DB', encoding='json_ietf')
        val = r['notification'][0]['update'][0]['val']
        if not val:
            print("  (empty — is thermalctld enabled? see header)")
        for name in sorted(val):
            print(f"  {name:22} {val[name].get('temperature'):>6} C   @ {val[name].get('timestamp')}")

        print("\nPOWER over gNMI (STATE_DB/PSU_INFO):")
        r = gc.get(path=['PSU_INFO'], target='STATE_DB', encoding='json_ietf')
        val = r['notification'][0]['update'][0]['val']
        for psu in sorted(val):
            i = val[psu]
            print(f"  {psu}: {i.get('power')} W  {i.get('voltage')} V  {i.get('current')} A  "
                  f"status={i.get('status')} led={i.get('led_status')} model={i.get('model')}")

    print("\nSTREAMING subscribe (TEMPERATURE_INFO, sample @2s, 3 samples):")
    sub = {'subscription': [{'path': 'TEMPERATURE_INFO', 'mode': 'sample',
                             'sample_interval': 2_000_000_000}],
           'mode': 'stream', 'encoding': 'json_ietf'}
    with gNMIclient(target=T, insecure=True, **AUTH) as gc:
        n = 0
        for resp in gc.subscribe2(subscribe=sub, target='STATE_DB'):
            note = (resp or {}).get('update') if isinstance(resp, dict) else None
            if not isinstance(note, dict):
                continue
            for u in note.get('update', []):
                v = u.get('val')
                if isinstance(v, dict) and 'ASIC On-board Rear' in v:
                    print(f"  sample {n+1}: ASIC {v['ASIC On-board Rear'].get('temperature')} C "
                          f"(gNMI ts {note.get('timestamp')})")
                    n += 1
            if n >= 3:
                break
    print(f"  -> {n} push samples received from a Subscribe (one run; repeat it before relying on it)")

if __name__ == '__main__':
    main()
