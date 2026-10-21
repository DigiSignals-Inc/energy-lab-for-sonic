## What this changes

<!-- One or two sentences: what changes, and why. -->

Related issue: <!-- for example "Fixes #12", or "none" -->

## How I tested it

<!-- Tick what you ran, and add anything else. See "Check your change" in CONTRIBUTING.md. -->

- [ ] `.venv/bin/python -m pytest tools/ -q`
- [ ] `python3 virtual-fabric/fabgen.py --validate build` (after `./run.sh preview` or `./run.sh virtual`)
- [ ] `./run.sh preview`
- [ ] `./run.sh virtual` (size: <!-- e.g. 2 2 1 -->) and `./run.sh down`
- [ ] On real switches (hardware and SONiC version: <!-- --> )
- [ ] Docs only: I checked that every command and link I mention works

Platform I tested on: <!-- Linux x86_64 / macOS Apple silicon / macOS Intel / Windows with WSL2 / cloud VM -->

<!-- What did you see? Paste short output or a screenshot if it helps. -->

## Checklist

- [ ] Every commit is signed off (`git commit -s`).
- [ ] Every new or changed value is labeled measured, modeled or absent, and a missing value is absent, not 0 ([ADR-0004](../blob/main/docs/adr/0004-measured-vs-modeled-labels.md)).
- [ ] UI text and docs say what the code really does, and never call a modeled number measured.
- [ ] Nothing needs a commercial or Enterprise SONiC image ([ADR-0001](../blob/main/docs/adr/0001-community-sonic-only.md)).
- [ ] `app/app.py` still imports only the Python standard library ([ADR-0009](../blob/main/docs/adr/0009-python-standard-library-web-server.md)).
- [ ] If I changed a value that is copied by hand (the size limits, the modeled hardware list), I changed it in both places.
- [ ] No passwords, keys, tokens, real IP addresses, hostnames or private names in the code, docs or commit messages.
- [ ] I updated the README or docs if the behavior changed.
- [ ] If this changes a design decision, I added a new record in `docs/adr/`.
