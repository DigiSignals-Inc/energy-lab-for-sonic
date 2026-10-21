# ADR-0008: Wait for a full boot, start BGP after zebra, and prove routing

- **Status:** Accepted
- **Date:** 2026-10-05

## Context
`./run.sh virtual` boots the switches, runs the generated `configure-fabric.sh` to turn on BGP, and then reports the lab as up. Each switch boots with SONiC's `/usr/bin/start.sh`, which starts zebra near its end. In this image bgpd is off by default, and `configure-fabric.sh` turns it on. In FRR, bgpd resolves its next hops through zebra. A bgpd that starts before zebra answers keeps every BGP next hop "invalid". Its sessions come up, but no routes cross that switch.

Two runs on 2026-10-05 hit this:
- On Apple silicon, the x86_64 switch image runs through Rosetta and boots slowly. `run.sh` called a switch ready as soon as Redis answered and `show interfaces status` worked. But `start.sh` was still starting zebra, so `configure-fabric.sh` started bgpd first. The BGP sessions were Established, but no routes crossed the fabric.
- On the Windows (WSL2) test, bgpd on one spine was already running 22 seconds before zebra. It had started with the container: containerlab's post-deploy step for `sonic-vs` nodes runs `supervisord` and then, at once, `supervisorctl start bgpd`, long before `start.sh` starts zebra. That spine dropped its routes. The check at the time sent one ping from the first host to the last, and that ping still passed because its flow went through the other spine.

Neither "the sessions are up" nor a single ping proves that routing works on every switch.

## Decision
"Up" means that no switch reports an invalid BGP next hop and that traffic crosses the fabric. `./run.sh virtual` checks this in three steps, in this order: `wait_boot`, `configure` and `check_traffic` in [run.sh](../../run.sh).

1. **Full boot.** `switch_ready()` counts a switch as ready only when three things are true: `redis-cli ping` answers, `start.sh` has finished (`! pgrep -f "/usr/bin/[s]tart.sh"`), and `show interfaces status` works. The brackets keep `pgrep` from matching the `sh -c` command that runs it. `wait_boot` polls every 10 seconds and gives up after `BOOT_TIMEOUT` seconds (default 1200).
2. **BGP after zebra.** [virtual-fabric/fabgen.py](../../virtual-fabric/fabgen.py) generates step 2 of `configure-fabric.sh`. On each switch, that step sets `bgpd=yes` in `/etc/frr/daemons` and waits up to 60 seconds for `vtysh -d zebra -c "show version"` to answer. Then it runs `supervisorctl restart bgpd`. We use restart, not start, so that a bgpd that is already running starts again after zebra.
3. **Proof of routing.** `check_traffic()` passes only when two things are true. `vtysh -c "show bgp nexthop"` shows no `invalid` on any spine or leaf, and the first host can ping the last host (this part is skipped when the fabric has only one host). It tries up to 30 times, 3 seconds apart. If routing still fails, `run.sh` stops with an error. The error names any switches that still have invalid next hops. Prometheus, the collector and the Command Center do not start.

## Alternatives considered
- **Fixed sleeps before configuring:** Boot time depends on the machine and on translation. A sleep that is long enough for a slow boot wastes time on a fast one, and an even slower boot can still outrun it.
- **Counting Established BGP sessions:** In the first failure, every session was Established. Sessions say nothing about invalid next hops.
- **One host-to-host ping only:** With ECMP, one flow uses one spine. In the second failure, the ping passed and missed the spine that was dropping routes.
- **`supervisorctl start bgpd`:** It does nothing when bgpd is already running, which is what happened in the second failure.
- **Restarting bgpd only after a failed check:** This adds a repair path that normal runs never use. One restart after zebra answers is simpler and gets the order right every time.

## Consequences
- Startup waits longer. It waits for `start.sh` on every switch, for zebra on each switch in turn, and for the routing check.
- A broken fabric is reported at startup instead of looking healthy. If next hops are invalid, the error names those switches.
- If zebra doesn't answer within 60 seconds, step 2 restarts bgpd anyway, and `check_traffic` reports the result.
- bgpd starts with the container because containerlab's post-deploy step starts it (found 2026-10-08 in containerlab's `nodes/sonic/sonic.go`). The restart covers this. A plain `docker start` would skip that step, and a restarted container also comes back without the links containerlab wired at deploy time. That is why the spine-failure scenario cuts a spine's links and stops its bgpd instead of killing its container (changed 2026-10-08). We tested the fix on the Mac but haven't yet repeated the Windows run where the early start appeared. Any change to step 2 must keep `restart`.
- The check passes any switch whose `show bgp nexthop` output has no `invalid`. That includes a switch where vtysh fails or bgpd isn't running. It doesn't prove that every ECMP path carries traffic.
