# How-To Manual — S-Board OTA Firmware Update

A complete step-by-step guide from zero to a working OTA update.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Hardware Setup](#2-hardware-setup)
3. [Software Setup](#3-software-setup)
4. [First-Time JTAG Flash](#4-first-time-jtag-flash)
5. [Building the Firmware](#5-building-the-firmware)
6. [Generating the OTA Binary](#6-generating-the-ota-binary)
7. [Running the GUI](#7-running-the-gui)
8. [Automatic OTA Update](#8-automatic-ota-update)
9. [Manual Step-by-Step OTA](#9-manual-step-by-step-ota)
10. [MCU Debug Operations](#10-mcu-debug-operations)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Prerequisites

### Hardware
- **S-Board** — TMS320F28P550SG9 (128-pin) or LaunchPad F28P55x
- **PCAN USB adapter** — PEAK-System PCAN-USB (or PCAN-USB FD)
- **XDS110 JTAG debugger** — for initial flash (built into LaunchPad)
- **CAN bus** — 120-ohm terminated at both ends
- **12V power supply** for S-Board (or USB for LaunchPad)

### Software
- **Code Composer Studio 12.8.0** — [download](https://www.ti.com/tool/CCSTUDIO)
- **C2000Ware 5.04.00.00** (or 5.05+) — installed via CCS Resource Explorer
- **TI C2000 CGT 22.6.1.LTS** — installed with CCS
- **Python 3.10+** — [download](https://www.python.org/downloads/)
- **PCAN drivers** — [download](https://www.peak-system.com/Drivers.523.0.html)

---

## 2. Hardware Setup

```
 ┌──────────┐       CAN-H ─────────── CAN-H    ┌──────────────┐
 │  PCAN    │       CAN-L ─────────── CAN-L    │   S-Board    │
 │  USB     │◄─────►                    ◄──────►│  (F28P55x)   │
 │  Adapter │       GND ──────────── GND       │              │
 └────┬─────┘       120Ω termination            └──────┬───────┘
      │             at each end                        │
    USB to PC                                     XDS110 JTAG
                                                  (first flash only)
```

### CAN Pin Mapping

| Board | MCANA TX | MCANA RX | Notes |
|-------|----------|----------|-------|
| **LaunchPad** | GPIO 4 | GPIO 5 | Default for development |
| **S-Board** | GPIO 1 | GPIO 0 | Production board |

### PCAN Adapter Settings
- **No** need to configure in PCAN-View — the Python script handles everything
- Just plug in the USB cable and install drivers

---

## 3. Software Setup

### Clone the repositories

```bash
git clone https://github.com/partham20/fw-updater-gui.git
git clone https://github.com/partham20/boot_manager.git
git clone https://github.com/partham20/S-Board-Firmware.git
```

### Install Python dependencies

```bash
cd fw-updater-gui
pip install -r requirements.txt
```

This installs:
- `python-can` — CAN bus communication
- `customtkinter` — modern GUI framework

### Verify PCAN driver

```bash
python -c "import can; bus=can.Bus(interface='pcan',channel='PCAN_USBBUS1',fd=True,f_clock_mhz=30,nom_brp=12,nom_tseg1=3,nom_tseg2=1,nom_sjw=1,data_brp=3,data_tseg1=3,data_tseg2=1,data_sjw=1); print('OK'); bus.shutdown()"
```

If this prints `OK`, your PCAN adapter is working.

---

## 4. First-Time JTAG Flash

You must flash **both** the boot manager and the application via JTAG **once**.
After that, the application can be updated over CAN (OTA).

### Step 4a: Flash the Boot Manager

1. Open CCS → **Import Project** → browse to `boot_manager/`
2. Select build configuration: **CPU1_FLASH**
3. Build (Ctrl+B)
4. Connect XDS110 JTAG
5. **Run → Debug** → the boot manager is flashed to Bank 0 sectors 0-7
6. **Run → Terminate** (don't need to run it)

### Step 4b: Flash the Application

1. Open CCS → **Import Project** → browse to `S-Board-Firmware/` (or `adc_ex2_soc_epwm/`)
2. Select build configuration: **CPU1_FLASH**
3. Build (Ctrl+B)
4. **Run → Debug** → the application is flashed to Bank 0 sectors 8-127
5. **Run → Resume** (let it run to verify it works)
6. You should see LED heartbeat on GPIO 21

---

## 5. Building the Firmware

After making code changes:

1. Open the S-Board project in CCS
2. Build: **Ctrl+B** (or Project → Build)
3. Wait for `Finished building target: "adc_ex2_soc_epwm.out"`
4. CCS also generates `.bin` — but this is the **WRONG** format (see below)

---

## 6. Generating the OTA Binary

**CRITICAL:** The CCS built-in hex utility generates a broken `.bin` that concatenates
sections without preserving address gaps. You **must** regenerate it:

```bash
"C:/ti/ccs1281/ccs/tools/compiler/ti-cgt-c2000_22.6.1.LTS/bin/hex2000" ^
  -o CPU1_FLASH/adc_ex2_soc_epwm.bin ^
  hex_image.hexcmd ^
  CPU1_FLASH/adc_ex2_soc_epwm.out
```

### Verify the binary

The correct binary should be ~40 KB (tight) or 240 KB (full bank). You can verify:

```bash
python -c "
d = open('CPU1_FLASH/adc_ex2_soc_epwm.bin','rb').read()
w0 = d[0]|(d[1]<<8); w2 = d[4]|(d[5]<<8)
print(f'Size: {len(d)} bytes')
print(f'Word 0: 0x{w0:04X} (should be codestart, NOT 0xFFFF)')
print(f'Word 2: 0x{w2:04X} (should be 0xFFFF fill gap)')
if w2 == 0xFFFF: print('GOOD - address gaps preserved')
else: print('BAD - sections concatenated, run hex2000 with hex_image.hexcmd!')
"
```

### If the firmware grows

If you add code and the firmware grows past the current end address:

1. Open `CPU1_FLASH/adc_ex2_soc_epwm.map`
2. Find the last section (usually `.cinit`) — note its `origin + length`
3. Calculate: `new_end = origin + length`
4. Update `hex_image.hexcmd`: `l = (new_end - 0x082000) * 2`

Or switch to full-bank mode (never needs updating): change `l = 0x9E1C` to `l = 0x3C000`.

---

## 7. Running the GUI

```bash
cd fw-updater-gui
python fw_sender_gui.py
```

The GUI opens with 8 tabs:

| Tab | Purpose |
|-----|---------|
| **Manual Control** | Step-by-step OTA + single frame send |
| **MCU Operations** | Boot flag, flash read/erase, CRC, reset, raw CAN |
| **CAN Monitor** | Live CAN bus sniffer |
| **CAN Bus** | PCAN channel, bit timing |
| **Protocol** | CAN IDs, command/response codes |
| **Timing** | Burst size, delays, timeouts, retries |
| **Header** | Image magic, type, dest bank, entry point |
| **Settings** | Save/reset, theme |

---

## 8. Automatic OTA Update

The simplest path — one button does everything:

1. **Select firmware file** — Browse to `CPU1_FLASH/adc_ex2_soc_epwm.bin`
2. **Set version** — increment from previous (e.g., 12)
3. **Click "Send Firmware"**

The progress bar shows:
```
Erasing Bank 2...  →  Sending header...  →  Streaming firmware...  →  Verifying CRC32...
```

On success:
```
✅ Done — S-Board resetting.
```

The S-Board resets twice:
1. First reset: boot manager sees flag, copies Bank 2 → Bank 0, resets again
2. Second reset: boot manager sees no flag, jumps to new application

**Total time:** ~15-30 seconds for a 40 KB firmware.

---

## 9. Manual Step-by-Step OTA

For debugging, run each step individually:

### Step 1: Connect

1. Go to **Manual Control** tab
2. Click **Connect PCAN**
3. Status should show "Connected"

### Step 2: Erase Bank 2

1. Click **1. Erase Bank 2**
2. Log shows: `[MANUAL] Step 1: Sending CMD_FW_START...`
3. Wait for: `OK — Bank 2 erased, ready to receive.`
4. This takes 5-15 seconds (128 sectors)

### Step 3: Send Header

1. Click **2. Send Header**
2. Log shows file size, frame count, CRC
3. Wait for: `OK — Header accepted.`

### Step 4: Stream Data

1. Click **3. Stream Data**
2. Progress updates in the log: `Burst 1/40 — 2% (0.3s)`
3. Wait for: `Done: 633/633 frames in 12.5s`

### Step 5: Verify CRC

1. Click **4. Verify CRC**
2. Wait for: `CRC PASSED — S-Board will write boot flag and reset.`

### Debugging with Single Frame

If data streaming fails, use **5. Send Single Frame** to send one frame at a time.
Each click sends the next 64-byte frame and shows a hex preview.

---

## 10. MCU Debug Operations

Go to the **MCU Operations** tab (requires PCAN connected):

### Read Boot Flag
- Click **Read Boot Flag**
- Shows: `updatePending: 0xA5A5 (SET)` or `0xFFFF (not set)`
- Use this to check if the flag was written after CRC_PASS

### Read Flash Memory
- Enter address (e.g., `0x0C0000` for Bank 2 start)
- Click **Read 8 Words**
- Shows 8 × 16-bit words at that address

### Compute CRC on MCU
- Enter start address and size
- Click **Compute CRC32**
- Compares MCU-computed CRC with local Python CRC

### Erase a Sector
- Enter sector address (e.g., `0x0E0000` for boot flag)
- Click **Erase Sector** (confirmation dialog)

### Reset Device
- Click **Reset Device** — MCU resets immediately

### Send Raw CAN Frame
- Enter CAN ID and hex data bytes
- Click **Send Raw Frame**
- Example: ID=`7`, Data=`34 01 00 00` (read boot flag command)

---

## 11. Troubleshooting

### "No response" on CMD_FW_START

| Check | Fix |
|-------|-----|
| CAN bus connected? | Verify CAN-H, CAN-L, GND wiring |
| Termination? | 120-ohm resistor at each end |
| S-Board powered? | Check power LED |
| PCAN channel? | Try PCAN_USBBUS1 through PCAN_USBBUS4 |
| Bit timing match? | Nominal 500k, Data 2M, 30 MHz clock |

### CRC FAIL after data transfer

| Check | Fix |
|-------|-----|
| Wrong .bin format? | Re-run hex2000 with `hex_image.hexcmd` |
| .bin file stale? | Rebuild in CCS, then re-run hex2000 |
| Inter-frame delay? | Increase from 1ms to 2-5ms in Timing tab |
| Burst size? | Reduce from 16 to 8 in Timing tab |

### Boot manager runs but old firmware appears

| Check | Fix |
|-------|-----|
| Boot manager outdated? | Re-flash `boot_manager.out` via JTAG |
| Boot flag not written? | Use MCU Ops → Read Boot Flag to check |
| CRC mismatch at boot? | Check boot manager CAN debug on ID 0x19 |

### Transfer aborted / timeout

| Check | Fix |
|-------|-----|
| ACK timeout? | Increase from 2s to 5s in Timing tab |
| Erase timeout? | Increase from 15s to 30s in Timing tab |
| MCU crashed? | Power cycle, re-flash via JTAG |

---

## Quick Reference

### hex2000 Command (run after every CCS build)

```bash
"C:/ti/ccs1281/ccs/tools/compiler/ti-cgt-c2000_22.6.1.LTS/bin/hex2000" -o CPU1_FLASH/adc_ex2_soc_epwm.bin hex_image.hexcmd CPU1_FLASH/adc_ex2_soc_epwm.out
```

### Key Addresses

| Address | What |
|---------|------|
| `0x080000` | Boot manager codestart |
| `0x082000` | Application codestart |
| `0x0C0000` | Bank 2 (OTA staging) |
| `0x0E0000` | Bank 3 (boot flag) |

### CAN IDs

| ID | Direction | Purpose |
|----|-----------|---------|
| 6 | PC → MCU | Data frames |
| 7 | PC → MCU | Commands |
| 8 | MCU → PC | Responses |
| 9 | MCU → PC | Boot manager hello |
| 0x0A | MCU → PC | Boot manager flag status |
| 0x10-0x18 | MCU → PC | App debug messages |
| 0x19-0x1D | MCU → PC | Boot manager debug |
