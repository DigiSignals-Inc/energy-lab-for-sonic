#!/usr/bin/env bash
# run.sh: one command to start energy-lab-for-sonic.
#
#   ./run.sh virtual [SPINES LEAVES HOSTS]   Build a virtual SONiC data center and start everything:
#                                            switches, collector, Prometheus and the Command Center.
#                                            Linux, Windows (inside WSL2) and macOS (it creates a small
#                                            Linux VM with Lima for you). Default size: 2 2 1.
#   ./run.sh preview [SPINES LEAVES HOSTS]   Design only. The Command Center shows the map, with no
#                                            switches running. Works anywhere with Python 3.
#   ./run.sh status                          Show what is running, and which SONiC build the switches run.
#   ./run.sh down                            Stop and remove everything this script started.
#   ./run.sh delete-vm                       macOS only: delete the Linux VM to free its disk space.
#
# Optional settings: copy .env.example to .env and edit it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$REPO/build"
RUN="$REPO/.run"
LOGS="$RUN/logs"
NAME="${LAB_NAME:-lab}"   # not $NAME: WSL sets NAME to the Windows computer's name
# The switch image comes from fabric.json (fabgen.py names it; ADR-0011). By default it is the
# official community SONiC virtual switch from the newest successful build of a release branch,
# downloaded once from SONiC's own build pipeline. We never host or redistribute it.
SONIC_ARTIFACT_URL="https://sonic-build.azurewebsites.net/api/sonic/artifacts?platform=vs&target=target/docker-sonic-vs.gz&branchName="
SONIC_BUILD_API="https://dev.azure.com/mssonic/build/_apis/build/builds"
SONIC_INFO="$BUILD/sonic-image.txt"   # where the image came from (provenance)
SONIC_LABEL="org.energy-lab.sonic."      # the same provenance, as labels on the image itself
PROM_IMAGE="prom/prometheus:v3.15.0"
PROM_CONTAINER="energy-lab-for-sonic-prometheus"
GB_PER_SWITCH=1.5
ORIG_ARGS=("$@")

say()  { printf '\n==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

load_env() {
  if [ -f "$REPO/.env" ]; then set -a; . "$REPO/.env"; set +a; fi
  CC_PORT="${COPILOT_PORT:-9300}"
  NAME="${LAB_NAME:-lab}"   # again, after .env (which may set LAB_NAME)
}

x86_translation() {   # true if this Linux can run x86_64 binaries through Rosetta or QEMU
  ls /proc/sys/fs/binfmt_misc/ 2>/dev/null | grep -q -E '^(rosetta|qemu-x86_64)$'
}

need_linux_x86() {
  [ "$(uname -s)" = "Linux" ] || die "the virtual fabric needs Linux. On Windows, run this inside WSL2 (Ubuntu).
       On a Mac, ./run.sh virtual sets up a Linux VM for you."
  case "$(uname -m)" in
    x86_64|amd64) ;;
    aarch64|arm64)
      x86_translation || die "SONiC's virtual switch image is x86_64 only, and this ARM machine can't run x86_64
       programs (no Rosetta or QEMU binfmt handler). Use an x86_64 machine, or a Mac with ./run.sh virtual."
      info "ARM machine: the x86_64 switch images run through translation (Rosetta or QEMU), so booting is slower"
      TRANSLATED=1 ;;
    *) die "SONiC's virtual switch image is x86_64 only, and this machine is $(uname -m)." ;;
  esac
}

is_wsl() { grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; }

check_wsl() {   # Windows: checks that only apply inside WSL
  is_wsl || return 0
  grep -qi wsl2 /proc/sys/kernel/osrelease || die "this Ubuntu runs on WSL 1, which can't run Docker.
       In PowerShell, run: wsl --set-version <distro name> 2   (wsl -l -v shows the name)"
  case "$REPO" in /mnt/*) die "this copy of the repo is in a Windows folder ($REPO).
       Clone it inside Ubuntu instead (cd ~ && git clone <repo URL>) and run ./run.sh virtual there." ;; esac
  if command -v docker >/dev/null 2>&1 && \
     [ "$(sudo docker info --format '{{.OperatingSystem}}' 2>/dev/null)" = "Docker Desktop" ]; then
    die "Docker here is Docker Desktop's, and containerlab can't wire switches inside it.
       In Docker Desktop, open Settings > Resources > WSL integration and turn it off for this Ubuntu,
       then run this again. run.sh then installs Docker Engine inside Ubuntu."
  fi
}

check_ram() {   # $1 = number of switches; the rest = the fabric arguments, for the hint
  local n="$1" need avail
  shift
  need=$(awk -v n="$n" -v g="$GB_PER_SWITCH" 'BEGIN{printf "%.1f", n*g+1}')
  avail=$(awk '/MemAvailable/{printf "%.1f", $2/1048576}' /proc/meminfo)
  info "memory: about ${need} GB needed, ${avail} GB free"
  if awk -v a="$avail" -v n="$need" 'BEGIN{exit !(a < n)}'; then
    local hint=""
    is_wsl && hint="
       WSL gets half of the PC's memory by default. To give it more, put these two lines in
       %UserProfile%\\.wslconfig on Windows, run 'wsl --shutdown' in PowerShell, and reopen Ubuntu:
         [wsl2]
         memory=12GB"
    [ -n "${LAB_IN_VM:-}" ] && hint="
       On a Mac the switches run in the Linux VM, whose memory is set when the VM is created
       (LAB_VM_MEMORY, default 8 GB). To give it more, run on the Mac:
         ./run.sh delete-vm
         LAB_VM_MEMORY=12 ./run.sh virtual $*"
    [ -n "${REMOVED_OLD_FABRIC:-}" ] && hint="$hint
       The previous fabric was already removed to free its memory. The dashboard, collector and
       Prometheus may still be running with no switches; ./run.sh down stops them."
    [ "${FORCE:-}" = 1 ] || die "not enough free memory for $n switches.
       Try a smaller fabric (./run.sh virtual 2 2 1) or set FORCE=1 to try anyway.$hint"
  fi
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker (official script from get.docker.com)"
    is_wsl && info "On WSL its installer suggests Docker Desktop and pauses for 20 seconds. Let it continue."
    curl -fsSL https://get.docker.com | sudo sh
  fi
  # Start the Docker service. WSL2 often runs without systemd, so fall back to 'service'.
  if ! sudo docker info >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo service docker start >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do sudo docker info >/dev/null 2>&1 && break; sleep 1; done
    sudo docker info >/dev/null 2>&1 || die "Docker is installed but its service won't start (try: sudo service docker start)."
  fi
  if ! docker info >/dev/null 2>&1; then
    if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
      say "Giving $USER access to Docker (docker group)"
      sudo usermod -aG docker "$USER"
    fi
    if [ -z "${LAB_REEXEC:-}" ]; then
      info "continuing with the new group, no log-out needed"
      # Start again as yourself through sudo, which picks up the new group. (Not 'sg': Ubuntu 26.04
      # no longer ships it.) -E keeps settings such as FORCE=1; PATH is passed on explicitly.
      exec sudo -E -u "$(id -un)" env LAB_REEXEC=1 PATH="$PATH" "$0" "${ORIG_ARGS[@]}"
    fi
    die "cannot reach Docker. Log out and back in, then run this again."
  fi
}

ensure_containerlab() {
  if ! command -v containerlab >/dev/null 2>&1; then
    say "Installing containerlab (official script from get.containerlab.dev)"
    sudo -E bash -c "$(curl -sL https://get.containerlab.dev)"
  fi
}

find_python() {   # sets PY to a Python 3 that actually runs. On Windows, python3 can be a Store placeholder.
  local c
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
      PY="$(command -v "$c")"; return 0
    fi
  done
  die "Python 3.8 or newer is required. Install it from https://www.python.org/downloads/ and run this again."
}

python_env() {
  local py="$REPO/.venv/bin/python"
  if [ ! -x "$py" ]; then
    say "Setting up Python packages (.venv)"
    if ! python3 -m venv "$REPO/.venv" >/dev/null 2>&1; then
      command -v apt-get >/dev/null 2>&1 || die "could not create a Python virtual environment."
      sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv >/dev/null
      python3 -m venv "$REPO/.venv"
    fi
  fi
  "$py" -m pip install -q --disable-pip-version-check -r "$REPO/requirements.txt"
  PY="$py"
}

generate() {    # $1 spines, $2 leaves, $3 hosts per leaf
  say "Designing the fabric: $1 spine(s) x $2 leaf(s) x $3 host(s) per leaf"
  "$PY" "$REPO/virtual-fabric/fabgen.py" --spines "$1" --leaves "$2" --hosts-per-leaf "$3" \
    --name "$NAME" --exec-mode docker --out "$BUILD" | tail -5 | sed 's/^/    /'
}

remove_old_fabric() {
  if [ -f "$BUILD/$NAME.clab.yaml" ]; then
    say "Removing the previous fabric"
    sudo containerlab destroy -t "$BUILD/$NAME.clab.yaml" --cleanup >/dev/null 2>&1 || true
    REMOVED_OLD_FABRIC=1
  fi
}

image_id() { dk image inspect --format '{{.Id}}' "$1" 2>/dev/null; }

write_image_info() {   # $1 image; "key: value" lines on stdin
  { echo "# The SONiC switch image this lab runs. Written by run.sh; ./run.sh status shows it."
    echo "image: $1"
    echo "image_id: $(image_id "$1")"
    cat
  } > "$SONIC_INFO"
}

record_matches() {   # $1 image: true if build/sonic-image.txt describes this exact image
  [ "$(sed -n 's/^image: //p' "$SONIC_INFO" 2>/dev/null)" = "$1" ] &&
    [ "$(sed -n 's/^image_id: //p' "$SONIC_INFO" 2>/dev/null)" = "$(image_id "$1")" ]
}

label_record() {   # $1 image: the provenance run.sh stored on the image as labels, as "key: value" lines
  dk image inspect --format '{{range $k, $v := .Config.Labels}}{{$k}}: {{$v}}{{"\n"}}{{end}}' "$1" 2>/dev/null |
    sed -n "s/^${SONIC_LABEL//./\\.}//p"
}

restore_record() {   # $1 image: rewrite build/sonic-image.txt from the image's labels, if it has them
  local rec
  rec="$(label_record "$1")"
  [ -n "$rec" ] || rec="source: already in Docker, origin not recorded"
  write_image_info "$1" <<< "$rec"
}

sync_image_record() {   # ./run.sh status: bring build/sonic-image.txt up to date with the image in use
  local image
  image="$(python3 -c "import json; print(json.load(open('$BUILD/fabric.json'))['switch_image']['image'])" 2>/dev/null)" || return 0
  [ -n "$image" ] && dk image inspect "$image" >/dev/null 2>&1 || return 0
  record_matches "$image" || restore_record "$image"
}

show_image_info() {   # status lines from build/sonic-image.txt
  if [ ! -f "$SONIC_INFO" ]; then
    echo "  switch image: not fetched yet (./run.sh virtual fetches it)"
    return 0
  fi
  local img src bld fetched
  img="$(sed -n 's/^image: //p' "$SONIC_INFO")"
  src="$(sed -n 's/^source: //p' "$SONIC_INFO")"
  bld="$(sed -n 's/^build: //p' "$SONIC_INFO")"
  fetched="$(sed -n 's/^fetched: //p' "$SONIC_INFO")"
  echo "  switch image: $img${src:+ ($src)}"
  [ -z "$bld" ] || echo "    build $bld${fetched:+, fetched $fetched}"
  echo "    details: ${SONIC_INFO#"$REPO/"}"
}

on_host() {   # $1 = a command to run on a switch: how to type it where the user is (on a Mac, through the VM)
  if [ -n "${LAB_IN_VM:-}" ]; then printf 'limactl shell %s sudo %s' "$VM" "$1"; else printf '%s' "$1"; fi
}

label_value() {   # $1 text: quoted for a Dockerfile LABEL line
  local v="$1"
  v="${v//\\/\\\\}"; v="${v//\"/\\\"}"; v="${v//\$/\\\$}"
  printf '"%s"' "$v"
}

sonic_cleanup() {   # on any exit from fetch_official_image: drop the download and a half-done image
  [ -z "${SONIC_TMP:-}" ] || rm -rf "$SONIC_TMP"
  [ -z "${SONIC_LOADED:-}" ] || docker rmi "$SONIC_LOADED" >/dev/null 2>&1 || true
  SONIC_TMP=""; SONIC_LOADED=""
}

missing_packages() {   # $1 image: sets ADDED_PKGS to the Debian packages its syncd/orchagent need but lack
  # Some SONiC builds link syncd with AddressSanitizer but leave its library out of the image
  # (all 202605 builds we checked, October 2026). syncd then can't start, and the switch has
  # no ports. build_switch_image then adds the library from Debian in a local layer, on this machine.
  local libs lib
  ADDED_PKGS=""
  libs="$(docker run --rm --platform linux/amd64 --entrypoint sh "$1" -c \
    'ldd /usr/bin/syncd /usr/bin/orchagent 2>/dev/null | sed -n "s/^[[:space:]]*\([^[:space:]]*\) => not found.*/\1/p" | sort -u')"
  for lib in $libs; do
    case "$lib" in
      libasan.so.8)  ADDED_PKGS="$ADDED_PKGS libasan8" ;;
      libubsan.so.1) ADDED_PKGS="$ADDED_PKGS libubsan1" ;;
      *) die "this SONiC build's syncd or orchagent needs $lib, which its image leaves out.
       Set LAB_SONIC_BRANCH to another branch (for example 202511), or LAB_SONIC_IMAGE to an image you have." ;;
    esac
  done
}

build_switch_image() {   # $1 base image, $2 tag to create, $3 "key: value" lines to store as labels
  # Adds $ADDED_PKGS (from missing_packages) and the labels in one local layer. docker create +
  # commit, not docker build: no buildx needed, and no build cache left behind. The commit
  # restores the image's own entrypoint and command.
  local base="$1" image="$2" labels="${3:-}" cid ep cmd script="true" ok=1 changes=() line
  if [ -z "$ADDED_PKGS" ] && [ -z "$labels" ]; then
    [ "$base" = "$image" ] || docker tag "$base" "$image"
    return 0
  fi
  [ -z "$ADDED_PKGS" ] || say "Adding$ADDED_PKGS from Debian: this build's syncd needs it, but the image leaves it out"
  ep="$(docker image inspect --format '{{json .Config.Entrypoint}}' "$base")"; [ "$ep" != null ] || ep="[]"
  cmd="$(docker image inspect --format '{{json .Config.Cmd}}' "$base")"; [ "$cmd" != null ] || cmd="[]"
  changes=(--change "ENTRYPOINT $ep" --change "CMD $cmd")
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    changes+=(--change "LABEL $SONIC_LABEL${line%%: *}=$(label_value "${line#*: }")")
  done <<< "$labels"
  [ -z "$ADDED_PKGS" ] || script="apt-get update -qq &&
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends$ADDED_PKGS >/dev/null &&
    rm -rf /var/lib/apt/lists/*"
  cid="$(docker create --platform linux/amd64 --entrypoint sh "$base" -c "$script")" || ok=0
  if [ "$ok" = 1 ] && [ -n "$ADDED_PKGS" ]; then
    docker start -a "$cid" >/dev/null 2>&1 && [ "$(docker inspect --format '{{.State.ExitCode}}' "$cid")" = 0 ] || ok=0
  fi
  [ "$ok" = 0 ] || docker commit "${changes[@]}" "$cid" "$image" >/dev/null || ok=0
  [ -z "$cid" ] || docker rm "$cid" >/dev/null 2>&1 || true
  [ "$ok" = 1 ] && return 0
  [ -n "$ADDED_PKGS" ] || die "could not tag the SONiC image as $image."
  die "could not add$ADDED_PKGS to the SONiC image $image (it comes from deb.debian.org).
       Check the network and run this again.${LAB_SONIC_IMAGE:+ LAB_SONIC_IMAGE must be a name:tag for this.}"
}

fetch_official_image() {   # $1 local tag to create, $2 SONiC branch
  local image="$1" branch="$2" code="" loc="" gz final sha seg id="" meta="" loaded official old change rec
  local opts=(-fL --retry 5 --retry-delay 5 --connect-timeout 20 --progress-bar)
  case "$(curl --help all 2>/dev/null)" in *--retry-all-errors*) opts+=(--retry-all-errors) ;; esac
  # First ask SONiC's build service which build is newest, without downloading anything. It answers
  # with a redirect to that build's artifact, or with an error page if the branch has no vs build.
  # (No curl --retry here: with -o /dev/null, curl 8.5 fails its retries with a write error.)
  for _ in 1 2 3; do
    read -r code loc < <(curl -s -o /dev/null --connect-timeout 20 -m 60 \
      -w '%{http_code} %{redirect_url}\n' "$SONIC_ARTIFACT_URL$branch" 2>/dev/null || echo '000 -') || true
    [ "${code:-000}" = 000 ] || break
    sleep 3
  done
  case "$code" in
    30[1278]) [ -n "$loc" ] || die "SONiC's build service sent no download link for branch '$branch'. Run this again." ;;
    000|"") die "could not reach sonic-build.azurewebsites.net to find the newest SONiC $branch build.
       Check the network and run this again." ;;
    *) die "SONiC's build service has no downloadable vs build for branch '$branch' (HTTP $code).
       Check LAB_SONIC_BRANCH (for example 202605 or 202511), or set LAB_SONIC_IMAGE to an image you have.
       If the branch is right, the service may be having trouble: run this again later." ;;
  esac
  # The link names the pipeline artifact in base64 text that includes the build id. Best effort:
  # if the link ever looks different, the build id is just left out of the record.
  case "$loc" in
    */_apis/artifact/*)
      seg="${loc#*/_apis/artifact/}"; seg="${seg%%/*}"
      while [ $(( ${#seg} % 4 )) -ne 0 ]; do seg="$seg="; done
      id="$( { printf '%s' "$seg" | base64 -d 2>/dev/null || true; } |
        LC_ALL=C sed -n 's#.*/buildId/\([0-9][0-9]*\)/.*#\1#p' 2>/dev/null)" || id="" ;;
  esac
  old="$(image_id "$image" || true)"
  if [ -n "$old" ] && [ -n "$id" ] && label_record "$image" | grep -q "^build_page: .*buildId=$id\$"; then
    record_matches "$image" || restore_record "$image"
    info "switch image: $image is already SONiC's newest $branch build (build id $id)"
    return 0
  fi
  if [ -n "$id" ]; then   # build number and commit from SONiC's public build API, best effort
    meta="$(curl -fsS -m 20 "$SONIC_BUILD_API/$id?api-version=7.0" 2>/dev/null | "$PY" -c '
import json, sys
d = json.load(sys.stdin)
print("%s (build id %s, commit %s, finished %s)" % (d.get("buildNumber") or "?", d.get("id") or "?",
      (d.get("sourceVersion") or "?")[:12], (d.get("finishTime") or "?")[:10]))' 2>/dev/null || true)"
    [ -n "$meta" ] || meta="build id $id"
  fi
  say "Downloading the official SONiC $branch virtual switch image (one time, about 225 MB)"
  info "the newest successful $branch build${meta:+: $meta}, straight from SONiC's build pipeline"
  SONIC_TMP="$(mktemp -d)"; SONIC_LOADED=""
  trap sonic_cleanup EXIT
  gz="$SONIC_TMP/docker-sonic-vs.gz"
  if ! final="$(curl "${opts[@]}" -o "$gz" -w '%{url_effective}' "$loc")"; then
    die "could not download the SONiC $branch switch image from SONiC's build pipeline.
       Check the network and run this again. If SONiC's newest $branch build is broken, set
       LAB_SONIC_BRANCH to another release branch, or LAB_SONIC_IMAGE to an image you already have."
  fi
  sha="$(sha256sum "$gz" | cut -d' ' -f1)"
  gzip -t "$gz" 2>/dev/null || die "the download from SONiC's build pipeline is not a valid .gz (an error page
       or a broken transfer; sha256 $sha). Run this again, or set LAB_SONIC_BRANCH / LAB_SONIC_IMAGE."
  say "Loading it into Docker"
  if ! loaded="$(docker load -i "$gz" 2>&1)"; then
    die "Docker could not load the SONiC image (sha256 $sha): $loaded"
  fi
  loaded="$(printf '%s\n' "$loaded" | sed -n 's/^Loaded image: //p; s/^Loaded image ID: //p' | tail -1)"
  [ -n "$loaded" ] || die "Docker loaded the SONiC image file but named no image (sha256 $sha). Run this again."
  rm -rf "$SONIC_TMP"; SONIC_TMP=""
  [ "$loaded" = "$image" ] || SONIC_LOADED="$loaded"   # removed again if a later step fails
  official="$(image_id "$loaded")"
  missing_packages "$loaded"
  change="none"
  [ -z "$ADDED_PKGS" ] || change="added the Debian package(s)$ADDED_PKGS in a local layer (syncd needs them; the image leaves them out)"
  rec="source: official SONiC build pipeline, branch $branch
build: ${meta:-unknown}
build_page: ${id:+https://dev.azure.com/mssonic/build/_build/results?buildId=$id}
requested_url: $SONIC_ARTIFACT_URL$branch
download_url: $final
sha256_gz: $sha
official_image_id: $official
local_change: $change
fetched: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  build_switch_image "$loaded" "$image" "$rec"
  # The loaded tag (docker-sonic-vs:latest) is not needed any more. A bare image id stays: it is the base.
  case "$loaded" in sha256:*|"$image") ;; *) docker rmi "$loaded" >/dev/null 2>&1 || true ;; esac
  SONIC_LOADED=""; trap - EXIT
  # A refresh leaves the previous build untagged: remove it, unless something still uses it.
  [ -z "$old" ] || [ "$old" = "$(image_id "$image")" ] || docker rmi "$old" >/dev/null 2>&1 || true
  write_image_info "$image" <<< "$rec"
  info "tagged it $image: SONiC $branch build ${meta:-unknown}"
}

use_own_image() {   # $1 = the image LAB_SONIC_IMAGE names: pull it if needed, and add missing libraries
  local image="$1" src
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    say "Pulling the switch image $image (LAB_SONIC_IMAGE)"
    docker pull "$image" || die "Docker has no image $image and can't pull it.
       Load it first (docker load -i <file>, then docker tag), or unset LAB_SONIC_IMAGE."
    src="source: docker pull (LAB_SONIC_IMAGE)
digest: $(docker image inspect --format '{{join .RepoDigests " "}}' "$image" 2>/dev/null)
fetched: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  elif record_matches "$image"; then
    info "switch image: $image (LAB_SONIC_IMAGE; ./run.sh status shows its build)"
    return 0
  else
    src="$(label_record "$image")"
    [ -n "$src" ] || src="source: your image (LAB_SONIC_IMAGE)"
  fi
  # An official 202605 image loaded by hand also lacks libasan8: add it here as well.
  missing_packages "$image"
  build_switch_image "$image" "$image" ""
  [ -z "$ADDED_PKGS" ] || src="$(printf '%s\n' "$src" | grep -v '^local_change: ')
local_change: added the Debian package(s)$ADDED_PKGS in a local layer (syncd needs them; the image leaves them out)"
  write_image_info "$image" <<< "$src"
  info "switch image: $image (LAB_SONIC_IMAGE)"
}

ensure_sonic_image() {   # the switch image named in fabric.json: reuse it, or fetch it once
  local image branch
  read -r image branch <<< "$("$PY" -c "import json; s=json.load(open('$BUILD/fabric.json'))['switch_image']; print(s['image'], s.get('official_branch') or '-')")"
  if [ "$branch" = - ]; then use_own_image "$image"; return 0; fi
  if docker image inspect "$image" >/dev/null 2>&1; then
    if [ "${LAB_SONIC_REFRESH:-}" != 1 ]; then
      # Keep the record if it describes this exact image; otherwise rebuild it from the image's labels.
      record_matches "$image" || restore_record "$image"
      info "switch image: $image (already here; ./run.sh status shows its build)"
      return 0
    fi
    info "LAB_SONIC_REFRESH=1: checking for a newer $branch build"
  fi
  fetch_official_image "$image" "$branch"
}

deploy() {
  say "Starting the switches with containerlab"
  sudo containerlab deploy -t "$BUILD/$NAME.clab.yaml" 2>&1 | tail -3 | sed 's/^/    /'
}

switch_ready() {   # $1 = switch container
  # Ready = Redis answers, SONiC's boot script has finished, and the ports exist. The boot script
  # starts zebra near its end, and BGP must start after zebra: if it starts first, its next hops
  # stay invalid and no routes cross the fabric (seen on slow, translated boots).
  # "show interfaces status" answers even with no ports, so check that the first port exists too.
  docker exec "$1" sh -c 'redis-cli ping >/dev/null 2>&1 && ! pgrep -f "/usr/bin/[s]tart.sh" >/dev/null &&
    show interfaces status >/dev/null 2>&1 && ip link show Ethernet0 >/dev/null 2>&1' 2>/dev/null
}

syncd_failed() {   # $1 = switch container: the boot script is done, but syncd gave up (so no ports)
  docker exec "$1" sh -c '! pgrep -f "/usr/bin/[s]tart.sh" >/dev/null &&
    supervisorctl status syncd 2>/dev/null | grep -q FATAL' 2>/dev/null
}

boot_stopped_early() {   # $1 = switch container
  # On ARM, Ubuntu's AppArmor rule for rsyslogd also matches the rsyslogd inside each switch and blocks
  # Rosetta or QEMU from loading it. SONiC's boot script runs with 'bash -e', so it stops at that step,
  # before Redis and the rest. The rule stays on; run.sh runs the script again without -e (no syslog).
  docker exec "$1" sh -c 'supervisorctl status start.sh | grep -q EXITED && ! redis-cli ping >/dev/null 2>&1' 2>/dev/null
}

wait_boot() {
  say "Waiting for SONiC to boot on every switch (usually a few minutes)"
  local switches prefix total ready start s resumed=" "
  switches="$("$PY" -c "import json; f=json.load(open('$BUILD/fabric.json')); print(' '.join(n['id'] for n in f['nodes'] if n['kind'] in ('spine','leaf')))")"
  prefix="$("$PY" -c "import json; print(json.load(open('$BUILD/fabric.json'))['exec']['container_prefix'])")"
  total=$(echo $switches | wc -w | tr -d ' ')
  start=$(date +%s)
  while :; do
    ready=0
    for s in $switches; do
      if switch_ready "${prefix}${s}"; then
        ready=$((ready+1))
      elif syncd_failed "${prefix}${s}"; then
        die "SONiC's syncd failed to start on $s, so the switch has no ports.
       See why with: $(on_host "docker exec ${prefix}$s timeout 10 /usr/bin/syncd_start.sh")
       Try another build with LAB_SONIC_BRANCH (for example 202511), or LAB_SONIC_IMAGE."
      elif [ -n "${TRANSLATED:-}" ] && [ "${resumed#* $s }" = "$resumed" ] && boot_stopped_early "${prefix}${s}"; then
        info "$s: boot stopped at the syslog step (expected on ARM); starting the rest without syslog"
        docker exec -d "${prefix}${s}" bash /usr/bin/start.sh
        resumed="$resumed$s "
      fi
    done
    if [ "$ready" = "$total" ]; then info "all $total switches are ready"; break; fi
    if [ $(( $(date +%s) - start )) -gt "${BOOT_TIMEOUT:-1200}" ]; then
      die "switches are still booting after $(( ${BOOT_TIMEOUT:-1200} / 60 )) minutes ($ready/$total ready).
       Check one with: $(on_host "docker logs ${prefix}<switch>")"
    fi
    info "$ready/$total switches ready…"
    sleep 10
  done
}

configure() {
  say "Configuring links, BGP and ECMP"
  bash "$BUILD/configure-fabric.sh" 2>&1 | tail -3 | sed 's/^/    /'
}

check_traffic() {   # every BGP next hop valid on every switch, and the first host reaches the last one
  local a b ip prefix switches s bad=""
  read -r a b ip prefix <<< "$("$PY" -c "import json; f=json.load(open('$BUILD/fabric.json')); h=[n for n in f['nodes'] if n['kind']=='host']; print(h[0]['id'], h[-1]['id'], h[-1]['ip'], f['exec']['container_prefix'])")"
  switches="$("$PY" -c "import json; print(' '.join(n['id'] for n in json.load(open('$BUILD/fabric.json'))['nodes'] if n['kind'] in ('spine','leaf')))")"
  say "Checking that routing works on every switch"
  for _ in $(seq 1 30); do
    bad=""
    for s in $switches; do
      docker exec "${prefix}$s" vtysh -c "show bgp nexthop" 2>/dev/null | grep -q invalid && bad="$bad $s"
    done
    if [ -z "$bad" ] && { [ "$a" = "$b" ] || docker exec "${prefix}$a" ping -c 1 -W 1 "$ip" >/dev/null 2>&1; }; then
      info "all BGP next hops are valid, and $a reaches $b ($ip)"
      return 0
    fi
    sleep 3
  done
  die "the switches are up, but routing isn't working.${bad:+ Invalid BGP next hops on:$bad.}
       Run ./run.sh virtual again. If it happens again, please open an issue and include this output."
}

monitoring() {
  say "Starting Prometheus"
  docker rm -f -v "$PROM_CONTAINER" >/dev/null 2>&1 || true
  # Host network so it can scrape the collector on 127.0.0.1:9200. It listens on localhost only,
  # and --rm deletes it (and its data) when it stops: the lab needs no history across runs.
  docker run -d --rm --name "$PROM_CONTAINER" --network host \
    -v "$BUILD/prometheus.yml:/etc/prometheus/prometheus.yml:ro" "$PROM_IMAGE" \
    --config.file=/etc/prometheus/prometheus.yml --web.listen-address=127.0.0.1:9090 >/dev/null
}

dk() {   # docker, through sudo when this shell hasn't picked up the docker group yet
  if docker info >/dev/null 2>&1; then docker "$@"; else sudo -n docker "$@"; fi
}

start_bg() {    # $1 name, then env assignments and the command
  local name="$1"; shift
  nohup env "$@" > "$LOGS/$name.log" 2>&1 &
  echo $! > "$RUN/$name.pid"
}

stop_pid() {
  local f="$RUN/$1.pid"
  if [ -f "$f" ]; then kill "$(cat "$f")" 2>/dev/null || true; rm -f "$f"; fi
}

port_free() {
  "$PY" -c "import socket,sys; s=socket.socket(); sys.exit(1 if s.connect_ex(('127.0.0.1', $1))==0 else 0)"
}

start_collector() {
  stop_pid collector
  say "Starting the telemetry collector"
  start_bg collector FABRIC_JSON="$BUILD/fabric.json" REF_BPS="${REF_BPS:-5000000}" LAG="${LAG:-0.2}" \
    PYTHONUNBUFFERED=1 "$PY" "$REPO/app/collector/fabric_exporter.py"
}

start_dashboard() {
  stop_pid dashboard
  sleep 1
  port_free "$CC_PORT" || die "port $CC_PORT is already in use. Set COPILOT_PORT in .env, or stop whatever is using it."
  say "Starting the Command Center"
  start_bg dashboard FABRIC_JSON="$BUILD/fabric.json" CONFIGURE_SH="$BUILD/configure-fabric.sh" \
    COPILOT_PORT="$CC_PORT" PROM_URL="${PROM_URL-http://127.0.0.1:9090}" PYTHONUNBUFFERED=1 "$PY" "$REPO/app/app.py"
  for _ in $(seq 1 30); do
    curl -fsS -m 2 "http://127.0.0.1:$CC_PORT/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  die "the Command Center did not start. See $LOGS/dashboard.log"
}

show_urls() {   # $1 = full | preview
  echo
  echo "✅  energy-lab-for-sonic is up"
  echo "    Command Center : http://localhost:$CC_PORT"
  if [ "$1" = full ]; then
    echo "    Prometheus     : http://localhost:9090"
  else
    echo "    (preview: the map only, with no live data. Use './run.sh virtual' for the real thing.)"
  fi
  echo "    Logs           : $LOGS"
  echo "    Stop           : ./run.sh down"
  echo "    Only this computer can open these links (see the README to reach them from another one)."
  is_wsl && echo "    Keep this Ubuntu window open: closing it stops WSL, and the lab with it."
  return 0
}

status() {
  local n f
  for n in dashboard collector; do
    f="$RUN/$n.pid"
    if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then echo "  $n: running (pid $(cat "$f"))"
    else echo "  $n: stopped"; fi
  done
  if command -v docker >/dev/null 2>&1 && dk info >/dev/null 2>&1; then
    echo "  switches and hosts: $(dk ps -q --filter "name=clab-$NAME-" | wc -l | tr -d ' ') containers"
    n="$(dk ps --format '{{.Status}}' --filter "name=^${PROM_CONTAINER}\$" | head -1)"
    echo "  prometheus: ${n:-stopped}"
    sync_image_record
  fi
  show_image_info
}

down() {
  say "Stopping"
  stop_pid dashboard
  stop_pid collector
  if command -v docker >/dev/null 2>&1; then
    sudo docker rm -f -v "$PROM_CONTAINER" >/dev/null 2>&1 || true
    if [ -f "$BUILD/$NAME.clab.yaml" ] && command -v containerlab >/dev/null 2>&1; then
      sudo containerlab destroy -t "$BUILD/$NAME.clab.yaml" --cleanup >/dev/null 2>&1 || true
    fi
  fi
  info "done"
}

# ---- macOS: run the Linux steps inside a small Lima VM ------------------------------------
# SONiC's virtual switch image is Linux/x86_64 only. On a Mac, this creates (once) a Linux VM
# that mounts this folder at the same path, runs ./run.sh inside it, and Lima forwards the
# dashboard ports to the Mac's localhost. Apple silicon uses Apple's virtualization with
# Rosetta for the x86_64 images. Settings: LAB_VM (name), LAB_VM_CPUS, LAB_VM_MEMORY (GiB).
load_env
VM="${LAB_VM:-energy-lab-for-sonic}"

# limactl writes its own log lines (level=info, and sometimes a harmless level=error) to
# stderr. Keep them out of the way, and show them only if the command fails.
lima_quiet() {
  local out
  if ! out=$("$@" 2>&1); then
    printf '%s\n' "$out" >&2
    return 1
  fi
}

mac_ensure_vm() {
  if ! command -v limactl >/dev/null 2>&1; then
    command -v brew >/dev/null 2>&1 || die "Lima is needed to run the switches on a Mac.
       Install Homebrew (https://brew.sh), then run: brew install lima"
    say "Installing Lima (brew install lima)"
    brew install lima
  fi
  if ! limactl list -q 2>/dev/null | grep -qx "$VM"; then
    say "Creating the Linux VM '$VM' (one time; it downloads Ubuntu, about 600 MB)"
    local extra=()
    [ "$(uname -m)" = "arm64" ] && extra=(--rosetta)
    limactl create --tty=false --name "$VM" --vm-type vz ${extra[@]+"${extra[@]}"} \
      --cpus "${LAB_VM_CPUS:-4}" --memory "${LAB_VM_MEMORY:-8}" --disk 40 \
      --mount-only "$REPO:w" template:ubuntu-24.04 >/dev/null   # stderr kept: download progress
  elif [ -n "${LAB_VM_MEMORY:-}${LAB_VM_CPUS:-}" ]; then
    local have_mem have_cpus
    have_mem=$(limactl list --format '{{.Memory}}' "$VM" 2>/dev/null | awk '{printf "%d", $1/1073741824}')
    have_cpus=$(limactl list --format '{{.CPUs}}' "$VM" 2>/dev/null)
    if { [ -n "${LAB_VM_MEMORY:-}" ] && [ "$LAB_VM_MEMORY" != "$have_mem" ]; } || \
       { [ -n "${LAB_VM_CPUS:-}" ] && [ "$LAB_VM_CPUS" != "$have_cpus" ]; }; then
      info "note: the Linux VM '$VM' already exists with ${have_cpus:-?} CPUs and ${have_mem:-?} GB, so"
      info "LAB_VM_CPUS and LAB_VM_MEMORY are ignored. To apply them, run ./run.sh delete-vm first."
    fi
  fi
  # The VM shares one folder. If this copy of the repo lives somewhere else, share this one instead.
  if ! limactl list --format '{{range .Config.Mounts}}{{.Location}}{{"\n"}}{{end}}' "$VM" 2>/dev/null | grep -qxF "$REPO"; then
    say "Pointing the Linux VM '$VM' at this folder"
    [ "$(limactl list --format '{{.Status}}' "$VM" 2>/dev/null)" = "Running" ] && lima_quiet limactl stop "$VM"
    lima_quiet limactl edit --tty=false --mount-only "$REPO:w" "$VM"
  fi
  if [ "$(limactl list --format '{{.Status}}' "$VM" 2>/dev/null)" != "Running" ]; then
    say "Starting the Linux VM '$VM' (a minute or two the first time)"
    lima_quiet limactl start --tty=false "$VM"
  fi
}

mac_run() {   # run this script inside the VM with the same arguments and settings
  local v pass=()
  for v in FORCE BOOT_TIMEOUT SPINES LEAVES HOSTS_PER_LEAF LAB_NAME LAB_VM LAB_SONIC_BRANCH LAB_SONIC_IMAGE \
           LAB_SONIC_REFRESH; do
    [ -z "${!v:-}" ] || pass+=("$v=${!v}")
  done
  limactl shell --workdir "$REPO" "$VM" -- env LAB_IN_VM=1 ${pass[@]+"${pass[@]}"} ./run.sh "$@"
}

if [ "$(uname -s)" = "Darwin" ] && [ -z "${LAB_IN_VM:-}" ]; then
  case "${1:-}" in
    virtual)
      mac_ensure_vm
      mac_run "$@"
      exit $? ;;
    status|down)
      if limactl list -q 2>/dev/null | grep -qx "$VM" && \
         [ "$(limactl list --format '{{.Status}}' "$VM" 2>/dev/null)" = "Running" ]; then
        mac_run "$1"
        if [ "$1" = down ]; then say "Stopping the Linux VM '$VM' (./run.sh delete-vm removes it)"; lima_quiet limactl stop "$VM"; fi
      else
        echo "  the Linux VM '$VM' is not running"
        [ "$1" != status ] || show_image_info
      fi
      [ "$1" = down ] && stop_pid dashboard   # ./run.sh preview runs on the Mac itself, not in the VM
      exit 0 ;;
    delete-vm)
      limactl delete -f "$VM" && echo "deleted the Linux VM '$VM'"
      exit 0 ;;
  esac
fi

cmd="${1:-help}"
[ $# -gt 0 ] && shift
mkdir -p "$LOGS"

case "$cmd" in
  virtual)
    S="${1:-${SPINES:-2}}"; L="${2:-${LEAVES:-2}}"; H="${3:-${HOSTS_PER_LEAF:-1}}"
    need_linux_x86
    check_wsl
    remove_old_fabric      # before the memory check: a running lab's switches still hold their memory
    check_ram $((S + L)) "$S" "$L" "$H"
    ensure_docker
    ensure_containerlab
    python_env
    generate "$S" "$L" "$H"
    ensure_sonic_image
    deploy
    wait_boot
    configure
    check_traffic
    monitoring
    start_collector
    start_dashboard
    show_urls full
    ;;
  preview)
    S="${1:-${SPINES:-2}}"; L="${2:-${LEAVES:-2}}"; H="${3:-${HOSTS_PER_LEAF:-1}}"
    find_python
    generate "$S" "$L" "$H"
    PROM_URL=""   # preview has no Prometheus; tell the Command Center not to look for one
    start_dashboard
    show_urls preview
    ;;
  status) status ;;
  down)   down ;;
  *)      sed -n '2,12p' "$0" ;;
esac
