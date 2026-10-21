# ADR-0011: Run the newest official SONiC 202605 build, downloaded from SONiC on the first run

- **Status:** Accepted
- **Date:** 2026-10-21
- **Supersedes:** the image part of ADR-0001 (`netreplica/docker-sonic-vs` pinned by digest)

## Context
Until now every virtual switch ran `netreplica/docker-sonic-vs`, pinned by sha256 digest (ADR-0001). That image is a community SONiC master build from January 2022 (tag `20220111`) with FRR 7.x, republished on Docker Hub by a third party. It is almost four years older than the 202505 release on our real switches, and readers who know current SONiC see an old control plane.

The SONiC project does not publish its virtual switch image (`docker-sonic-vs`) to a container registry. It is a file, `target/docker-sonic-vs.gz` (about 225 MB), in the artifacts of SONiC's public build pipeline on Azure DevOps (organization `mssonic`). Anyone can download it without an account. SONiC's build service redirects this URL to the newest successful build of a branch:

```
https://sonic-build.azurewebsites.net/api/sonic/artifacts?branchName=202605&platform=vs&target=target/docker-sonic-vs.gz
```

On 2026-10-21 that was build 20261020.3 (build id 1252518, commit `f4c52e3`). The pipeline keeps a build's artifacts for about 60 days, so a URL for one fixed build stops working after that.

The image is GPL-licensed software (the Linux user space, FRR and more). If we hosted a copy, we would be distributing it and would have to offer the matching source.

The 202605 image runs Debian bookworm and FRR 10.5. Its `start.sh` still starts zebra (and the new mgmtd and staticd) but not bgpd, ports still start admin-down, `eth1` is still `Ethernet0`, and the Redis database numbers are unchanged. So the boot, BGP and routing steps from ADR-0007 and ADR-0008 still fit.

One problem showed up on the first live run (2026-10-21). In the 202605 image, `/usr/bin/syncd` is built with AddressSanitizer and needs `libasan.so.8`, but the image doesn't include that library. syncd exits at once (status 127), so the switch never gets its `Ethernet` ports, and every BGP next hop stays invalid. Both 202605 builds we checked have it (20261006.3 and 20261020.3). The newest 202511 build (FRR 10.4.1) and the newest master build (FRR 10.7.1) don't. With Debian's `libasan8` package added, syncd starts, also under Rosetta.

## Decision
The switches run the official community SONiC virtual switch from the newest successful 202605 build. `./run.sh virtual` downloads it from SONiC's build pipeline on the first run. We never host or redistribute it.

- [virtual-fabric/fabgen.py](../../virtual-fabric/fabgen.py) is the one place that names the image. `switch_image()` returns `docker-sonic-vs:<branch>` with `DEFAULT_SONIC_BRANCH = "202605"`. fabgen writes it into `fabric.json` as `"switch_image": {"image": ..., "official_branch": ...}` and into the containerlab topology, and `--validate` checks that the two agree.
- [run.sh](../../run.sh) reads `switch_image` from `build/fabric.json`. In `ensure_sonic_image`, it uses the image if Docker already has it. Otherwise `fetch_official_image` first asks SONiC's build service for the newest build without downloading it (a branch with no vs build stops here with a clear error), then downloads it with curl (with retries), checks that it is a valid gzip file, loads it with `docker load`, and tags it `docker-sonic-vs:<branch>`. A failed or interrupted run removes the download and the half-made image.
- Before tagging, `missing_packages` runs `ldd` on syncd and orchagent inside the image. If they need `libasan.so.8` (or `libubsan.so.1`) and the image lacks it, `build_switch_image` makes one local layer on top that installs that Debian package from deb.debian.org. The same check runs on an image named by `LAB_SONIC_IMAGE`, so an official 202605 image loaded by hand works too. The layer is built on the user's machine and never leaves it. Any other missing library stops run.sh with an error that suggests another branch. When a build ships the library, nothing is added.
- `switch_ready` now also requires the port `Ethernet0`, because `show interfaces status` answers even with no ports. `wait_boot` stops at once, with a hint, if syncd has given up on a switch.
- run.sh records where the image came from in `build/sonic-image.txt`: the build number, build id, commit and finish date (from the pipeline's public build API), the requested and final download URLs, the sha256 of the `.gz`, the image id of the official build as loaded, any local change (the added package), the final local image id and the download time. It also stores the same lines as labels (`org.energy-lab.sonic.*`) on the image, so the record comes back if `build/` is removed or the branch changes. `./run.sh status` shows the image and its build.
- Three settings, in `.env` or on the command line (on a Mac, run.sh passes them into the VM):
  - `LAB_SONIC_BRANCH` picks another branch, for example `202511`. The image is then `docker-sonic-vs:<branch>`. `master` is untested with this lab: it has bgpcfgd and lldpd changes that 202605 does not.
  - `LAB_SONIC_REFRESH=1` checks for a newer build of the branch and replaces the local image if there is one.
  - `LAB_SONIC_IMAGE` names any image Docker already has or can pull. run.sh then downloads nothing.
- [rebuild-fabric.sh](../../virtual-fabric/rebuild-fabric.sh) keeps the live fabric's image (and its branch) on a resize from the Command Center, and stops with a clear error if that image is gone from Docker.

## Alternatives considered
- **Keep the 2022 `netreplica/docker-sonic-vs` pin:** repeatable, but the switches would stay on a four-year-old SONiC and FRR 7.x, and the image is a third party's copy, not SONiC's own build.
- **Pin one fixed official build by its build id:** the pipeline deletes a build's artifacts after about 60 days, so the lab would stop working for new users within two months unless we kept moving the pin.
- **Host a copy on ghcr.io, with a source notice:** fully repeatable, but then we distribute GPL software and must provide the matching source for every copy, for as long as we host it.
- **Build the image ourselves from sonic-buildimage:** the same distribution question, plus hours of build time and a build machine to maintain.
- **Wait for SONiC to publish the image to a registry:** nothing says when, or whether, that will happen.
- **Default to 202511, whose image works as published:** it avoids the local layer, but it is one release older (FRR 10.4.1), and the goal is the newest SONiC release. `LAB_SONIC_BRANCH=202511` selects it.
- **Install libasan8 into each running switch:** every boot would need the network and apt, and orchagent has already given up on syncd by the time it could run.

## Consequences
- The switches run current community SONiC: Debian bookworm and FRR 10.5, two releases newer than the 202505 on our real switches.
- A first run on a new machine gets whatever 202605 build is newest that day. Two machines set up weeks apart can run different 202605 builds. `./run.sh status` and `build/sonic-image.txt` say which one, so a bug report can name the build. After the first run, the tagged image stays in Docker, so later runs on the same machine reuse it.
- To move a machine to the newest build, run `LAB_SONIC_REFRESH=1 ./run.sh virtual`. It works the same on Linux, WSL and a Mac, and keeps the record right.
- SONiC's newest build can be broken. If it is, `LAB_SONIC_IMAGE` runs an image you have already tested (for example one you tagged yourself), and `LAB_SONIC_BRANCH` picks another branch.
- We avoid GPL distribution obligations: users download the image from the SONiC project directly, as they would by hand. The libasan8 layer comes from Debian on the user's machine, so we distribute nothing there either.
- On 202605 the first run also needs deb.debian.org, and the switches run SONiC's AddressSanitizer build of syncd, which uses more memory than a normal build. When a 202605 build ships the library, run.sh stops adding it, with no code change. The upstream packaging gap should be reported to the SONiC project.
- The first run needs these hosts: sonic-build.azurewebsites.net (finds the newest build), `*.artifacts.visualstudio.com` (the download), dev.azure.com (build details, optional) and, for 202605, deb.debian.org (libasan8). Without them, run.sh stops with an error that names the settings above.
- SONiC publishes no checksum for the `.gz`, so the download is trusted through HTTPS to SONiC's pipeline. run.sh records its sha256 so two machines can compare builds, but it cannot verify it.
- Failure modes: a branch with no vs build (the build service answers HTTP 500) stops before any download; a broken or non-gzip download (an error or sign-in page) stops before `docker load`; a missing library other than libasan8/libubsan1 stops with a hint to use another branch; deb.debian.org unreachable stops on 202605. Each removes what it downloaded or loaded.
- The download is about 225 MB, down from about 280 MB, but the image is about 1.9 GB unpacked (with the libasan8 layer), up from about 0.8 GB.
- Tested 2026-10-21 on an Apple silicon Mac (16 GB, Lima VM with 8 GB, Rosetta), default `2 2 1` fabric, build 20261020.3 with the libasan8 layer: the syslog workaround (ADR-0007) still applies, all 4 switches booted, every BGP next hop was valid, the leaves installed both spines as ECMP next hops in the kernel (FRR marks the routes "Offloaded"), host1 reached host2, and the collector, Prometheus and the Command Center worked. The spine-failure scenario rerouted through the other spine and restored ECMP, and the traffic surge ran. Each switch used about 370 MB, so `GB_PER_SWITCH` stays at 1.5. Linux x86_64 and WSL2 are not retested yet.
- N6 in [REQUIREMENTS.md](../REQUIREMENTS.md) ("every run is the same") now holds per machine, not across machines: the image is recorded, not pinned.
- Every change of branch or a new build is a reason to rerun the Apple silicon test (ADR-0007) and the routing check (ADR-0008).
