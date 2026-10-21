# HARDWARE — Dell Z9100-ON bring-up and integration

> **Status:** written while bringing up two Z9100-ON switches in August 2026, and used on
> them since. Everything marked **[verify on box]** could not be confirmed from source and
> must be checked against the real unit.
>
> **Platform facts (verified):** `x86_64-dell_z9100_c2538-r0`, Broadcom **Tomahawk-1**
> (BCM56960), 32×QSFP28 100G + 2×SFP+ (SFP+ **unusable** in SONiC), Intel Atom C2538,
> 2×750W reverse-airflow PSUs, ONIE. Runs **community SONiC only**.
> **Install image: community SONiC 202505 `sonic-broadcom.bin`** (**[verify]** from 202511 TH1 moves to
> a separate `sonic-broadcom-legacy-th.bin`; this lab was built and tested on 202505).
>
> **Known platform gap:** `pmon_daemon_control.json` skips **thermalctld** and **ledd** on
> this platform (fans are driven autonomously by a Dell SMF FPGA). So `TEMPERATURE_INFO` and
> `FAN_INFO` are **absent from STATE_DB** → temps and fan RPM are **not on gNMI** out of the
> box. PSU watts **do** reach STATE_DB (`psud` → `PSU_INFO`) and therefore gNMI. Board/ASIC
> temps and fan RPM are readable via `show environment` / lm-sensors / SMF hwmon over SSH.
> Re-enabling thermalctld is a one-line flip and the platform ships a full `thermal.py` with
> 8 sensors including "ASIC On-board Rear" (a board sensor; there is no ASIC die sensor).
> Our two switches now have it enabled.

## E. Bring-up runbook

> Every command below is copy-pasteable. Anything not confirmed from source is flagged **[verify on box]**.
>
> **Two before-you-start warnings.** (1) These are 2×750W reverse-airflow units with 5 fan trays — at full RPM they are ~70 dB. Running them all the time needs a closet or an enclosure, and a circuit check. (2) The AVR54 Atom C2538 clock errata affects this exact model; grade-A refurb units with long power-on life can fail to boot. **Power both on and confirm they POST before doing anything else** — if one is dead, you want to know first.

### E.0 Unbox and inventory

```
□ 2× Z9100-ON chassis, 32×QSFP28 + 2×SFP+ (SFP+ is unusable in SONiC — ignore it)
□ 4× 750W PSU (verify airflow direction on the PSU sticker matches your aisle)
□ 4× C13 power cords
□ Rails NOT included — you need a 2-post/4-post rail kit or a 2U shelf (see §F)
□ Record both service tags / serials — they go into fabric.json instances[].serial
```

### E.1 Console

`installer.conf` for `x86_64-dell_z9100_c2538-r0` sets `CONSOLE_PORT=0x2f8`, `CONSOLE_DEV=1` → **`ttyS1`**. ONIE runs at **115200 8N1**.

What worked on our units: the front-panel **micro-USB console port** (a CP2102 USB-UART on
the switch, see E.2) with a data-capable USB-A → micro-USB cable. The RJ45 console port
with a Cisco-style rollover cable and a USB-serial adapter is the alternative; we did not
use it on this bench. Either way it is not the RJ45 mgmt (Ethernet) jack.

```bash
# micro-USB console port (CP2102) → USB-A cable → laptop
ls /dev/cu.usbserial-*            # macOS
screen /dev/cu.usbserial-XXXX 115200       # exit: ctrl-a k
# Linux: sudo screen /dev/ttyUSB0 115200
```

> **Gotcha:** the Z9100 `installer.conf` does **not** set `CONSOLE_SPEED`. SONiC's installer reads the speed from `/proc/cmdline` and falls back to **9600** if it cannot. If the console goes silent after the SONiC install completes, reconnect at **9600** on the same port, then fix it permanently:
> ```bash
> sudo sed -i 's/console=ttyS1,9600/console=ttyS1,115200/' /host/grub/grub.cfg   # [verify on box]
> ```

### E.2 ONIE — install community SONiC over Dell OS9

Units ship with Dell OS9 and the internal disk is **completely full** with 13 factory
partitions (FTOS-A/B, OA, GUEST-OS, EDA-DIAG, SPARE…). There is **0 free space**, so a
naive install fails with *"Could not create partition"*. You must reach an ONIE **rescue**
shell, free the disk, then install. The path below is the one that actually worked on
service-tag SERIAL-B (2026-08-20) — the "official" menu path does **not** work on these units.

**Console:** micro-USB port on the front panel marked with the `|O|O|` console glyph (a
CP2102 USB-UART, *not* the RJ45 mgmt jack). 115200 8N1. On macOS it enumerates as
`/dev/cu.usbserial-XXXX`; `screen /dev/cu.usbserial-0001 115200`.

**Do NOT** use the GRUB "ONIE" menu entry or `onie-boot-mode -o uninstall`:
- Dell's GRUB "ONIE" entry runs `onieboot`, which just **reboots straight back into OS9** — an
  infinite loop, not ONIE.
- `onie-boot-mode -o uninstall` on a *factory* NOS is a no-op that **also** creates a reboot
  loop. Avoid it.
- The `o` autoboot hotkey does not interrupt, and `configfile` is disabled in Dell's GRUB.

**The deterministic way in — boot the ONIE kernel directly in rescue mode.** At the GRUB
menu press **`c`** for a `grub>` command line, then:

```
grub> insmod gzio
grub> insmod part_gpt
grub> insmod ext2
grub> set root=(hd0,gpt2)
grub> linux /onie/vmlinuz-4.1.28-onie quiet console=tty0 console=ttyS1,115200n8 i2c-ismt.bus_speed=100 boot_reason=rescue
grub> initrd /onie/initrd.img-4.1.28-onie
grub> boot
```

(These args are ONIE's own `grub.cfg` recipe: `onie_initargs` + `ONIE_EXTRA_CMDLINE_LINUX`
`i2c-ismt.bus_speed=100`, with `boot_reason=rescue` to land in the rescue shell instead of
the discovery installer. `(hd0,gpt2)` is the `ONIE-BOOT` partition.)

You land at `ONIE:/ #`. Free the disk, then install over the direct-attached ethernet:

```bash
# 1. See the damage — factory disk is 100% full:
ONIE:/ # sgdisk -p /dev/sda           # 13 partitions, "Total free space is 0 sectors"

# 2. Delete every Dell partition, KEEP #1 GRUB-BOOT and #2 ONIE-BOOT (you booted from #2):
ONIE:/ # sgdisk -d 13 -d 12 -d 11 -d 10 -d 9 -d 8 -d 7 -d 6 -d 5 -d 4 -d 3 /dev/sda
ONIE:/ # sgdisk -p /dev/sda           # now ~14.8 GiB free

# 3. Network install (direct cable to the Command Center host on 192.0.2.2:8000):
ONIE:/ # ifconfig eth0 192.0.2.11 netmask 255.255.255.0 up
ONIE:/ # ping -c2 192.0.2.2
ONIE:/ # onie-nos-install http://192.0.2.2:8000/sonic-broadcom.bin
#   -> when mke2fs asks "Found a dos partition table in /dev/sda3. Proceed anyway? (y,n)"
#      answer:  y      (leftover DOS signature from the wiped Dell partitions)
# USB alternative if no cable:
#   mkdir /mnt/usb && mount /dev/sdb1 /mnt/usb && onie-nos-install /mnt/usb/sonic-broadcom.bin
```

Serve the image on the host first: `cd <folder-with-sonic-broadcom.bin> && python3 -m http.server 8000`,
and give the host the link IP: `sudo ifconfig <iface> 192.0.2.2 netmask 255.255.255.0 up`.

It downloads (~904 MB), verifies checksum, creates `/dev/sda3` `SONiC-OS`, installs GRUB,
and **auto-reboots into SONiC**. First boot runs `platform-modules-z9100` setup and lands at
`sonic login:`.

**Image:** community SONiC **202505**, `sonic-broadcom.bin` (verified booting: image
`202505.1196025-ff3c0c9f6`, Debian 12). Get it from the SONiC build pipelines
(`https://sonic-build.azurewebsites.net/ui/sonic/pipelines`, Broadcom platform, branch 202505)
or the unofficial index at `sonic.software`. **[verify]** from 202511 onward Tomahawk-1 moves
to a separate `sonic-broadcom-legacy-th.bin`; **stay on 202505** — the last release with TH1 in
the mainline Broadcom image.

Default credentials after first boot: `admin` and the SONiC factory-default password (the value `SW_PW` defaults to in `.env.example`).

### E.2b First-boot config load (REQUIRED — fresh install comes up "empty")

A fresh install boots with **no config in Redis**: `show platform summary` shows `HwSKU: None`,
and `swss`/`syncd`/`pmon` stay **inactive** (only database/teamd/sysmgr/eventd run), so
`show platform temperature|fan|psustatus` all report *"Not detected"*. The factory config
**does** exist on disk (`/etc/sonic/config_db.json`, HwSKU `Force10-Z9100-C32`, 32×100G) — it
just was never loaded because first boot had no minigraph. Load it once:

```bash
admin@sonic:~$ sudo config reload -y -f      # loads /etc/sonic/config_db.json, starts swss/syncd/pmon/bgp
# wait ~90s, then confirm pmon is up and telemetry is live:
admin@sonic:~$ docker ps --format '{{.Names}}'      # expect swss, syncd, pmon, bgp, lldp...
admin@sonic:~$ show platform temperature            # board sensors (no ASIC die sensor)
admin@sonic:~$ show platform psustatus              # real PSU wattage
admin@sonic:~$ show platform fan                     # real fan RPM
```

### E.3 Base config (both switches)

```bash
# ── HW-SPINE (Z9100-A) ────────────────────────────────────────────────────────
sudo config hostname hw-spine-1
sudo passwd admin                                  # change it — do not ship the default

# OOB management on eth0
sudo config interface ip add eth0 192.0.2.11/24
sudo sonic-db-cli CONFIG_DB HSET "MGMT_INTERFACE|eth0|192.0.2.11/24" gwaddr 192.0.2.1
sudo config save -y

# confirm the platform detected itself correctly
show platform summary
#   expect:  Platform: x86_64-dell_z9100_c2538-r0
#            HwSKU:    Force10-Z9100-C32
#            ASIC:     broadcom
show version
show interfaces status | head -20
```

Repeat on **HW-LEAF (Z9100-B)** with `hw-leaf-1` / `192.0.2.12`.

### E.4 SSH keys + host-key pinning (do this before anything talks over the WAN)

```bash
# on the Command Center host
ssh-keygen -t ed25519 -f ~/.ssh/z9100_ed25519 -C "energy-lab" -N ''
ssh-copy-id -i ~/.ssh/z9100_ed25519.pub admin@192.0.2.11
ssh-copy-id -i ~/.ssh/z9100_ed25519.pub admin@192.0.2.12
ssh-keyscan -t ed25519 192.0.2.11 192.0.2.12 > /etc/energy-lab/known_hosts
ssh -i ~/.ssh/z9100_ed25519 -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=/etc/energy-lab/known_hosts admin@192.0.2.11 'show version | head -3'
```

This replaces `sshpass -p "$SW_PW"` + `StrictHostKeyChecking=no` for the physical instances only — the emulated fabric keeps its current transport.

### E.5 The 100G spine↔leaf link and FEC

**100G DAC links stay DOWN until FEC RS is set explicitly.** This is the single most likely thing to burn an hour.

```bash
# ── both switches, on the port carrying the 100G DAC ──
sudo config interface startup Ethernet0
sudo config interface speed   Ethernet0 100000
sudo config interface fec     Ethernet0 rs
sudo config save -y

show interfaces status Ethernet0        # Oper must be 'up'
show interfaces fec status Ethernet0    # [verify on box] — some builds: show interfaces status
```

**Breakout on HW-LEAF for the 4:1 congestion funnel.** Dynamic Port Breakout is flaky on this platform — do it once, offline, and confirm it survives a reboot. Prefer the static hwsku if DPB misbehaves.

```bash
# option A — dynamic (try first)
sudo config interface breakout Ethernet4 '4x25G[10G]' -y     # [verify on box: exact mode string]
sudo config save -y && sudo reboot
# after reboot expect Ethernet4, Ethernet5, Ethernet6, Ethernet7 at 25G

# option B — static hwsku fallback, if DPB does not persist
#   sudo sonic-cfggen -H -k Force10-Z9100-C8D48 --print-data > /tmp/cfg.json
#   ... then reload. [verify on box]
```

> **Tomahawk-1 FEC trap on breakout lanes.** TH1 predates 802.3by: it does **not** support RS FEC or FEC auto-negotiation on 25G lanes that are broken out. Set **Base-R (`fc`) or `none`** on every breakout lane and match it on the NIC — a mismatch keeps the link dark with no useful log line.
> ```bash
> for p in Ethernet4 Ethernet5 Ethernet6 Ethernet7; do
>   sudo config interface speed $p 25000
>   sudo config interface fec   $p fc          # or 'none'
>   sudo config interface startup $p
> done
> sudo config save -y
> # on rocegen:  sudo ethtool --set-fec ens1f0np0 baser   # or 'off'
> ```

### E.6 eBGP between the two switches

Community SONiC renders FRR from CONFIG_DB via `bgpcfgd` — configure there, not in `vtysh`, or it will be lost on restart.

```bash
# ── HW-SPINE (ASN 65001, 10.255.0.0/31, lo 10.255.255.1) ──
sudo config interface ip add Ethernet0 10.255.0.0/31
sudo config interface ip add Loopback0 10.255.255.1/32
sudo sonic-db-cli CONFIG_DB HSET "DEVICE_METADATA|localhost" bgp_asn 65001
sudo sonic-db-cli CONFIG_DB HMSET "BGP_NEIGHBOR|10.255.0.1" \
     asn 65002 holdtime 10 keepalive 3 local_addr 10.255.0.0 \
     name HW-LEAF nhopself 0 rrclient 0
sudo config save -y
sudo systemctl restart bgp                     # [verify on box] bgpcfgd may apply live

# ── HW-LEAF (ASN 65002, 10.255.0.1/31, lo 10.255.255.2) ──
sudo config interface ip add Ethernet0 10.255.0.1/31
sudo config interface ip add Loopback0 10.255.255.2/32
sudo sonic-db-cli CONFIG_DB HSET "DEVICE_METADATA|localhost" bgp_asn 65002
sudo sonic-db-cli CONFIG_DB HMSET "BGP_NEIGHBOR|10.255.0.0" \
     asn 65001 holdtime 10 keepalive 3 local_addr 10.255.0.1 \
     name HW-SPINE nhopself 0 rrclient 0
sudo config save -y && sudo systemctl restart bgp

# verify on both
show ip bgp summary
show ip route bgp
vtysh -c 'show bgp summary json' | head -30
```

Also give HW-LEAF the RoCE host subnets so the two `rocegen` netns can route to each other:

```bash
# HW-LEAF, facing the ConnectX ports
sudo config interface ip add Ethernet4 10.60.0.1/30       # rocegen port0 = 10.60.0.2
sudo config interface ip add Ethernet5 10.60.0.5/30       # rocegen port1 = 10.60.0.6
sudo config save -y
```

### E.7 Enable gNMI with TLS

`sonic-gnmi` ships enabled but binds `--noTLS --bind_address 127.0.0.1 --port 8080` until `GNMI|certs` exists — i.e. invisible from the network.

```bash
# ── on the Command Center host: one CA, one cert per switch ──
openssl req -x509 -newkey rsa:4096 -sha256 -days 1825 -nodes \
  -keyout lab-ca.key -out lab-ca.pem \
  -subj "/CN=Energy Lab CA"

for h in hw-spine-1:192.0.2.11 hw-leaf-1:192.0.2.12; do
  n=${h%%:*}; ip=${h##*:}
  openssl req -newkey rsa:2048 -nodes -keyout $n.key -out $n.csr -subj "/CN=$n"
  openssl x509 -req -in $n.csr -CA lab-ca.pem -CAkey lab-ca.key -CAcreateserial \
    -out $n.pem -days 1825 -sha256 \
    -extfile <(printf "subjectAltName=DNS:%s,IP:%s\nextendedKeyUsage=serverAuth" "$n" "$ip")
  scp -i ~/.ssh/z9100_ed25519 $n.pem $n.key lab-ca.pem admin@$ip:/tmp/
done
```

```bash
# ── on each switch ──
sudo mkdir -p /etc/sonic/telemetry
sudo mv /tmp/hw-spine-1.pem /tmp/hw-spine-1.key /tmp/lab-ca.pem /etc/sonic/telemetry/
sudo chmod 600 /etc/sonic/telemetry/hw-spine-1.key

sudo sonic-db-cli CONFIG_DB HMSET "GNMI|certs" \
     server_crt /etc/sonic/telemetry/hw-spine-1.pem \
     server_key /etc/sonic/telemetry/hw-spine-1.key \
     ca_crt     /etc/sonic/telemetry/lab-ca.pem
sudo sonic-db-cli CONFIG_DB HMSET "GNMI|gnmi" \
     port 8080 client_auth false log_level 2
# user_auth defaults to 'cert' and will demand a GNMI_CLIENT_CERT table you have not
# created — set it explicitly for password auth:   [verify on box]
sudo sonic-db-cli CONFIG_DB HSET "GNMI|gnmi" user_auth password
sudo config save -y
sudo systemctl restart gnmi        # or: docker restart gnmi
sudo ss -lntp | grep 8080          # must now show 0.0.0.0:8080, not 127.0.0.1
```

Verify from the Command Center (both the OpenConfig path the exporter already uses, and a native DB subscribe):

```bash
gnmic -a 192.0.2.11:8080 --tls-ca lab-ca.pem -u admin -p '<pw>' \
  get --path "openconfig-interfaces:interfaces/interface[name=Ethernet0]/state/counters/out-octets"

gnmic -a 192.0.2.11:8080 --tls-ca lab-ca.pem -u admin -p '<pw>' \
  --target COUNTERS_DB subscribe --mode stream --stream-mode sample \
  --sample-interval 1s --path "/COUNTERS/Ethernet0"          # [verify on box: target/path form]

gnmic -a 192.0.2.11:8080 --tls-ca lab-ca.pem -u admin -p '<pw>' \
  --target STATE_DB get --path "/PSU_INFO/PSU 1"
```

> **Parity note.** Community SONiC ships only about **10** OpenConfig models (10 measured on 202505; the count depends on the build) and has **no `openconfig-qos`**; its `openconfig-platform` app exposes **only the system EEPROM** — no PSU/fan/thermal components. The exporter's existing OpenConfig out-octets path survives unchanged (translib maps it to `SAI_PORT_STAT_IF_OUT_OCTETS`), but **every** PFC/ECN/queue/watermark/power number must come over native DB targets, which is why a DB-first provider is the right abstraction.

### E.8 Enable the flex-counter groups

Without these, the `COUNTERS_DB` fields your gNMI subscriptions target simply do not exist — and the failure looks identical to "the hardware doesn't support it".

```bash
sudo counterpoll port      enable
sudo counterpoll queue     enable
sudo counterpoll watermark enable
sudo counterpoll pg-drop   enable
sudo counterpoll wredqueue enable      # ships disabled; turns on ECN-marked queue counters
sudo counterpoll wredport  enable      # [verify on box]
counterpoll show
sudo config watermark telemetry interval 10      # default 120 s is too slow for a demo
sudo config save -y
```

### E.9 Lossless RoCE: PFC, buffers, WRED/ECN, watchdog

The Z9100 already ships a complete lossless profile — **do not invent the buffer numbers.** `Force10-Z9100-C32/pg_profile_lookup.ini` gives 100G/40m → `xon 1248, xoff 2288, size 177632, threshold -3`; 25G/40m → `xon 1248, xoff 2288, size 53248, threshold -3`. Pools: ingress_lossless 10,875,072 (xoff 4,194,112), egress_lossy 9,243,812, egress_lossless 15,982,720. Default lossless TCs are **3 and 4**.

```bash
# 1. load the platform QoS/buffer templates
sudo config qos reload
sudo config save -y
show buffer_pool watermark

# 2. PFC on priorities 3 and 4, on the fabric + host-facing ports
for p in Ethernet0 Ethernet4 Ethernet5 Ethernet6 Ethernet7; do
  sudo config interface pfc priority $p 3 on
  sudo config interface pfc priority $p 4 on
done
sudo config save -y
show priority-group watermark shared

# 3. RETUNE WRED — this is the step that makes or breaks the demo.
#    AZURE_LOSSLESS ships green_min 1,048,576 / green_max 2,097,152. You will
#    never queue a megabyte on a 25G port from two ranks, so ECN would never fire.
#    Drop the marking region ~100x, and keep ECN max BELOW the PG xoff headroom
#    so ECN marks BEFORE PFC pauses. Tomahawk honours only the GREEN profile.
sudo config ecn -profile AZURE_LOSSLESS -gmin 15360 -gmax 153600 -gdrop 5
show ecn

# 4. PFC watchdog
sudo config pfcwd start --action drop all 400 --restoration-time 400
show pfcwd config
sudo config save -y
```

**Optional egress shaper** to force the congestion point deterministically if the 4:1 funnel alone is not enough — set a `SCHEDULER` `pir` on the HW-LEAF egress port. **[verify on box]** for exact `config` syntax; the CONFIG_DB route is `SCHEDULER|<name>` with `type: DWRR`, `pir: <bps>`, referenced from `QUEUE|<port>|<idx>`.

### E.10 The thermalctld flip (the flagship finding — do it, and PR it)

`device/dell/x86_64-dell_z9100_c2538-r0/pmon_daemon_control.json` is exactly `{"skip_ledd": true, "skip_thermalctld": true}` — so `TEMPERATURE_INFO` and `FAN_INFO` never appear in STATE_DB and are therefore **not on gNMI out of the box**. The platform API is fully implemented anyway: `chassis.py` declares 8 thermal sensors, 5 fan trays, 2 PSUs, and `thermal.py` names all 8 including `ASIC On-board Rear`.

**It is safe to flip**, and here is the proof: `chassis.py` does **not** implement `get_thermal_manager()`, and `thermalctld` catches that `NotImplementedError` and runs **monitor-only** — it will populate the tables and never issue a fan-speed command, so it cannot fight the Dell SMF FPGA's autonomous fan control.

```bash
# record the "before" state
sudo sonic-db-cli STATE_DB KEYS 'TEMPERATURE_INFO|*'     # → empty
show environment                                          # → temps ARE readable over SSH

# the one-line flip
sudo cp /usr/share/sonic/device/x86_64-dell_z9100_c2538-r0/pmon_daemon_control.json{,.bak}
sudo python3 - <<'EOF'
import json
p="/usr/share/sonic/device/x86_64-dell_z9100_c2538-r0/pmon_daemon_control.json"
d=json.load(open(p)); d["skip_thermalctld"]=False; json.dump(d,open(p,"w"),indent=4)
EOF
sudo systemctl restart pmon
sleep 30
sudo sonic-db-cli STATE_DB KEYS 'TEMPERATURE_INFO|*'      # → 8 sensors
sudo sonic-db-cli STATE_DB KEYS 'FAN_INFO|*'              # → 5 trays
show platform temperature
# watch the fans for 10 minutes — RPM must be unchanged (monitor-only)
```

This is how a node moves from **NULL** to **gNMI·live**: bind the node with the flip un-applied, see the `ABSENT` badge with its reason, apply the flip, and watch the badge change. It is also a candidate upstream PR against `sonic-buildimage`.

### E.11 Verify the counters actually move

```bash
# baseline
show pfc counters
show queue counters Ethernet4
show queue watermark unicast
show queue wredcounters                     # WredDrp/pkts, EcnMarked/pkts per UC0-9
show priority-group watermark shared
show platform psustatus
sudo sonic-db-cli STATE_DB HGETALL "PSU_INFO|PSU 1"      # power, voltage, current, temp

# now generate real load from rocegen (2 netns, 2 ports, both into the fabric)
sudo ip netns add r0 && sudo ip link set ens1f0np0 netns r0
sudo ip netns add r1 && sudo ip link set ens1f1np1 netns r1
sudo ip netns exec r0 ip addr add 10.60.0.2/30 dev ens1f0np0 && sudo ip netns exec r0 ip link set ens1f0np0 up
sudo ip netns exec r1 ip addr add 10.60.0.6/30 dev ens1f1np1 && sudo ip netns exec r1 ip link set ens1f1np1 up
sudo ip netns exec r0 ip route add 10.60.0.4/30 via 10.60.0.1
sudo ip netns exec r1 ip route add 10.60.0.0/30 via 10.60.0.5

sudo ip netns exec r1 ib_write_bw -d mlx5_1 -R --report_gbits -D 60 &
sudo ip netns exec r0 ib_write_bw -d mlx5_0 -R --report_gbits -D 60 10.60.0.6

# with GPUs, the real thing:
NCCL_P2P_DISABLE=1 NCCL_SHM_DISABLE=1 NCCL_IB_DISABLE=0 NCCL_DEBUG=INFO \
  mpirun -np 2 all_reduce_perf -b 8M -e 512M -f 2 -g 1

# re-read — these must now be non-zero
show pfc counters                  # PFC3/PFC4 Rx on the congested port
show queue wredcounters            # WredDrp and (if TH1 supports it) EcnMarked
show queue watermark unicast
```

> If `EcnMarked` stays flat, check that `counterpoll wredqueue` is enabled: the counter exists on Tomahawk-1, but its group ships disabled. The dependable evidence set is **PFC pause counters + PFC watchdog + PG watermarks + WRED drops**, and the ECN proof shifts host-side to per-QP CNP counters on the ConnectX (`ethtool -S` / `rdma statistics`) — arguably a *better* story, because it shows the sender reacting.

### E.12 Register in the Command Center

Not finished yet: connecting real switches to the Command Center is open work (see
[docs/REQUIREMENTS.md](../docs/REQUIREMENTS.md), F11). Until then, use the bench scripts in
[bench/](bench/) and the SONiC command line.

---

## F. SHOPPING LIST

Order the NICs, DACs and console cable first — cable and FEC surprises are what eat the schedule.

| # | Item | Qty | Unit | Total | Why |
|---|---|---|---|---|---|
| 1 | Mellanox **MCX4121A-ACAT** ConnectX-4 Lx, dual-port 25G SFP28 (used, eBay) | 2 | $62–76 | **$124–152** | The only way to actually congest the fabric. Dual-port → both ports in separate netns → a real ring. Second card = spare + 2-host option. |
| 2 | 100G QSFP28 **DAC**, 1 m (FS.com or equiv.) | 2 | $25–40 | **$50–80** | HW-SPINE ↔ HW-LEAF uplink + one spare. |
| 3 | 100G QSFP28 → **4×25G SFP28 breakout DAC**, 1 m | 2 | $40–70 | **$80–140** | The 4:1 congestion funnel. One spare — this is the part most likely to have a FEC/compat surprise. |
| 4 | 25G SFP28 DAC, 1 m (NIC ↔ breakout, if not using the fan-out directly) | 4 | $15–25 | **$60–100** | Optional if the breakout legs reach the NIC directly. |
| 5 | **Console cable**: data-capable USB-A → micro-USB for the front-panel console port (what worked, E.1); a USB→RJ45 rollover cable (FTDI chipset — avoid Prolific clones) only if you use the RJ45 console instead | 2 | $15–25 | **$30–50** | Non-negotiable. Buy two; one will be flaky. |
| 6 | **Rack rails** for Z9100-ON (not included) *or* a 2U vented shelf | 2 | $30–120 | **$60–240** | Rails are model-specific and expensive used; a shelf is fine for an office rack. |
| 7 | **Metered/switched PDU** (APC AP7900B, Raritan PX2, used) | 1 | $120–250 | **$120–250** | Ground-truth wall power to validate `PSU_INFO` watts. This single item turns "our modeled power is close" into "here is our model error, measured." |
| 8 | Used **RTX 3060 12GB** (or Tesla P4 8GB @ ~$167) | 2 | $180–230 | **$360–460** | Genuine NCCL all-reduce. **Optional** — `osu_allreduce`/`ib_write_bw` over UCX gives the same traffic matrix with no CUDA. |
| 9 | Mini-PC as remote-access router + loadgen host, if not reusing an existing PC | 1 | $150–250 | **$150–250** | Needs a PCIe x8 slot for the ConnectX — a NUC will not do. Optional if the GPU host serves double duty. |
| 10 | Spare 750W reverse-airflow PSU | 2 | $30–60 | **$60–120** | Optional. Grade-A refurb PSUs do fail. |
| 11 | C13 power cords | 4 | $5 | **$20** | |

**Totals**

| Configuration | Cost |
|---|---|
| **Minimum credible** (NICs, DACs, breakout, console, shelf, reuse an existing PC; `ib_write_bw`/OSU instead of NCCL) | **~$375–620** |
| **Recommended** (+ PDU, + rails) | **~$560–1,010** |
| **Full flagship** (+ 2 GPUs for real NCCL, + dedicated loadgen host) | **~$1,070–1,720** |

For scale: the full build costs **less than 18 hours** of one Azure `ND96isr_H100_v5` (~$98.32/hr, 8 GPUs minimum, InfiniBand auto-wired between VMs in a Microsoft-managed scale set with **no tenant path onto your hardware**).

**Also budget (non-cash):** office circuit capacity for 2× (2×750W) switches, and a plan for ~70 dB of fan noise if these live in occupied space.

---

---

