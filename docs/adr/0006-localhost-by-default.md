# ADR-0006: The lab listens on localhost by default

- **Status:** Accepted
- **Date:** 2026-10-08

## Context
The Command Center can change the lab as well as show it. `POST /api/fabric/apply` runs [rebuild-fabric.sh](../../virtual-fabric/rebuild-fabric.sh), which runs `sudo containerlab destroy` and then `sudo containerlab deploy`. The spine-failure scenario cuts a spine's fabric links and stops its BGP. Other scenarios start traffic floods, and so do `/api/overload` and `/api/traffic`. `/api/chat` sends questions to whatever model the user has configured with their own API key.

Until 2026-10-08, [app/app.py](../../app/app.py) bound `0.0.0.0` on port 9300 and had no login. The collector on port 9200 and Prometheus on port 9090 also listened on all interfaces. On 2026-10-07 we did a security review to prepare the repo for public release. It found two ways in:

- Any device on the same network could call the API.
- Any website the user visited could call it too. The server parsed POST bodies whatever their Content-Type, so a cross-site form post worked even against localhost. A DNS-rebinding page could also reach the port under its own host name.

## Decision
- `COPILOT_BIND` defaults to `127.0.0.1`. It is set in the config section of [app/app.py](../../app/app.py) and in [.env.example](../../.env.example). If `COPILOT_BIND` is not in `LOCAL_NAMES` and `COPILOT_TOKEN` is empty, `main()` exits before it opens the port.
- `Handler._allowed` checks every GET and POST. With no token set, it answers only requests whose `Host` header names `localhost`, `127.0.0.1` or `::1` (`LOCAL_NAMES`, parsed by `_host_name`). Every other request gets 403, which stops DNS rebinding.
- With a token set, every request except `/healthz` must carry it or gets 401. The browser passes the token once as `?token=`. The reply then sets a `lab_token` cookie with `HttpOnly; SameSite=Strict`. Scripts can send the `X-Lab-Token` header instead. The check uses `hmac.compare_digest`.
- `_read_json` requires `Content-Type: application/json` on every POST and returns 415 otherwise. A browser can't send that type to another site without a CORS preflight, and the server never approves one. It also limits the body to `MAX_BODY` (64 KB) and returns 413 for anything larger.
- `do_POST` returns 400 when a field has the wrong type, for example `{"id": []}`. `Handler.timeout` closes a stalled socket after 60 seconds.
- `_send` adds `X-Frame-Options: SAMEORIGIN` to every response, so other sites can't put the page in a frame.
- `_conv` sends the model at most the last 10 turns of chat history, each cut to 2,000 characters, and cuts the question to 4,000 characters.
- The collector listens on `127.0.0.1:9200` ([fabric_exporter.py](../../app/collector/fabric_exporter.py)). `monitoring()` in [run.sh](../../run.sh) starts Prometheus with `--web.listen-address=127.0.0.1:9090`.
- The [README](../../README.md#use-it-from-another-computer) shows how to reach the lab from another computer: an SSH tunnel (`ssh -L 9300:localhost:9300 you@lab-machine`) or token mode.

## Alternatives considered
- **User accounts and a login page:** a lab that one person runs on their own machine doesn't need a user store, password handling and sessions. A shared token is enough for the network case.
- **Keep `0.0.0.0` and print a warning:** a warning doesn't stop another device on the network, or a web page in the user's own browser, from driving the lab.
- **A reverse proxy with basic auth:** this adds another component to install, configure and keep running, and the lab is meant to start with one command.

## Consequences
- By default, only the machine that runs the lab can reach it, and other websites can't drive it through the user's browser.
- Lima forwards the VM's localhost ports to the Mac, so the links still open on the Mac. A fresh Mac run on 2026-10-08 worked through this forwarding. WSL2 also forwards localhost to Windows, but we have not repeated the Windows test since this change.
- To use the lab from another computer, including a cloud VM, use an SSH tunnel or token mode.
- Token mode is plain HTTP, so the token and the cookie cross the network unencrypted. The first `?token=` URL also stays in the browser history and in the Command Center log (`.run/logs/dashboard.log` when `run.sh` starts it). Use token mode only on a network you trust. The SSH tunnel is the safer choice.
- Localhost does not separate users. Anyone else who can log in to the same machine can still reach port 9300.
- Every POST must send JSON. Without token mode, clients must also address the app as `localhost`, `127.0.0.1` or `[::1]`. A tool that reaches it under any other host name gets 403.
- The server is IPv4 only. `main()` rejects an IPv6 `COPILOT_BIND`, such as `::1`, with a clear message.
- On 2026-10-08 we tested these checks with curl. A rebinding `Host` got 403, a non-JSON POST got 415, a 2 GB `Content-Length` got 413, and `{"id": []}` got 400. In token mode, requests without the token got 401. Requests with it got 200, whether it came in the URL, the cookie or the header. A network bind with no token made the app exit.
