# Setting up the two traffic machines

**What these are for:** they push enough traffic through the two Dell Z9100 switches to make the
switches actually congest, so PFC pause frames and ECN marks fire.

**Time:** about 90 minutes for both machines, most of it waiting.
**You need:** a USB stick 8 GB or bigger (it gets wiped), a monitor, a USB keyboard, and an
ethernet cable to your normal network.

Work through this in order. Don't skip the checks — each one catches a specific problem that is
painful to diagnose later.

---

## ⚠️ Read this first — three things that catch everyone

**1. These machines have no built-in graphics.** The Xeon has no display output, so if there is no
graphics card fitted, you get **no picture at all** — not even the BIOS. The machine will look dead.
It isn't. Before anything else, check each machine has a graphics card in it. Any cheap card works;
it is only there so you can see the screen.

**2. The 100G card is set to the wrong mode from the factory.** The ConnectX-4 MCX455A-ECAT is a
dual-personality card and ships in **InfiniBand** mode. In that mode it will *never* link to our
switch, no matter what you do — no lights, no link, nothing. There is a step below that flips it to
Ethernet. Don't skip it and don't panic before it.

**3. Do not download "the latest Mellanox driver."** These cards are old enough that NVIDIA has
dropped them from the current driver package. The driver that already comes with Ubuntu supports
them properly, and that is what we are using. Installing the newest vendor driver is the wrong move.

---

## Part 1 — Download Ubuntu

**Exactly this version. Not 26.04, not the Desktop edition.**

```
https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso
```

About 3.4 GB.

*Why not the newest Ubuntu:* our network cards are from 2014 and NVIDIA never released a driver
package for Ubuntu 26.04 that supports them. 24.04 is the newest version that still has a fallback
if we need one.

*Why Server and not Desktop:* the Desktop edition installs a newer kernel that the fallback driver
cannot build against, and a graphical desktop steals CPU during the traffic test. Server still gives
you a normal text login on the monitor — you just don't get a desktop, which we never use.

**Check the download is intact** (a corrupted ISO produces bizarre install failures):

On a Mac:
```bash
cd ~/Downloads
shasum -a 256 ubuntu-24.04.4-live-server-amd64.iso
```
On Windows PowerShell:
```powershell
Get-FileHash .\ubuntu-24.04.4-live-server-amd64.iso -Algorithm SHA256
```

It must print exactly:
```
e907d92eeec9df64163a7e454cbc8d7755e8ddc7ed42f99dbc80c40f1a138433
```
If it doesn't match, download again. Don't continue.

---

## Part 2 — Put it on the USB stick

**Easiest way, Mac or Windows — balenaEtcher:**

1. Get it from <https://etcher.balena.io/> and install it.
2. Plug in the USB stick.
3. **Flash from file** → pick the Ubuntu `.iso`
4. **Select target** → pick the USB stick — *read this twice, it erases whatever you choose*
5. **Flash**, wait, done.

On Windows, **Rufus** also works. If it says *"ISOHybrid image detected"*, keep the default
**Write in ISO Image mode**.

One stick does both machines — you don't need two.

---

## Part 3 — BIOS settings (do this on both machines)

Plug in monitor, keyboard, and an ethernet cable to your normal network. Power on and **tap `Esc`
about once a second as soon as the screen lights up.** A menu appears. From it press **`F10`** for
BIOS setup.

*(`F9` is the one-time boot menu — useful in the next part. `Esc` first, always.)*

Change these:

| Setting | Where | Set to |
|---|---|---|
| **Secure Boot** | Advanced → Secure Boot Configuration | **Disable** |
| **Legacy Support** | same screen | **Disable** |
| **Fast Boot** | same screen | **Disable** |

Then **save and exit** (`F10`).

- *Secure Boot off* — if we ever need the fallback driver, Secure Boot blocks it from loading.
- *Legacy off* — we want a clean modern install.
- *Fast Boot off* — otherwise the machine boots too quickly to catch `Esc`.

**You will not find "Above 4G Decoding" on these machines. It isn't there and isn't needed.** Don't
go looking for it.

### Which slot the network card goes in

Counting from the top of the machine, **the network card must be in slot 5** — the lower of the two
full-length slots. That is HP's own recommendation for network cards, and it is the only slot with
enough bandwidth for 100G besides the graphics slot.

- **Slot 2** (upper full-length) → the graphics card
- **Slot 5** (lower full-length) → the ConnectX network card

**If these are Z640s rather than Z440s:** same idea, but some slots on a Z640 only work when a
**second CPU** is fitted. If the card doesn't show up later, move it to a different full-length slot
and try again. There's a one-line check for this in Part 5.

**Which card goes in which machine:**
- The **bigger card with one large port** (ConnectX-4, 100G) → this machine becomes the **sender**
- The **smaller card** (ConnectX-4 Lx, 25G) → this machine becomes the **receiver**

---

## Part 4 — Install Ubuntu

Plug in the USB stick. Power on, tap **`Esc`**, then **`F9`**, and choose the USB stick from the list.

Then follow the installer. It's a text menu — arrow keys, space to tick, enter to continue.

- Language / keyboard → whatever you like
- **Type of install → `Ubuntu Server` (not "minimized")**
- Network → it should pick up an address from the ethernet cable. **Write down the IP it shows.**
- Proxy → leave blank
- Storage → **Use an entire disk** → confirm
- Profile — **use exactly these on both machines so nothing gets confusing later:**

| | First machine | Second machine |
|---|---|---|
| Your name | `sonic` | `sonic` |
| Server name | `z440-tx` | `z440-rx` |
| Username | `sonic` | `sonic` |
| Password | *pick one, same on both, and note it down* | *the same one* |

- **✅ Tick "Install OpenSSH server"** — this is the important one. Without it, nobody can log in remotely.
- Snaps → **install none of them**
- Let it finish, **Reboot Now**, pull the USB stick out when it asks.

Do the whole of Part 3 and Part 4 again for the second machine.

---

## Part 5 — Six checks, then you're done

Log in at the machine (username `sonic` and the password you set) and run these. Keep the output;
it is everything needed to take it from here remotely.

**1. What's its address?**
```bash
ip -4 addr show scope global | grep inet
```

**2. Is the network card actually seen?** *(this is the Z640 dead-slot check too)*
```bash
lspci -nn | grep -i mellanox
```
- Sender should show `ConnectX-4`
- Receiver should show `ConnectX-4 Lx`
- **Nothing printed?** The card is in a slot that isn't wired up. Power off, move it to the other
  full-length slot, try again. Don't install anything until this prints something.

**3. Install the tools** (needs internet on the machine):
```bash
sudo apt update
sudo apt install -y rdma-core ibverbs-utils ibverbs-providers rdmacm-utils \
                    perftest infiniband-diags mstflint ethtool iproute2 numactl
```

**4. Flip the 100G card to Ethernet — sender machine only.**

This is the gotcha from the top of this page. Check what mode it's in:
```bash
ibv_devinfo | grep -E 'hca_id|link_layer|fw_ver'
```

If `link_layer` says **`Ethernet`** — nothing to do, skip ahead.

If it says **`InfiniBand`**, fix it:
```bash
BDF=$(lspci -nn | grep -i 'connectx-4' | head -1 | cut -d' ' -f1)
sudo mstconfig -d $BDF set LINK_TYPE_P1=2      # 2 means Ethernet
sudo reboot
```
After it comes back, run `ibv_devinfo | grep link_layer` again — it should now say `Ethernet`.

The 25G Lx card in the receiver is Ethernet-only and never needs this.

**5. Confirm SSH works.** From your own laptop, on the same network:
```bash
ssh sonic@<the IP from step 1>
```

---

**6. Switch to SSH keys.** From your own laptop, copy your public key to each machine, so the next
steps (and any tool you use) never need the password:
```bash
ssh-copy-id sonic@<the IP from step 1>
```

---

## Note these down for the next steps

Keep the password to yourself; nothing below needs it.

- The **IP address** of each machine, and which is `z440-tx` and which is `z440-rx`
- The output of `lspci -nn | grep -i mellanox` from both
- The output of `ibv_devinfo | grep -E 'hca_id|link_layer|fw_ver'` from both
- Whether the machines are **Z440 or Z640** — and if Z640, whether they have one CPU or two
  (look for one or two heatsinks under the plastic shroud)

Everything after this — drivers, network card tuning, cabling to the switches, the switch-side
configuration, and the traffic test — can be done remotely over SSH. No more screen and keyboard.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| **No picture at all**, fans spin | No graphics card fitted. See the warning at the top. |
| Can't get into BIOS | Fast Boot is on. Hold `Esc` from the moment you press power, don't tap. |
| USB stick not in the boot list | Re-flash it; make sure Legacy Support is disabled. |
| `lspci` shows no Mellanox card | Card in a slot that isn't wired (Z640 without the 2nd CPU), or not seated. Move it to the other full-length slot. |
| `link_layer` says InfiniBand | Expected on the 100G card. Step 4 fixes it. |
| Installer can't reach the network | Ethernet cable in the **motherboard** port, not the network card. |
| SSH refused | "Install OpenSSH server" wasn't ticked. Fix on the machine: `sudo apt install -y openssh-server` |

---

## For Claude Code — paste this once SSH is up

> I have two Ubuntu 24.04 Server machines for a SONiC congestion bench:
> `z440-tx` at `<IP>` and `z440-rx` at `<IP>`, both username `sonic`, reachable over SSH with my key.
> `z440-tx` has a ConnectX-4 100G (MCX455A-ECAT), `z440-rx` has a ConnectX-4 Lx 25G (MCX4111A-ACUT).
> Use the **inbox mlx5 driver only** — do not install MLNX_OFED or DOCA, NVIDIA dropped ConnectX-4
> from the current stack and the in-kernel driver supports it fine.
> Please: verify both cards are in Ethernet mode, set up PFC on priority 3 with DSCP 26 using
> `dcb app`/`dcb pfc`/`dcb ets` from iproute2, turn global pause off, then set the matching QoS on
> the two Z9100 switches, and get `ib_write_bw` running from tx to rx through the 100G-to-25G
> speed mismatch until the switch reports queue drops. The proof counters are
> `np_ecn_marked_roce_packets` and `rx_prio3_pause`.
