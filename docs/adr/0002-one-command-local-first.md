# ADR-0002: One command, local first, on Linux, macOS and Windows

- **Status:** Accepted
- **Date:** 2026-10-04

## Context

We want anyone to clone this repo and run the whole lab on their own Linux, macOS or Windows machine with one command, without a cloud account and without setting up Docker, containerlab or a VM by hand. Before 2026-10-04 that was not possible. The lab could only run on a prepared cloud VM; the only local script expected a Mac with a hand-set-up Lima VM and Prometheus.

Three facts limit where the lab can run. The SONiC virtual switch image (`docker-sonic-vs`) is x86_64 Linux only, so macOS and Windows can't run it directly, and ARM machines need x86_64 translation. On Windows, containerlab can't wire switches inside Docker Desktop's engine. Each switch needs about 1.5 GB of memory, so the default fabric needs about 7 GB free.

## Decision

[run.sh](../../run.sh) is the one entry point. `./run.sh virtual [SPINES LEAVES HOSTS]` (default `2 2 1`) takes a fresh clone to a running lab. Its steps, by function name:

1. **Check:** `need_linux_x86` (x86_64 Linux, or ARM Linux with a Rosetta or QEMU handler), `check_wsl`, `remove_old_fabric` (destroys the previous fabric, if any, so its memory is free before the check), and `check_ram` (1.5 GB per switch plus 1 GB; `FORCE=1` starts anyway).
2. **Install if missing:** `ensure_docker` (get.docker.com), `ensure_containerlab` (get.containerlab.dev), and `python_env` (`.venv` from [requirements.txt](../../requirements.txt)).
3. **Build:** `generate` ([virtual-fabric/fabgen.py](../../virtual-fabric/fabgen.py)), `ensure_sonic_image` (downloads the official SONiC switch image on the first run, ADR-0011), `deploy` (containerlab), `wait_boot`, `configure` (links, BGP and ECMP), and `check_traffic` (every BGP next hop valid, and the first host pings the last).
4. **Start:** `monitoring` (Prometheus), `start_collector` and `start_dashboard`, all listening on localhost.

- **macOS** (added 2026-10-05): run.sh installs Lima with Homebrew if needed and creates a VM once: `--vm-type vz`, `--rosetta` on Apple silicon, 4 CPUs, 8 GB of memory, a 40 GB disk, Ubuntu 24.04. The VM mounts the repo at the same path, run.sh runs `./run.sh virtual` inside it, and Lima forwards the ports to the Mac's localhost. If the repo is cloned to another folder, run.sh repoints the mount (`limactl edit --mount-only`). `LAB_VM` names the VM. `LAB_VM_CPUS` and `LAB_VM_MEMORY` in [.env.example](../../.env.example) apply only when it is created.
- **Windows** (added 2026-10-05): the same command runs inside Ubuntu on WSL2, and WSL forwards the ports to Windows. run.sh stops and prints the fix for WSL 1, a repo under `/mnt`, Docker Desktop's engine, and too little memory (with a `.wslconfig` example). [.gitattributes](../../.gitattributes) keeps LF line endings so the scripts survive a Windows clone.
- **Other commands:** `./run.sh preview` shows the designed map with no switches. It needs only bash, curl and Python 3.8 or newer (Git Bash on Windows). `./run.sh status` shows what is running. `./run.sh down` stops and removes everything run.sh started, and on a Mac it also stops the VM. `./run.sh delete-vm` (Mac only) deletes the VM.

## Alternatives considered

- **Docker Desktop on Windows or macOS:** on Windows, containerlab can't wire switches inside its engine, so run.sh refuses it. A Lima VM or WSL2 Ubuntu runs the same Linux steps as plain Linux.
- **Cloud first (a script or Terraform that creates a cloud VM):** a cloud VM costs money while it runs, needs a cloud account, and isn't local. Any x86_64 Ubuntu cloud VM still works as plain Linux (see the [README](../../README.md)).

## Consequences

- Tested from fresh clones. Only the Mac run is on the current code.
  - An Apple silicon Mac (16 GB, Lima installed but no lab VM, no Docker) was up in about 4 minutes (230 s) on 202605; about 3 minutes with the 2022 image. The step that installs Lima with Homebrew is not tested yet.
  - An Ubuntu 24.04 VM (4 cores, 16 GB, no Docker) was up in under 3 minutes on the first run.sh (2026-10-04). Linux has not been re-run since the boot-check, BGP and localhost-only changes.
  - Windows ran end to end on Windows Server 2025 with WSL 3.0.1 and its default Ubuntu 26.04 (16 GB). That Windows run predates the change that starts BGP after zebra and checks every next hop, and the localhost-only change. Windows 10 and 11 desktops are not tested yet.
- The first run downloads the switch image (about 225 MB, about 1.9 GB unpacked, since ADR-0011; the 2022 image was about 280 MB), Prometheus (about 110 MB), Docker, containerlab and the Python packages. A Mac also downloads Ubuntu for the VM (about 600 MB) and Lima's container tools (about 250 MB), and keeps the VM's disk until `./run.sh delete-vm`.
- run.sh needs `sudo` on Linux and WSL. It runs the official installers as root and adds you to the `docker` group, which is root-equivalent. To skip the installers, install Docker and containerlab first.
- Platform quirks live in run.sh, so a change to it needs a run on all three platforms. WSL sets `NAME` to the PC's name, so the lab name comes from `LAB_NAME`. Ubuntu 26.04 has no `sg`, so run.sh restarts itself with `sudo -E -u` to pick up the docker group. WSL often runs without systemd, so run.sh falls back to `service docker start`.
- On Apple silicon the switches run under Rosetta: boot is slower, and they run without syslog.
- On Windows the Ubuntu window must stay open. Closing it stops WSL, and the lab with it.
