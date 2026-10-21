# Contributing

Thanks for taking a look. This lab is small, and every bug report, idea, test run and
pull request helps. You don't need to be a SONiC expert to help. Plenty of useful work
here is plain Python, bash, HTML and writing.

By taking part, you agree to follow our [Code of Conduct](CODE_OF_CONDUCT.md). To report
behavior that breaks it, email shreyas.raghuraman@digisignals.com.

## Contents

- [The two halves of this repo](#the-two-halves-of-this-repo)
- [Run it](#run-it)
- [Check your change](#check-your-change)
- [The rules every change follows](#the-rules-every-change-follows)
- [Report a bug, share an idea or ask a question](#report-a-bug-share-an-idea-or-ask-a-question)
- [How a change gets in](#how-a-change-gets-in)
- [Sign your commits (DCO)](#sign-your-commits-dco)
- [Where to start](#where-to-start)
- [Use our real switches](#use-our-real-switches)

## The two halves of this repo

| | Virtual lab | Real hardware |
|---|---|---|
| What it is | A small spine-leaf data center of community SONiC virtual switches on your own machine | What we used on two Dell Z9100-ON switches running community SONiC 202505, with two traffic PCs |
| Where it lives | `run.sh`, `app/`, `virtual-fabric/` | `real-hardware/` |
| Who can run it | Anyone with Linux, a Mac or Windows with WSL2 | Anyone with their own SONiC switches, or us on our bench |
| Traffic, BGP, ECMP | Measured from the virtual switches | Measured from the switches |
| Power, temperature, fans, ECN, PFC | **Modeled** from the measured traffic | Read from the switch's own sensors and counters |

Most contributions touch the virtual lab, because everyone can run it. If your change
needs real switches, see [Use our real switches](#use-our-real-switches).

## Run it

You need bash and Python 3.8 or newer. CI tests Python 3.9 and 3.12, so 3.8 works but
isn't tested there. Read the [README](README.md) first: it covers memory, platforms and
what each command installs.

**Try it in seconds, with no Docker or VM:**

```bash
./run.sh preview
```

Then open http://localhost:9300. Preview draws the map of the fabric you designed and
runs the offline assistant, but no switches are running, so there is no live data.
Scenario and traffic buttons need running switches, so they don't work in preview.

**Run the full virtual lab:**

```bash
./run.sh virtual          # default: 2 spines, 2 leaves, 1 host per leaf
./run.sh status
./run.sh down             # stops the lab (on a Mac it also stops the VM; ./run.sh delete-vm removes it)
```

On Linux and WSL2 this uses `sudo` and installs Docker and containerlab if they are
missing. On a Mac it installs Lima with Homebrew and creates a Linux VM. Logs go to
`.run/logs/`.

## Check your change

Every pull request runs CI on Python 3.9 and 3.12: Python and shell syntax checks,
ShellCheck on `run.sh`, the unit tests, and a preview smoke test. CI can't start
switches, so please also tell us in the pull request what you ran.

**Unit tests** (in a local virtual environment, so nothing goes into your system Python):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tools/ -q
```

One test is skipped: it needs a reachable switch gNMI endpoint (set COVERAGE_LIVE_GNMI=HOST:PORT). ./run.sh virtual exposes none, so that is expected.

**Quick checks anyone can run:**

```bash
python3 virtual-fabric/fabgen.py --spines 2 --leaves 2 --hosts-per-leaf 1 --name lab --out /tmp/fabcheck
python3 virtual-fabric/fabgen.py --validate /tmp/fabcheck   # check the generated files agree
python3 -m py_compile app/*.py app/collector/*.py virtual-fabric/*.py tools/*.py
for f in run.sh virtual-fabric/rebuild-fabric.sh; do bash -n "$f"; done
```

**What to run, by what you changed:**

| You changed | Please run |
|---|---|
| `app/app.py`, `app/ui.html`, `app/studio.html` | `./run.sh preview` (it catches imports that only work inside `.venv`), then `./run.sh virtual` if the change touches live data or buttons |
| `app/collector/` | `./run.sh virtual`, then check your metric in Prometheus at http://localhost:9090 |
| `virtual-fabric/fabgen.py` | `--validate`, then `./run.sh virtual` at the default size and at one bigger size, such as `2 4 1` |
| `run.sh` | `./run.sh virtual` and `./run.sh down` on your platform. Say which platform you used. Platform quirks live here, so we retest on the others before merging |
| `tools/` | the unit tests above, and add a test for what you changed |
| `real-hardware/` | say what hardware and SONiC version you ran it on, or that you couldn't run it |
| Docs only | check that every command and link you mention works |

## The rules every change follows

These come from our design records in [docs/adr/](docs/adr/README.md). Read the record
before you change what it covers.

1. **Measured or modeled, always labeled** ([ADR-0004](docs/adr/0004-measured-vs-modeled-labels.md)).
   This is the most important rule in the repo.
   - Every value the lab shows is marked as measured, modeled or absent.
   - A new metric or panel needs a label. A value a switch can't produce is **absent**,
     never 0, because a 0 looks measured.
   - Never describe a modeled number as a measurement, in code, UI text, docs, issues or
     pull requests. In the virtual lab, power, temperature, fans, ECN and PFC are modeled.
   - Every new difference between platforms goes in [`app/nodecaps.py`](app/nodecaps.py).
   - Text must match what the code really does. If you find a place where it doesn't,
     that is a bug worth reporting.
2. **Community SONiC only** ([ADR-0001](docs/adr/0001-community-sonic-only.md)). Nothing
   here may need a commercial or Enterprise SONiC image to run or test. Don't add one,
   and don't add material from one.
3. **fabgen.py is the single source of truth for the fabric** ([ADR-0003](docs/adr/0003-fabric-json-single-source-of-truth.md)).
   Change the generator, not the files in `build/`. A few values are copied by hand and
   must change in both places: the size limits and the modeled hardware list (`fabgen.py`
   and `app/app.py`). The switch image is named only in `fabgen.py`
   ([ADR-0011](docs/adr/0011-official-sonic-image.md)).
4. **The Command Center uses only the Python standard library** ([ADR-0009](docs/adr/0009-python-standard-library-web-server.md)).
   Only the collector may use packages from `requirements.txt`.
5. **Safe by default** ([ADR-0006](docs/adr/0006-localhost-by-default.md)). The lab
   listens on localhost unless a token is set. New routes go after the `_allowed` check,
   POST bodies must be JSON, and text from a request never reaches a shell.
6. **Nothing private in the repo.** Never commit passwords, API keys, tokens, real IP
   addresses, hostnames, serial numbers or customer names. Put your own settings in
   `.env`, which git ignores, and use placeholders such as `<switch-a-ip>` in docs.

A change to one of these decisions needs a new design record. Copy the newest record
in [docs/adr/](docs/adr/README.md) and open it as a pull request, so we can discuss it.

## Report a bug, share an idea or ask a question

Open an issue and pick a form:

- **Bug report:** something doesn't work as the docs say.
- **Feature or idea:** something you'd like the lab to do.
- **Run my test on your real switches:** see [below](#use-our-real-switches).

For a question, open a blank issue and add the `question` label.

Search the [existing issues](../../issues) first. Issues are public, so leave out
passwords, API keys, tokens, IP addresses and hostnames. Check logs before you paste
them: in token mode, `.run/logs/dashboard.log` can contain the token.

**Security problems are different.** Don't open a public issue. Follow [SECURITY.md](SECURITY.md):
report it from the **Security** tab, or email shreyas.raghuraman@digisignals.com.

## How a change gets in

1. **For anything bigger than a small fix, open an issue first** so we can agree on the
   approach before you spend time on it.
2. **Fork** the repo and **create a branch** from `main` with a short, clear name, such as
   `fix-preview-buttons` or `docs-intel-mac`.
3. **Keep the pull request small**, with one topic. Several small pull requests are much
   easier to review than one big one.
4. **Write commit messages** like the existing ones: a short summary of what the change
   does, such as `Wait for each switch's boot script to finish`.
5. **Run the checks** in [Check your change](#check-your-change).
6. **Sign off every commit** (see [the next section](#sign-your-commits-dco)).
7. **Open the pull request.** The template asks what you changed, why and how you
   tested it. Update the README and docs in the same pull request if the behavior changes.
8. **Review.** A maintainer reads it, may ask for changes, and merges it when it is ready.
   We are a small team, so a reply can take a few days. If a week passes with no reply,
   comment on the pull request to remind us.

Your contribution is licensed under the [Apache License 2.0](LICENSE), like the rest of
the repo (Section 5 of the license). Your name and email address in the commit become
public.

## Sign your commits (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) (DCO).
By signing off a commit, you confirm that you wrote it, or have the right to submit it
under this repo's license. There is no separate agreement to sign.

Add `-s` when you commit:

```bash
git commit -s -m "Explain the preview mode in the README"
```

Git adds a line like this at the end of the message, using your `user.name` and
`user.email`:

```
Signed-off-by: Your Name <you@example.com>
```

Forgot? Fix the last commit with `git commit --amend -s --no-edit`, or every commit on
your branch with `git rebase --signoff main`. Then push again with `git push --force-with-lease`.

SONiC's own repositories use the same sign-off, plus a contributor license agreement, so
this habit carries over if you contribute upstream.

## Where to start

- Issues labeled [good first issue](../../labels/good%20first%20issue) are small and
  well described.
- Issues labeled [help wanted](../../labels/help%20wanted) are ones we'd especially
  like help with.

Areas where help is always welcome:

- **Tests.** `app/app.py`, the collector, `fabgen.py` and `run.sh` have no automated tests yet.
- **Platforms we haven't tested:** an Intel Mac, and Windows 10 or 11 desktops with WSL2.
  Linux x86_64 and WSL2 also need a fresh run on the SONiC 202605 image.
  A report that says "it worked" or "it failed here" is valuable.
- **Docs:** anything that confused you on your first run. You see the gaps that we no
  longer notice.

[docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) lists what the lab must do and its known gaps.

## Use our real switches

DigiSignals keeps a small real bench: two Dell Z9100-ON switches (Broadcom Tomahawk)
running community SONiC 202505, and two traffic PCs, one sending at 100G and one
receiving at 25G. That 4:1 mismatch gives one switch port that really congests. The
switches report power supply output, temperatures, fans, port and queue counters, PFC
and ECN. [real-hardware/](real-hardware/README.md) describes how we built it.

If your idea needs real hardware, open an issue with the **Run my test on your real
switches** form. You describe the test, and we run it and post the results on your issue.
Use it for a single, well-defined run, such as "what does the power supply read at idle
and under congestion".

Please know before you ask:

- **We run the commands.** We don't give logins to the bench to people outside the
  team. We may adjust or decline a test that could harm the switches or that the bench
  can't do.
- **We read every script before we run it,** and we don't run binaries or code we
  can't review.
- **It's a shared, small bench.** We run bench tests on our scheduled lab days, so it
  can take several weeks, and we can't promise a date.
- **Results are public.** We post them on the issue, labeled as measured on our bench,
  with the date, the switch and the SONiC version.
- **The dashboard isn't connected to the real switches yet** ([REQUIREMENTS F11](docs/REQUIREMENTS.md)).
  Bench runs use the scripts in `real-hardware/` and the SONiC command line.

DigiSignals team members book the bench through our internal channels, not through issues.
