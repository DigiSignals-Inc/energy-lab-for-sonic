# ADR-0007: On Apple silicon, finish the switch boot without syslog

- **Status:** Accepted
- **Date:** 2026-10-05

## Context
The SONiC virtual switch image (`docker-sonic-vs`; since ADR-0011 the newest official 202605 build, named by `switch_image` in `fabric.json`) is x86_64 only. On a Mac, `run.sh` creates an Ubuntu 24.04 VM with Lima. On Apple silicon (`uname -m` is `arm64`), `mac_ensure_vm` adds `--rosetta` to `limactl create`, so the switch containers run through Rosetta.

On the first Apple silicon run, none of the 4 switches in the default fabric booted. Ubuntu's AppArmor profile for rsyslogd also applies to the rsyslogd inside each switch container, and it stops Rosetta from loading that binary. So rsyslogd fails to start. SONiC's boot script, `/usr/bin/start.sh`, runs with `bash -e`, so it stops at the syslog step, before it starts Redis and the rest of SONiC.

x86_64 machines (Linux, WSL2, cloud VMs) do not hit this, because nothing is translated there.

## Decision
We leave the AppArmor rule on and finish the boot without syslog. All of it is in [run.sh](../../run.sh):

- `need_linux_x86` sets `TRANSLATED=1` on `aarch64` or `arm64` when `x86_translation` finds a Rosetta or QEMU binfmt handler. On x86_64 it is never set.
- `wait_boot` checks every switch every 10 seconds. If `TRANSLATED` is set and a switch is not ready, it calls `boot_stopped_early`. That function returns true when `supervisorctl status start.sh` shows `EXITED` and `redis-cli ping` fails.
- For each such switch, `wait_boot` runs `docker exec -d <switch> bash /usr/bin/start.sh` and prints "boot stopped at the syslog step (expected on ARM); starting the rest without syslog". Started this way, the script runs without `-e`, so it carries on past the failed syslog step.
- The `resumed` list makes sure each switch gets this second run only once.
- `switch_ready` then waits for the second run to finish, the same as for a normal boot.

## Alternatives considered
- **Turn off Ubuntu's rsyslogd AppArmor profile in the VM:** this weakens the VM's security just to run one log daemon that the lab does not use.
- **Run a full x86_64 VM under QEMU emulation instead of Rosetta:** emulating the whole machine is much slower than translating only the x86_64 programs, and the boot under Rosetta is already slower than on x86_64.
- **Patch the switch image to skip syslog or drop `-e`:** we would have to build and publish our own image, a fork of the community image, instead of running SONiC's own published `docker-sonic-vs` build (ADR-0011).

## Consequences
- The lab runs on a Mac with Apple silicon. From a fresh clone on a 16 GB Mac, it is up in about 4 minutes (230 s) on 202605 (about 3 minutes with the 2022 image), with ECMP across both spines (see [README.md](../../README.md)).
- Switches on ARM run without syslog. No code in this repo reads switch syslog. The limit is listed in the README and in [ARCHITECTURE.md](../ARCHITECTURE.md).
- The second run starts `start.sh` from the top, so the steps before syslog run twice. This worked with the 2022 image. Since ADR-0011, a new machine gets the newest 202605 build, so a new build or branch is a reason to test on Apple silicon again.
- Without `-e`, if a later step fails, the script keeps going. `switch_ready` and the routing check in `check_traffic` are what catch a broken switch.
- `boot_stopped_early` does not check why the script stopped. On ARM, any failure that leaves `start.sh` exited before Redis is up gets one retry. If the switch is still not ready when `BOOT_TIMEOUT` runs out (1200 seconds by default, counted from the start of `wait_boot`), `wait_boot` stops and suggests `docker logs`.
- The same code runs on ARM Linux that uses QEMU's binfmt handler, but we have tested it only on Apple silicon.
- x86_64 machines never run this code, and their switches keep syslog.
