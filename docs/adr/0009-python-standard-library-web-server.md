# ADR-0009: The Command Center uses only the Python standard library

- **Status:** Accepted
- **Date:** 2026-10-04

## Context
The Command Center is the lab's web dashboard and JSON API. It has used only the Python standard library from the start.

`./run.sh preview` designs a fabric and shows its map in the Command Center. It runs no switches, Docker or VM. It is how someone sees the Command Center on a machine that cannot run the switches, such as Windows without WSL2. So it has to work on any machine with Python 3.8 or newer, including Git Bash on Windows, with no packages to install. On 2026-10-04 (commit e95b15c) we tested it in Git Bash on Windows 11 from a fresh clone. Preview, the map API and `./run.sh down` all worked.

The collector does need third-party packages. It uses `prometheus-client` to serve metrics and `pygnmi` to read switch telemetry over gNMI. The step in [run.sh](../../run.sh) that installs them, `python_env`, is written for Linux. It runs `.venv/bin/python`, and if it cannot create the virtual environment it uses `apt-get` to install `python3-venv`.

## Decision
The Command Center uses only the standard library. Only the collector uses third-party packages.

- [app/app.py](../../app/app.py) imports only standard library modules. It also imports [coverage_audit.py](../../app/coverage_audit.py) and [nodecaps.py](../../app/nodecaps.py) from the same folder, and those also use only the standard library.
- The server is `ThreadingHTTPServer`, which `app.py` defines from `ThreadingMixIn` and `HTTPServer` with `daemon_threads = True`. Requests go to `Handler`, a `BaseHTTPRequestHandler`.
- `do_GET` serves `/` from [app/ui.html](../../app/ui.html) and `/studio` from [app/studio.html](../../app/studio.html). It reads the file from disk on every request. It also answers `/healthz` and the JSON API under `/api/`. `do_POST` passes JSON bodies to `_post`.
- Both pages are plain HTML with one inline `<script>` each. They load no external files, and there is no build step.
- [requirements.txt](../../requirements.txt) pins `prometheus-client==0.21.1` and `pygnmi==0.8.15`. `./run.sh virtual` installs them into `.venv` through `python_env`.
- `./run.sh preview` skips `python_env`. Instead, `find_python` tries `python3`, then `python`, and uses the first that runs and is version 3.8 or newer. Preview runs both [fabgen.py](../../virtual-fabric/fabgen.py) and `app.py` with that Python. `fabgen.py` also uses only the standard library.

## Alternatives considered
- **Flask or FastAPI:** we would write less HTTP code by hand. But preview would need a virtual environment and a `pip install`, and both would need a separate setup for Windows. Preview would no longer work without installing packages.
- **A JavaScript framework with a build step:** anyone changing the UI would need Node.js and a build toolchain. Either `run.sh` would build the pages or the repo would ship built files. Today you edit `ui.html` and refresh the browser.

## Consequences
- Preview needs a bash shell with curl, and Python 3.8 or newer. Git Bash, macOS and most Linux systems include curl. Preview installs nothing from pip.
- With no framework, there are no security defaults, so `Handler` implements them by hand (see [ADR-0006](0006-localhost-by-default.md)):
  - `_allowed` checks the Host header, or checks the token when `COPILOT_TOKEN` is set.
  - `_read_json` requires `application/json` (otherwise 415) and limits bodies to 64 KiB (otherwise 413).
  - `do_POST` returns 400 for fields of the wrong type.
  - `_send` adds `X-Frame-Options` and `X-Content-Type-Options`.
  - New routes must go inside `do_GET` or `_post`, after the `_allowed` check.
- Each connection gets its own thread. `Handler.timeout` is 60 seconds, so a stalled client cannot hold a thread forever. `_send` closes the connection after every response. The `/api/snapshot` long-poll holds a thread for up to `SNAPSHOT_HOLD` (25 s). At most `SNAPSHOT_MAX_WAITERS` (48) clients can wait at once, and later clients get an answer right away. This suits a lab with a few viewers, not many users.
- `app.py` is one file of about 5,700 lines, and its routes are a chain of `if` statements, which makes it slow to read. We can split it into modules later without changing this decision.
- No automated test enforces this rule. `./run.sh virtual` runs `app.py` with `.venv`'s Python, so if `app.py` imports a package from `requirements.txt` (or one added there), it works there and fails only in preview. Test changes to `app.py` with `./run.sh preview`.
