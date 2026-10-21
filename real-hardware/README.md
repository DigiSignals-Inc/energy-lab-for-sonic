# real-hardware

What we used on two Dell Z9100-ON switches running community SONiC 202505.

- [HARDWARE.md](HARDWARE.md): Z9100 bring-up and integration runbook
- [PC-SETUP.md](PC-SETUP.md): setting up the two traffic machines that make the switches congest
- `bench/`: the traffic and bench scripts, plus a bench AI agent and a live congestion dashboard
  (`congestion-dashboard-win.py` serves `congestion-dashboard.html` on 127.0.0.1:8808; it has no
  login and its buttons start traffic, so set `BENCH_BIND` wider only on a closed lab network)
- `fixtures/`: telemetry captured from the real switches
- `gnmi-probe-hw.py`: checks which gNMI paths a switch answers
