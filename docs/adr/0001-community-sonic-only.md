# ADR-0001: Community SONiC only, run as docker-sonic-vs in containerlab

- **Status:** Accepted. The choice of image (`netreplica/docker-sonic-vs`, pinned by digest) is superseded by [ADR-0011](0011-official-sonic-image.md); the rest still holds.
- **Date:** 2026-09-28

## Context
We want a public repo that anyone can clone and start with one command, on a laptop or a cloud VM. That rules out any switch image that needs a license, an account or a commercial agreement: we could not publish it, and a lab built on it would not run for most readers. Our two Z9100-ON switches run community SONiC 202505, and [real-hardware/](../../real-hardware/) holds what we used on them.

The virtual switches have to start on Linux, in a Lima VM on macOS and in WSL2 on Windows (ADR-0002). Every run should also get the same switch software, so a result on one machine means the same on another.

## Decision
Every virtual switch runs community SONiC. Until ADR-0011 it came from the public `netreplica/docker-sonic-vs` image, pinned by sha256 digest. Hosts are `alpine:3.20` containers. containerlab wires them together.

> **Superseded in part (2026-10-21):** the switches now run the newest official SONiC 202605 build, which run.sh downloads from SONiC's build pipeline on the first run, and fabgen.py is the one place that names the image. The bullets below about the digest and pulling the image describe the old setup. See [ADR-0011](0011-official-sonic-image.md).

- The digest is set in two places, and they must match: `SONIC_IMAGE` in [run.sh](../../run.sh) and `SONIC_IMAGE` in [virtual-fabric/fabgen.py](../../virtual-fabric/fabgen.py). `HOST_IMAGE` in fabgen.py sets the host image.
- fabgen.py writes `build/<name>.clab.yaml` with the containerlab kind `sonic-vs` for every spine and leaf, and `kind: linux` for every host.
- `./run.sh virtual` pulls the switch image if it is missing, then runs `sudo containerlab deploy` on that file.
- `--spine-model` and `--leaf-model` in fabgen.py only set a modeled label and capacity numbers. The container is always docker-sonic-vs.
- We never ship, pull or depend on an Enterprise SONiC image. [app/nodecaps.py](../../app/nodecaps.py) and `SCEN_CAPS` in [app/app.py](../../app/app.py) keep an `enterprise-sonic` node class only for a real switch (`"physical": true`) that declares `"nos": "enterprise-sonic"` in fabric.json. fabgen.py writes neither field, so `node_binding()` resolves every generated node to `virtual`.

## Alternatives considered
- **A commercial SONiC distribution's virtual image:** it needs a license, so we couldn't publish it, and most readers couldn't run the lab.
- **Build the SONiC VS image ourselves from sonic-buildimage:** it would give a current release, but we would have to build, host and maintain our own switch image. We used a published community build, pinned by digest (now ADR-0011).
- **The SONiC VS virtual-machine image (KVM):** each switch becomes a full VM and needs KVM on the host. On ARM Macs, Rosetta translates x86_64 programs but not whole x86_64 machines, so each switch would need full CPU emulation there.

## Consequences
> The first three bullets describe the 2022 image; see [ADR-0011](0011-official-sonic-image.md) for the current one.

- Anyone can run the lab. The first `./run.sh virtual` downloads about 280 MB for the switch image. It needs no account and no license key.
- Every run uses the same switch build. Upgrading is a deliberate change: update the digest in run.sh and fabgen.py together, then retest on Linux, macOS and WSL2.
- The pinned build is community SONiC master from January 2022 (image tag `20220111`), older than the 202505 release on our real switches. (Superseded: ADR-0011 moves the switches to the newest official 202605 build.)
- The image has no switching chip. SONiC's COUNTERS_DB stays empty, so [the collector](../../app/collector/fabric_exporter.py) reads kernel counters from `/sys/class/net/<port>/statistics`. Power, temperature, fans and congestion are modeled and labeled (ADR-0004).
- The image is x86_64 only. ARM machines run it through Rosetta or QEMU, and the switches boot more slowly (ADR-0007).
- Each switch needs about 1.5 GB of memory. run.sh checks free memory before it starts. fabgen.py allows at most 8 spines, 16 leaves and 4 hosts per leaf.
- ssh mode and the EVPN/VXLAN overlay view in app.py are for real switches. The virtual fabric does not use them.
- A change that needs an Enterprise image to run or test does not belong in this repo.
- New code must not add paths that only work with a commercial SONiC image.
