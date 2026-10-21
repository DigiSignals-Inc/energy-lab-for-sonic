# Requirements

What energy-lab-for-sonic must do, for whom, and what it deliberately doesn't do. Each
requirement says whether it is met and how we checked. [ARCHITECTURE.md](ARCHITECTURE.md)
explains how the lab meets them, and the [ADRs](adr/README.md) explain why it is built
this way.

## Purpose

Let anyone see what a SONiC network does (traffic, routing and failures) and how
congestion relates to power and heat, on a virtual data center on their own machine.

## Who it is for

- Network engineers and students learning SONiC and data-center networking.
- SONiC community members exploring telemetry and energy ideas.
- Teams that want a reproducible lab for demos and talks.

## What it must do

| ID | Requirement | Status |
|---|---|---|
| F1 | Build a spine-leaf fabric of community SONiC switches with one command: `./run.sh virtual`. Default 2 spines, 2 leaves, 1 host per leaf; up to 8 spines, 16 leaves and 4 hosts per leaf, as memory allows. | Met |
| F2 | Configure eBGP with ECMP automatically, and report "up" only when every BGP next hop is valid and the first host reaches the last one. | Met |
| F3 | Show a live map with traffic, BGP and ECMP, plus temperature, power and fans for every switch. | Met |
| F4 | Label every value as measured, modeled or absent. Never show a missing value as zero, and show a switch that can't be read as absent. | Met |
| F5 | Run failure and traffic scenarios and show their effect. In the default lab: incast, traffic surge, cable cut, gray failure, spine failure with restore, graceful drain, bad config push and thermal auto-heal. Grey out, with the reason, any scenario this lab can't run. | Met, except two gaps below |
| F6 | Explain the fabric through an assistant that works offline, or with the user's own model. | Met |
| F7 | Expose every metric through Prometheus for the user's own queries. | Met |
| F8 | Show which telemetry fields a platform really provides (coverage audit). | Met |
| F9 | Stop and remove everything with one command: `./run.sh down`. | Met |
| F10 | Show the designed map without Docker: `./run.sh preview`. | Met |
| F11 | Run the same dashboard against real SONiC switches. | Not yet: the code path exists (ssh mode), but our switches aren't connected |

## How well it must do it

| ID | Requirement | Status |
|---|---|---|
| N1 | Runs on Linux (x86_64), macOS and Windows (WSL2). | Met on Linux, an Apple silicon Mac, and Windows Server 2025 with WSL2. Not yet tested on an Intel Mac or a Windows 10/11 desktop |
| N2 | Starts from a fresh clone with nothing to install by hand except Homebrew on a Mac and WSL on Windows. `run.sh` installs Docker, containerlab and the Python packages. | Met |
| N3 | The first run finishes in a few minutes on a good connection. | Met with the 2022 image: 2m37s on Linux, 3m21s on Windows. With 202605: about 4 minutes on a Mac (Linux and WSL2 not rerun) |
| N4 | The default fabric fits a 16 GB laptop: about 1.5 GB per switch, plus 1 GB. | Met |
| N5 | Safe by default: listens on localhost only, needs a token for any network address, ignores requests from other websites, and never passes request text to a shell. | Met; each case tested |
| N6 | Every run is the same: the switch image, Prometheus and the top-level Python packages are pinned. Docker and containerlab install their latest versions. | Met on one machine for the pinned parts. Prometheus and the top-level Python packages are pinned. The switch image is the newest official SONiC 202605 build on a machine's first run, and is recorded, not pinned (ADR-0011) |
| N7 | A failed link or spine is noticed within seconds, and a change reaches the dashboard within about half a minute. | Met: BGP notices within its 9 s hold time; the dashboard shows a change about 15–25 s later |
| N8 | Nothing leaves the machine unless the user configures an AI model. | Met |
| N9 | Open source under Apache 2.0, community SONiC only, with no commercial images. | Met |

## Out of scope

- Commercial or Enterprise SONiC images.
- Production monitoring: there are no user accounts, no HTTPS and no history across runs.
- Accurate energy numbers for virtual switches. The energy model is illustrative and
  labeled as modeled.
- High traffic rates. The virtual lab carries megabits per second, not gigabits.
- Resizing the fabric from the dashboard. Run `./run.sh virtual SPINES LEAVES HOSTS`.

## Known gaps

- **F5:** after a cut cable is restored, that link's BGP session took longer than 30
  seconds to come back.
- **F5:** on a fabric with 2 leaves the elephant-flow button isn't greyed out; it fails
  when pressed.
- **F11:** connecting real switches to the dashboard is not finished.
- **N1:** Intel Mac and Windows 10/11 desktop are not tested yet.
- **N6:** SONiC's build pipeline deletes builds after about 60 days, so the switch image
  can't be pinned across machines. Two machines set up weeks apart can run different
  202605 builds; `./run.sh status` names the build. A broken upstream build is possible:
  `LAB_SONIC_IMAGE` runs an image you have already tested (ADR-0011).
- **Switch image 202605:** the move from the 2022 image (FRR 7) to SONiC 202605 (FRR 10.5)
  was checked on an Apple silicon Mac only; Linux and WSL2 need a new run.
- There are few automated tests. CI runs syntax checks, the unit tests and a preview
  smoke test on every pull request. Every requirement above was checked by hand in a
  live lab.

Last checked: 2026-10-21.
