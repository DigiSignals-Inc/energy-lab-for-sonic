# Security policy

This lab is meant to run on your own machine. By default it listens only on localhost.
We still take security problems seriously, and we're grateful when someone reports one
privately.

## Report a vulnerability privately

**Please don't report security problems in a public issue, pull request or comment.**

Use GitHub's private vulnerability reporting:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Fill in the form. Only you and the maintainers can see it.

Or go straight to [the private report form](../../security/advisories/new).

If you can't use GitHub, email **shreyas.raghuraman@digisignals.com** with "Security" in the subject.

Please include:

- what the problem is, and what an attacker could do with it
- the steps to reproduce it, and the commit you tested (`git rev-parse --short HEAD`)
- your platform (Linux, macOS or Windows with WSL2) and which settings you changed in `.env`
- whether you'd like to be credited, and the name to use

Don't include real passwords, API keys or tokens. A placeholder is enough.

## What happens next

We are a small team, so these are our aims rather than guarantees:

- **Within 5 business days:** we confirm we received your report.
- **Within 10 business days:** we tell you whether we can reproduce it and how serious
  we think it is.
- **Within 90 days:** we release a fix or a documented workaround. We'll tell you if
  we need longer, and why.

We'll keep you updated, agree with you on when to disclose, and publish a GitHub
security advisory once a fix is on `main`. We credit reporters in the advisory unless
they prefer not to be named.

## Supported versions

We fix security problems on the `main` branch, and they reach the next release. Please
test against the latest `main`.

## What is in scope

Problems in the code in this repository, for example:

- **The Command Center** (`app/app.py`, `app/ui.html`, `app/studio.html`), on port 9300:
  - reaching or driving it from another device, or from another website in the user's
    browser, when `COPILOT_BIND` is left at its default (`127.0.0.1`)
  - getting past the `COPILOT_TOKEN` check when the lab is served on a network
  - text from a request reaching a shell command, or reading or writing files outside
    the repo
  - API keys from `.env` showing up in a page, an API response or a log
- **The collector** (`app/collector/`) and **Prometheus** as `run.sh` starts them:
  listening on anything other than `127.0.0.1`
- **`run.sh` and `virtual-fabric/`:** generated files or commands that do something unsafe
  with the fabric size, names or settings they are given
- **`real-hardware/` scripts:** the same kinds of problems in the bench scripts
- **Anything private committed by mistake:** a password, key, token or internal
  address anywhere in the repo or its history

## Known limits, not vulnerabilities

These are documented design choices
([ADR-0006](docs/adr/0006-localhost-by-default.md),
[ADR-0002](docs/adr/0002-one-command-local-first.md),
[ADR-0011](docs/adr/0011-official-sonic-image.md)):

- **Anyone who can reach the Command Center can drive the lab.** That is why it listens
  only on localhost by default. Localhost doesn't separate users: anyone else who can
  log in to the same machine can reach it.
- **Token mode is plain HTTP.** With `COPILOT_BIND=0.0.0.0` and a token, the token and
  its cookie cross the network unencrypted, and the first `?token=` link stays in the
  browser history and in `.run/logs/dashboard.log`. Use token mode only on a network
  you trust. An SSH tunnel is safer.
- **`run.sh` needs root on Linux and WSL2.** It runs the official Docker and containerlab
  installers as root, and adds you to the `docker` group, which is equivalent to root.
- **The virtual switches use SONiC's public default password** (`SW_PW` in
  `.env.example`). They are containers on your own machine. Change it on real switches.
- **The virtual switch image is downloaded, not pinned.** On the first run, `run.sh`
  downloads the newest official SONiC 202605 virtual switch build over HTTPS from SONiC's
  own build pipeline. SONiC publishes no checksum for it, so `run.sh` records its sha256
  in `build/sonic-image.txt` but can't verify it. On 202605 it also adds Debian's
  `libasan8` package in a local layer ([ADR-0011](docs/adr/0011-official-sonic-image.md)).
  The switches are lab containers, not hardened systems, so don't expose the virtual lab
  to a network you don't trust.
- **The AI assistant sends data out only if you set it up to.** If you set
  `COPILOT_BRAIN` to an online provider, each question sends the live fabric data to
  that provider.

## Problems in other projects

Please report these to the project concerned:

- SONiC itself: see the [SONiC security policy](https://github.com/sonic-net/SONiC/blob/master/SECURITY.md)
- Docker, containerlab, Prometheus, Lima, WSL or your AI provider: their own security
  contacts

If you're unsure where a problem belongs, report it to us privately and we'll help.

## Our own bench

Please don't try to access or test DigiSignals' switches, PCs or networks. They aren't
part of this repo's scope. If you want a test run on real hardware, see
[CONTRIBUTING.md](CONTRIBUTING.md#use-our-real-switches).
