#!/bin/bash
# =============================================================================
# PC 2 — make everything permanent.
# Run from the Mac. Needs passwordless sudo on PC2 already in place.
# Settings (see .env.example): BENCH_PC2 (ssh destination, default alias "pc2"),
# BENCH_PC2_MGMT_CIDR (static mgmt address to write), BENCH_MGMT_GATEWAY and
# BENCH_PC2_DATA_CIDR (data-path address, default 10.100.0.2/24).
# Every item here exists because it bit us on PC 1.
#
# The default data-path address is from the earlier setup, where the two PCs were
# cabled back to back (PC 1 at 10.100.0.1). In the current switch bench
# (bench-knowledge.md) PC 2 is the 100G sender and PC 1, the 25G receiver, is at
# 10.100.1.1, so set BENCH_PC2_DATA_CIDR to an address on that subnet.
# =============================================================================
set -u
H="${BENCH_PC2:-pc2}"
MGMT_CIDR="${BENCH_PC2_MGMT_CIDR:?set BENCH_PC2_MGMT_CIDR to the static mgmt address for PC 2, e.g. <pc2-ip>/24}"
MGMT_GW="${BENCH_MGMT_GATEWAY:?set BENCH_MGMT_GATEWAY to the default gateway of the mgmt LAN}"
MGMT_IP="${MGMT_CIDR%/*}"
DATA_CIDR="${BENCH_PC2_DATA_CIDR:-10.100.0.2/24}"
R(){ ssh -o BatchMode=yes -o ConnectTimeout=10 "$H" "$@"; }
hdr(){ printf '\n\033[1;36m=== %s ===\033[0m\n' "$1"; }

hdr "0. preflight"
R 'sudo -n true' || { echo "passwordless sudo NOT set up - run the sudoers command first"; exit 1; }
echo "  passwordless sudo OK"

hdr "1. permanent network config"
# Static, because a fixed address means the team always knows where it is.
# optional:true stops systemd-networkd-wait-online hanging the boot for 2 min
# (the 'job systemd-networkd-wait-online' message PC 1 showed at every boot).
# Unquoted heredoc: only ${MGMT_IP}, ${MGMT_CIDR}, ${MGMT_GW} and ${DATA_CIDR} expand below.
R 'sudo tee /etc/netplan/99-lab.yaml >/dev/null' <<YAML
# Energy Lab for SONiC - PC 2 (100G sender)
# eno1    : management, reachable from the Mac at ${MGMT_IP}
# ens5np0 : ConnectX-4 100G data path at ${DATA_CIDR}
network:
  version: 2
  renderer: networkd
  ethernets:
    eno1:
      dhcp4: no
      addresses: [${MGMT_CIDR}]
      routes:
        - to: default
          via: ${MGMT_GW}
      nameservers:
        addresses: [1.1.1.1, 8.8.8.8]
      optional: true
    ens5np0:
      dhcp4: no
      addresses: [${DATA_CIDR}]
      optional: true
YAML
R 'sudo chmod 600 /etc/netplan/99-lab.yaml && sudo netplan generate && echo "  netplan file valid"'

hdr "2. apply (detached, so a blip cannot strand us)"
R 'sudo nohup sh -c "sleep 1; netplan apply" >/dev/null 2>&1 &' || true
sleep 8
until ping -c1 -W800 "$MGMT_IP" >/dev/null 2>&1; do printf '.'; sleep 2; done
echo "  back up"
R 'ip -br -4 addr | sed "s/^/  /"; ip route | sed "s/^/  /"'

hdr "3. internet through the Mac"
R 'ping -c2 -W2 1.1.1.1 >/dev/null 2>&1 && echo "  IPv4 out: OK" || echo "  IPv4 out: FAIL"
   getent hosts archive.ubuntu.com >/dev/null 2>&1 && echo "  DNS     : OK" || echo "  DNS     : FAIL"'

hdr "4. 100G link settings, persisted (PC 1 needed BOTH of these)"
R 'sudo tee /etc/systemd/system/mlx-link.service >/dev/null' <<'UNIT'
[Unit]
Description=ConnectX-4 100G link settings (speed + RS-FEC)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# RS-FEC is mandatory for 100G against the Broadcom Tomahawk in the Z9100.
# Without it the link simply never comes up - this cost us two cables and
# two switch ports before we found it on PC 1.
ExecStart=-/usr/sbin/ethtool -s ens5np0 autoneg off speed 100000 duplex full
ExecStart=-/usr/sbin/ethtool --set-fec ens5np0 encoding rs

[Install]
WantedBy=multi-user.target
UNIT
R 'sudo systemctl daemon-reload && sudo systemctl enable mlx-link.service >/dev/null 2>&1 && echo "  mlx-link.service enabled"'

hdr "5. SSH survives reboot + team can get in"
R 'sudo systemctl enable ssh >/dev/null 2>&1
   sudo systemctl enable ssh.socket >/dev/null 2>&1
   printf "  ssh        : %s / %s\n" "$(systemctl is-enabled ssh)" "$(systemctl is-active ssh)"
   printf "  ssh.socket : %s\n" "$(systemctl is-enabled ssh.socket)"
   printf "  password login (team): %s\n" "$(sudo sshd -T 2>/dev/null | grep -i ^passwordauthentication | awk "{print \$2}")"
   printf "  authorized keys: %s\n" "$(wc -l < ~/.ssh/authorized_keys 2>/dev/null)"'

hdr "6. toolchain"
R 'sudo apt-get update -qq >/dev/null 2>&1
   sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     ethtool iproute2 numactl mstflint rdma-core ibverbs-utils ibverbs-providers \
     rdmacm-utils perftest infiniband-diags iperf3 pciutils >/dev/null 2>&1 \
     && echo "  installed" || echo "  install FAILED (no internet?)"'

hdr "7. card mode - ConnectX-4 ships in InfiniBand mode"
R 'ibv_devinfo 2>/dev/null | grep -E "hca_id|link_layer|fw_ver" | sed "s/^/  /" || echo "  (no rdma tools yet)"'

hdr "8. summary"
R 'echo "  host    : $(hostname)"
   echo "  mgmt    : $(ip -4 -br addr show eno1 | awk "{print \$3}")"
   echo "  data    : $(ip -4 -br addr show ens5np0 2>/dev/null | awk "{print \$3}")"
   echo "  gateway : $(ip route | awk "/default/{print \$3}")"
   echo "  pcie err: $(awk "/TOTAL_ERR_COR/{print \$2}" /sys/bus/pci/devices/0000:00:03.0/aer_dev_correctable)"'
echo
echo "Run  ssh $H 'sudo reboot'  then re-run this script to prove it all survives."
