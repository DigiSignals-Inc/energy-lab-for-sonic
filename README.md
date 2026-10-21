# Energy Lab for SONiC

See what a SONiC network is doing, including how much power the switches draw,
on a virtual data center or on real switches. Built by DigiSignals.

One command builds a small spine-leaf data center out of
[community SONiC](https://sonicfoundation.dev/) virtual switches, sets up BGP
and ECMP, and opens a live dashboard. You can push traffic through it and watch
links, queues, temperature and power react.

## Quick start

The same three commands work on Linux and macOS. Windows runs them inside WSL2.

**Linux (x86_64)** or **macOS** (tested on Apple silicon; Intel Macs are untested), in a terminal:

```bash
git clone https://github.com/DigiSignals-Inc/energy-lab-for-sonic.git
cd energy-lab-for-sonic
./run.sh virtual
```

On a Mac, the switches need Linux, so the first run installs [Lima](https://lima-vm.io)
with [Homebrew](https://brew.sh) and creates a small Linux VM (4 CPUs, 8 GB of memory,
40 GB of disk). Everything then runs inside it, and the links below still open on your
Mac. On Apple silicon, the x86_64 switch images run through Rosetta.

**Windows** (tested on Windows Server 2025 with WSL2; Windows 10 and 11 should work the same way but are untested):

1. Open PowerShell as administrator and run `wsl --install`. Restart if asked. Then open
   **Ubuntu** from the Start menu and choose a user name and password.
2. In the Ubuntu window, run the same three commands:

   ```bash
   git clone https://github.com/DigiSignals-Inc/energy-lab-for-sonic.git
   cd energy-lab-for-sonic
   ./run.sh virtual
   ```

   Clone inside Ubuntu, not into a Windows folder such as `C:\`. If Docker Desktop is
   installed, turn off its WSL integration for Ubuntu first (Settings > Resources > WSL
   integration). `run.sh` installs its own Docker inside Ubuntu. Docker's installer
   suggests Docker Desktop and pauses for 20 seconds; let it continue.
3. Open the links in your Windows browser. Keep the Ubuntu window open while you use the
   lab: closing it stops WSL, and the lab with it.

The first run takes a few minutes. When it finishes it prints:

```
✅  energy-lab-for-sonic is up
    Command Center : http://localhost:9300
    Prometheus     : http://localhost:9090
```

Open the Command Center and press **Start incast** to send traffic across the fabric.

Want a look before installing anything? `./run.sh preview` shows the map of the fabric in a
few seconds, with no Docker or VM. It has no live data, and the scenario buttons need the full lab.

When you're done:

```bash
./run.sh down
```

This removes the switches, containers and background processes the script started.
On a Mac it also stops the Linux VM. `./run.sh delete-vm` deletes the VM to free its disk space.

## What you can do with it

- **Watch the live map:** links, traffic, BGP and ECMP, plus temperature, power and fans
  for every switch. Each value is marked as measured or modeled.
- **Run scenarios:** incast, traffic surge, cable cut, gray failure, spine failure,
  graceful drain, bad config push and thermal auto-heal. Elephant flow needs at least
  3 leaves. The congestion storm needs an ssh-mode fabric of virtual switches (it uses the
  kernel tc RED, which real Tomahawk silicon bypasses), and the GPU-cluster scenarios need a
  GPU fabric with rail loopbacks, so the dashboard greys them out in the virtual lab and
  says why.
- **Traffic Studio** (make your own traffic) is in the dashboard but not usable yet: it
  needs ssh mode, which isn't finished ([REQUIREMENTS F11](docs/REQUIREMENTS.md)), and the
  virtual lab's leaves have no loopback addresses yet.
- **Ask the assistant** what is happening and why. It works offline, or with your own AI key.
- **Query every metric** in Prometheus.
- **Use the real-switch material** in [`real-hardware/`](real-hardware/) as a reference
  for your own bench.

## Commands

| Command | What it does |
|---|---|
| `./run.sh virtual [SPINES LEAVES HOSTS]` | Builds and starts everything: switches, collector, Prometheus and the Command Center. Default size: `2 2 1` (2 spines, 2 leaves, 1 host per leaf). |
| `./run.sh preview [SPINES LEAVES HOSTS]` | The Command Center with the map of the fabric you designed, and no switches running. Works anywhere with Python 3, without Docker or a VM. |
| `./run.sh status` | Shows what is running, and which SONiC build the switches run. |
| `./run.sh down` | Stops and removes everything `run.sh` started. |
| `./run.sh delete-vm` | macOS only: deletes the Linux VM. The next `./run.sh virtual` creates a new one. |

Example of a bigger fabric: `./run.sh virtual 2 4 2`.

## What you need

- **Linux on x86_64, a Mac, or Windows with WSL2.** The SONiC virtual switch image
  is x86_64 Linux only. On a Mac, `run.sh` runs it in a Linux VM. On Windows, it
  runs in WSL2.
- **Memory:** about 1.5 GB per switch, plus 1 GB. The default `2 2 1` fabric has
  4 switches, so it needs about 7 GB free, and `run.sh` checks before it starts.
  - **Mac:** the VM gets 8 GB, so a Mac with 16 GB of memory is enough. For a bigger
    fabric, run `./run.sh delete-vm`, then `LAB_VM_MEMORY=12 ./run.sh virtual 2 4 1`.
  - **Windows:** WSL gets half of the PC's memory, so a 16 GB PC gives it 8 GB.
    `run.sh` explains how to give it more if needed.
- **`sudo` access** (Linux and WSL). If they're missing, `run.sh` installs Docker
  (from get.docker.com) and containerlab (from get.containerlab.dev). On a Mac it
  installs them inside the VM.
- **Python 3.8 or newer.** The Python packages go into a local `.venv`.
- **Downloads on the first run:** the SONiC switch image (about 225 MB, straight from
  the SONiC project's build pipeline), Prometheus
  (about 110 MB), Docker, containerlab and two Python packages (`prometheus-client`, `pygnmi`) with
  their dependencies. A Mac also downloads
  Ubuntu for the VM (about 600 MB).

Tested from a fresh clone. Only the Mac run is on the current code; the Linux run
(2026-10-04) and the Windows run predate the boot-order and localhost-only changes:

- **Ubuntu 24.04 VM** (4 cores, 16 GB, no Docker): up in under 3 minutes, with
  all 4 BGP sessions up (8 peer entries) and ECMP across both spines (with the 2022 image; not yet rerun on 202605).
- **MacBook with Apple silicon** (16 GB, Lima installed, no lab VM and no Docker), SONiC
  202605 build 20261020.3: up in about 4 minutes (230 s), with ECMP across both spines, and
  incast traffic crossing leaf, spine and leaf. Inside the VM, the switches run without
  their own syslog, because Ubuntu's security profile for rsyslogd blocks Rosetta.
  Nothing in this lab needs switch syslog.
- **Windows Server 2025 with WSL2** (16 GB; WSL 3.0.1 with its default Ubuntu, 26.04): up,
  with the links opening in the Windows browser, traffic crossing the fabric,
  incast working and the assistant answering (with the 2022 image; not yet rerun on 202605).

## Use it from another computer

The Command Center and Prometheus only answer on the computer that runs them,
because anyone who can reach the Command Center can drive the lab. To open it from
another computer, use an SSH tunnel:

```bash
ssh -L 9300:localhost:9300 you@lab-machine
```

Then open http://localhost:9300 on your own computer.

To serve it on a network instead, set `COPILOT_BIND=0.0.0.0` and a long random
`COPILOT_TOKEN` in `.env`, then open `http://<lab-machine>:9300/?token=<your token>`
once. The browser remembers it. This works when the lab runs directly on Linux. On a
Mac (Lima) or under WSL2 the ports are forwarded only to that machine's localhost, so
use the SSH tunnel there.

## Run it on a cloud VM

Any x86_64 Ubuntu VM with 4 cores and 16 GB of memory works. We tested Azure's
`Standard_D4s_v6`. Allow only SSH (port 22) in the VM's firewall, SSH in, run the
three Quick start commands, and open the Command Center through the SSH tunnel above.

## What's running

```
  SONiC switches (containerlab, docker-sonic-vs)  ◄──── actions (docker exec) ────┐
          │  counters, routes, interfaces (docker exec)                            │
          ▼                                                                        │
  collector :9200 ───► Prometheus :9090 ───► Command Center :9300 ─────────────────┘
                                             (map, scenarios, traffic, assistant)
```

- **Virtual switches** are official community SONiC 202605 (`docker-sonic-vs`, with
  FRR 10.5), wired as a spine-leaf fabric by
  [`virtual-fabric/fabgen.py`](virtual-fabric/fabgen.py). We don't host the image: the
  first `./run.sh virtual` downloads the newest successful 202605 build from SONiC's own
  build pipeline, and `./run.sh status` shows which build you have. To use another
  branch or your own image, see [Switch image](#switch-image).
- **Traffic, routes and counters are real.** Packets go through the virtual
  switches, and BGP and ECMP run as they would on hardware.
- **Energy and congestion are modeled in virtual mode.** A virtual switch has no
  fans, power supplies or buffers, so the collector models temperature, power,
  fan speed, ECN and PFC from the real traffic. Every modeled value is labeled
  `modeled`. On real switches, these come from the hardware.
- **The AI assistant** works offline by default, with built-in rules and no API
  key. You can point it at any OpenAI-compatible API instead: see
  `COPILOT_BRAIN` in [`.env.example`](.env.example). Each question then sends the
  live fabric data to that provider.

[`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) lists what the lab must do and what is out of scope,
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) explains how the pieces fit together, and
[`docs/adr/`](docs/adr/README.md) records why.

## Settings

Every setting is listed, with its default, in [`.env.example`](.env.example).
To change any of them:

```bash
cp .env.example .env
# edit .env, then run ./run.sh again
```

`.env` is git-ignored. Never commit real keys or passwords.

### Switch image

SONiC publishes its virtual switch as a build file, not in a container registry, and
keeps each build for about 60 days. So the first `./run.sh virtual` on a machine
downloads the newest successful build of the 202605 branch from
[SONiC's build pipeline](https://sonic-build.azurewebsites.net/ui/sonic/pipelines),
loads it into Docker as `docker-sonic-vs:202605`, and writes where it came from (build
number, commit, download URL, sha256) to `build/sonic-image.txt` and, as labels, on the
image itself. Later runs reuse it.
The 202605 builds leave out a library that their `syncd` needs (`libasan8`), so run.sh
adds that one Debian package to your local copy; `build/sonic-image.txt` says so.

| Setting | What it does |
|---|---|
| `LAB_SONIC_BRANCH=202511` | Another SONiC branch. The image becomes `docker-sonic-vs:202511`. |
| `LAB_SONIC_IMAGE=my-sonic:tested` | An image Docker already has or can pull. Nothing is downloaded. |
| `LAB_SONIC_REFRESH=1` | Check for a newer build of the branch, and download it if there is one. |

To move to SONiC's newest 202605 build later, run `LAB_SONIC_REFRESH=1 ./run.sh virtual`
(the same on Linux, WSL and a Mac). It replaces the old build, and
`build/sonic-image.txt` describes the new one. SONiC publishes no checksum for the image,
so the download is trusted through HTTPS to SONiC's build pipeline. A firewall must allow
sonic-build.azurewebsites.net, `*.artifacts.visualstudio.com`, dev.azure.com (build details,
optional) and, for 202605, deb.debian.org.

`master` is untested with this lab: it has bgpcfgd and lldpd changes that 202605 does not.

## Real switches

[`real-hardware/`](real-hardware/) has what we used on two Dell Z9100-ON
switches running community SONiC 202505: the bring-up runbook, the traffic-PC
setup, and the bench scripts that make the switches congest. This part needs your
own hardware: SONiC switches, and traffic PCs with fast network cards.

## Repository layout

| Folder | What's in it |
|---|---|
| [`app/`](app/) | The Command Center (dashboard and AI assistant) and the telemetry collector |
| [`virtual-fabric/`](virtual-fabric/) | The fabric generator and the script that resizes a running fabric |
| [`real-hardware/`](real-hardware/) | Z9100 runbook, traffic-PC setup and bench scripts |
| [`docs/`](docs/) | How it works |
| [`tools/`](tools/) | Telemetry coverage analyzer (reads a switch over gNMI) |

## Status and what's next

`./run.sh virtual` is verified on the current code on an Apple silicon Mac. Earlier
versions ran from a fresh clone on Linux and on Windows Server 2025 (WSL2).
Coming next:

- Replay mode: play back a recorded session on any laptop, with no switches

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md): it
explains how to run the checks, sign off your commits (DCO) and ask for a test on our
real switches. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

To run the unit tests:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tools/ -q
```

## Security

This is a lab for your own machine, and it listens only on localhost by default.
If you find a security problem, please report it privately through the repository's
**Security** tab (**Report a vulnerability**) rather than in a public issue.
[SECURITY.md](SECURITY.md) says what is in scope and what to expect.

## License

Apache License 2.0. See [LICENSE](LICENSE).

SONiC is a trademark of The Linux Foundation. This project is not affiliated with
or endorsed by The Linux Foundation or the SONiC project.
