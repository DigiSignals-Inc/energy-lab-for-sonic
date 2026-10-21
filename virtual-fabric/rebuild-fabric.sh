#!/bin/bash
# ── Energy Lab — REBUILD the fabric in place (POST /api/fabric/apply) ─────────
#   Regenerate (fabgen) → redeploy (containerlab --reconfigure destroys the old
#   lab + brings the new one up: this IS the resize) → wait for SONiC → configure.
#   Writes to the SAME build dir the exporter + copilot watch, so both hot-reload
#   the new fabric.json the moment this finishes.
#
#   The copilot's apply orchestrator runs this and maps these machine-parseable
#   markers (each on its own line) to a progress state:
#       @@STEP generating | @@STEP deploying | @@STEP configuring | @@STEP done
#       @@ERROR <message>            (on any failure; exit 1)
#
#   Usage:  bash rebuild-fabric.sh <spines> <leaves> <hosts_per_leaf> [exec_mode]
#           exec_mode defaults to limactl (laptop Lima VM); docker on Linux or WSL2.
#   Idempotent + safe to re-run.
set -u

# DS = this script's own directory (virtual-fabric/). Deriving it from
# BASH_SOURCE — instead of hardcoding a path — makes the script correct wherever
# the tree lives.
DS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="${BUILD_DIR:-$(cd "$DS/.." && pwd)/build}"   # the repo-level build/ that run.sh, the collector and the app all use
# fabgen writes the whole new build HERE first; only the watched artifact
# (fabric.json) is promoted into $BUILD atomically at the very END (see step 4).
# That is what keeps the exporter + copilot on the OLD fabric during teardown+boot
# and swaps them to the NEW one only once it is deployed AND configured — otherwise
# their mtime watchers would hot-swap the moment fabgen wrote fabric.json, minutes
# before the containers exist. STAGE is a dotdir inside $BUILD so the mv that
# promotes fabric.json is an atomic same-filesystem rename.
STAGE="$BUILD/.staging"
PYENV="$DS/../.venv/bin/python"          # repo-local venv if there is one, else python3
[ -x "$PYENV" ] || PYENV="$(command -v python3)"
VM="${LIMA_VM:-sonic}"

S="${1:-}"; L="${2:-}"; H="${3:-}"; MODE="${4:-limactl}"
# optional modeled-hardware ids (5th/6th positional) — forwarded to fabgen. Empty
# means "let fabgen use its defaults", so old 4-arg callers are unaffected.
LEAFM="${5:-}"; SPINEM="${6:-}"

fail() { echo "@@ERROR $*"; exit 1; }

[ -n "$S" ] && [ -n "$L" ] && [ -n "$H" ] \
  || fail "usage: rebuild-fabric.sh <spines> <leaves> <hosts_per_leaf> [exec_mode]"
[ -n "$PYENV" ] || fail "no python interpreter found (need fabgen.py's runtime)"
case "$MODE" in docker | limactl) ;; *) fail "exec_mode must be docker|limactl (got '$MODE')" ;; esac
mkdir -p "$BUILD" || fail "cannot create build dir $BUILD"

# NAME = the CURRENTLY-LIVE fabric's name (from the fabric.json we're about to
# replace). Reusing it is what makes `containerlab --reconfigure` REPLACE the
# running lab instead of spinning up a second one alongside it. Laptop deploys
# name it 'lab'. Default to 'lab' on first build.
NAME="$("$PYENV" -c "import json;print(json.load(open('$BUILD/fabric.json')).get('name','lab'))" 2>/dev/null)"
[ -n "$NAME" ] || NAME=lab
CLAB="$NAME.clab.yaml"

# ── 1. GENERATE — fabgen emits fabric.json + <name>.clab.yaml + configure-fabric.sh
#      into STAGE. The deploy/configure steps need <name>.clab.yaml + configure-fabric.sh
#      (+ prometheus.yml) in $BUILD, so copy THOSE across now; fabric.json is held
#      back in STAGE and promoted only at the end so watchers don't jump early.
echo "@@STEP generating"
rm -rf "$STAGE" || fail "cannot clear staging dir $STAGE"
mkdir -p "$STAGE" || fail "cannot create staging dir $STAGE"
HWARGS=()
[ -n "$LEAFM" ] && HWARGS+=(--leaf-model "$LEAFM")
[ -n "$SPINEM" ] && HWARGS+=(--spine-model "$SPINEM")
# Keep the switch image the live fabric runs (run.sh fetched and tagged it), so a resize
# never needs a new download. An official image keeps its branch, so fabric.json still
# says which SONiC branch it came from; the user's own image is passed as --sonic-image.
read -r LIVE_IMG LIVE_BRANCH <<< "$("$PYENV" -c "import json;s=json.load(open('$BUILD/fabric.json')).get('switch_image') or {};print(s.get('image') or '-', s.get('official_branch') or '-')" 2>/dev/null)"
FABENV=()
if [ "${LIVE_BRANCH:--}" != - ]; then
  FABENV=(LAB_SONIC_BRANCH="$LIVE_BRANCH" LAB_SONIC_IMAGE=)
elif [ "${LIVE_IMG:--}" != - ]; then
  HWARGS+=(--sonic-image "$LIVE_IMG")
fi
env ${FABENV[@]+"${FABENV[@]}"} "$PYENV" "$DS/fabgen.py" --spines "$S" --leaves "$L" --hosts-per-leaf "$H" \
  --name "$NAME" --exec-mode "$MODE" ${HWARGS[@]+"${HWARGS[@]}"} --out "$STAGE" \
  || fail "fabgen failed for ${S}x${L} (hosts/leaf ${H}) — check ranges"
[ -f "$STAGE/fabric.json" ] || fail "fabgen did not emit fabric.json"
IMG="$("$PYENV" -c "import json;print(json.load(open('$STAGE/fabric.json'))['switch_image']['image'])" 2>/dev/null)" \
  || fail "generated fabric.json names no switch image"

# stage the NON-watched artifacts into $BUILD (deploy needs <name>.clab.yaml; configure
# needs configure-fabric.sh; prometheus.yml is harmless to refresh). fabric.json stays
# in STAGE until step 4.
cp "$STAGE/$CLAB" "$BUILD/$CLAB" || fail "cannot stage $CLAB"
cp "$STAGE/configure-fabric.sh" "$BUILD/configure-fabric.sh" || fail "cannot stage configure-fabric.sh"
[ -f "$STAGE/prometheus.yml" ] && cp "$STAGE/prometheus.yml" "$BUILD/prometheus.yml"

# switch (spine/leaf) id list from the freshly generated (staged) fabric.json
NODES="$("$PYENV" -c "import json;d=json.load(open('$STAGE/fabric.json'));print(' '.join(n['id'] for n in d['nodes'] if n['kind'] in ('spine','leaf')))")" \
  || fail "could not read node list from generated fabric.json"
NSW="$(echo "$NODES" | wc -w | tr -d ' ')"
[ "$NSW" -ge 1 ] || fail "generated fabric has no switches"

# ── 2. DEPLOY — explicit DESTROY then fresh deploy. containerlab's own
#      --reconfigure occasionally half-runs its destroy phase (removes the lab dir)
#      and then trips its own "lab already deployed" guard, wedging the resize. An
#      explicit `destroy --cleanup` reliably clears containers AND the shared clab
#      network (no stale endpoints), and deploy onto a clean slate always succeeds.
echo "@@STEP deploying"
if [ "$MODE" = "limactl" ]; then
  limactl start "$VM" >/dev/null 2>&1
  VMHOME="$(limactl shell "$VM" -- bash -c 'echo $HOME')" || fail "cannot reach Lima VM '$VM'"
  limactl shell "$VM" -- bash -c 'mkdir -p ~/energy-lab-build' || fail "cannot prep VM build dir"
  # Without the image, containerlab would try Docker Hub and fail with a confusing error.
  limactl shell "$VM" -- sudo docker image inspect "$IMG" >/dev/null 2>&1 \
    || fail "switch image $IMG is missing; run ./run.sh virtual $S $L $H to fetch it"
  limactl copy "$BUILD/$CLAB" "$VM:$VMHOME/energy-lab-build/$CLAB" \
    || fail "cannot stage topology into VM '$VM'"
  limactl shell "$VM" -- bash -c "cd ~/energy-lab-build; sudo containerlab destroy -t $CLAB --cleanup >/dev/null 2>&1 || true; sudo docker ps -aq --filter name=clab-$NAME- | xargs -r sudo docker rm -f >/dev/null 2>&1 || true; sudo docker network rm clab >/dev/null 2>&1 || true; sudo containerlab deploy -t $CLAB" \
    || fail "containerlab deploy failed in VM '$VM'"
else
  cd "$BUILD" || fail "cannot enter build dir $BUILD"
  # Without the image, containerlab would try Docker Hub and fail with a confusing error.
  docker image inspect "$IMG" >/dev/null 2>&1 \
    || fail "switch image $IMG is missing; run ./run.sh virtual $S $L $H to fetch it"
  # destroy by topo, THEN force-remove any clab-<name>-* orphans the new (possibly
  # SMALLER) topo doesn't list — a downsize leaves the old fabric's extra hosts
  # running, and they hold network endpoints that wedge the deploy. Drop the shared
  # clab network too, then deploy onto a guaranteed-clean slate.
  sudo containerlab destroy -t "$CLAB" --cleanup >/dev/null 2>&1 || true
  docker ps -aq --filter "name=clab-$NAME-" | xargs -r docker rm -f >/dev/null 2>&1 || true
  docker network rm clab >/dev/null 2>&1 || true
  sudo containerlab deploy -t "$CLAB" || fail "containerlab deploy failed"
fi

# ── 3. WAIT for SONiC (redis + show interfaces) on EVERY switch, then CONFIGURE
if [ "$MODE" = "limactl" ]; then
  count_up() { limactl shell "$VM" -- bash -c "c=0; for s in $NODES; do sudo docker exec clab-$NAME-\$s sh -c 'redis-cli ping >/dev/null 2>&1 && show interfaces status >/dev/null 2>&1' && c=\$((c+1)); done; echo \$c"; }
else
  count_up() { c=0; for s in $NODES; do docker exec "clab-$NAME-$s" sh -c 'redis-cli ping >/dev/null 2>&1 && show interfaces status >/dev/null 2>&1' && c=$((c + 1)); done; echo "$c"; }
fi

tries=0
until [ "$(count_up 2>/dev/null | tr -d '[:space:]')" = "$NSW" ]; do
  tries=$((tries + 1))
  [ "$tries" -gt 60 ] && fail "timed out after ~10min waiting for $NSW switches to boot"
  echo "    ...still booting ($tries; want $NSW up)"
  sleep 10
done
echo "    all $NSW switches up"

# configure-fabric.sh is self-contained; for limactl mode its own X() reaches into
# the VM (limactl shell ... docker exec), so we run it on the HOST for both modes —
# exactly as run.sh does.
echo "@@STEP configuring"
bash "$BUILD/configure-fabric.sh" || fail "configure-fabric.sh failed"

# ── 4. PROMOTE — now (and only now: deployed AND configured) atomically swap the
#      new fabric.json into $BUILD. This single rename flips the mtime the exporter
#      + copilot watch, so their next poll reloads the NEW fabric with live switches
#      already up — never mid-teardown. The copilot's apply worker also reload_fabric()s
#      on the @@STEP done it sees just below; both now read the promoted file.
mv -f "$STAGE/fabric.json" "$BUILD/fabric.json" || fail "could not promote new fabric.json into build"
rm -rf "$STAGE" 2>/dev/null || true

echo "@@STEP done"
exit 0
