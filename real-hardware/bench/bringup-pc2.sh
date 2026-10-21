#!/bin/bash
# ---------------------------------------------------------------------------
# PC 2 bring-up.  Run from the Mac.  Usage:  ./bringup-pc2.sh <ip>
# Every step here exists because it bit us on machine 1.
#
# Written for the earlier setup, where the two PCs were cabled back to back
# (PC 1 at 10.100.0.1, PC 2 at 10.100.0.2). In the current switch bench
# (bench-knowledge.md) PC 2 is the 100G sender and PC 1, the 25G receiver,
# is at 10.100.1.1, so use an address on that subnet for PC 2's data port.
# ---------------------------------------------------------------------------
IP="${1:?usage: bringup-pc2.sh <ip-of-pc2>}"
U="${BENCH_PC_USER:-sonic}"   # login user on PC 2 (PC-SETUP.md creates "sonic"); see .env.example
S="ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new ${U}@${IP}"

hdr(){ printf '\n\033[1;36m=== %s ===\033[0m\n' "$1"; }

hdr "1. reachable + who am i"
$S 'echo "host: $(hostname)  user: $(whoami)"; uname -r' || { echo "SSH FAILED"; exit 1; }

hdr "2. does it have internet through the Mac?"
$S 'ip route get 1.1.1.1 2>/dev/null | head -1;
    ping -c2 -W2 1.1.1.1  >/dev/null 2>&1 && echo "  IPv4 out : OK" || echo "  IPv4 out : FAIL"
    getent hosts archive.ubuntu.com >/dev/null 2>&1 && echo "  DNS      : OK" || echo "  DNS      : FAIL"'

hdr "3. addresses (must survive reboot - machine 1 lost this)"
$S 'ip -4 -br addr; echo "--- netplan files ---"; ls -1 /etc/netplan/'

hdr "4. is a Mellanox card present, and in a slot that actually works?"
$S 'lspci -nn | grep -i mellanox || echo "  NO MELLANOX CARD SEEN"'

hdr "5. PCIe health - the dead-slot check (machine 1 had 699 errors)"
$S 'for d in $(lspci -D | grep -i mellanox | cut -d" " -f1); do
      echo "device $d";
      sudo lspci -vv -s "$d" 2>/dev/null | grep -E "LnkSta:|LnkCap:" | sed "s/^/    /";
   done
   echo "--- correctable errors per root port (want 0) ---"
   for f in /sys/bus/pci/devices/*/aer_dev_correctable; do
      [ -r "$f" ] || continue
      n=$(awk "/TOTAL_ERR_COR/{print \$2}" "$f")
      [ "${n:-0}" -gt 0 ] && echo "    $(dirname $f | xargs basename) -> $n correctable"
   done; echo "    (nothing listed above = clean)"'

hdr "6. tools"
$S 'sudo apt-get update -qq &&
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
      ethtool iproute2 numactl mstflint rdma-core ibverbs-utils \
      ibverbs-providers rdmacm-utils perftest infiniband-diags iperf3 >/dev/null &&
    echo "  installed"'

hdr "7. link mode of the card (ConnectX-4 ships in InfiniBand mode)"
$S 'command -v ibv_devinfo >/dev/null && ibv_devinfo 2>/dev/null | grep -E "hca_id|link_layer|fw_ver" || echo "  (no RDMA device yet)"'

hdr "8. summary"
$S 'echo "  hostname : $(hostname)"
    echo "  kernel   : $(uname -r)"
    echo "  nics     : $(ls /sys/class/net | tr "\n" " ")"'
echo
echo "Next: a data-path address on the Mellanox port (10.100.0.2/24 back to back; see the header), then RS-FEC + speed 100000 (machine 1 needed both)."
