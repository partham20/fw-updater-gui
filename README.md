# S-Board Firmware Updater

Professional CAN-FD OTA firmware update tool for TI C2000 F28P55x (GEN3 Power Distribution Unit).

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)

## Features

- **Modern CustomTkinter GUI** with dark/light/system themes
- **Automatic mode** — one-click full firmware transfer (erase, header, data, verify)
- **Manual step-by-step control** — run each OTA step independently for debugging
- **MCU Operations** — read/write boot flag, read flash, erase sectors, compute CRC, reset device
- **CAN Bus Monitor** — live sniffer for all CAN traffic
- **Raw CAN frame sender** — type any ID + hex data
- **Fully configurable** — CAN bit timing, protocol IDs/codes, timeouts, burst size, inter-frame delay
- **Abort button** — cleanly stops a transfer mid-stream
- **Settings persistence** — all settings saved/loaded from JSON

## Screenshots

```
+------------------------------------------------------------------+
|  S-Board Firmware Updater                          Theme: [Dark]  |
+------------------------------------------------------------------+
| Firmware: [D:\...\adc_ex2_soc_epwm.bin]           [Browse]       |
| Version:  [11]                        [Send Firmware]  [Abort]    |
+------------------------------------------------------------------+
| Manual Control | MCU Ops | CAN Monitor | CAN Bus | Protocol | ...|
|                                                                   |
|  [Connect PCAN]  [Disconnect]    Connected                       |
|                                                                   |
|  [1. Erase Bank 2]    Send CMD_FW_START...                       |
|  [2. Send Header]     Send CMD_FW_HEADER...                      |
|  [3. Stream Data]     Send data frames in bursts...              |
|  [4. Verify CRC]      Send CMD_FW_COMPLETE...                    |
|  [5. Single Frame]    Send one frame for debugging...            |
+------------------------------------------------------------------+
| Idle.                                              0%             |
+------------------------------------------------------------------+
```

## Requirements

- Python 3.10+
- PCAN USB adapter (PEAK-System)
- PCAN drivers installed

## Installation

```bash
git clone https://github.com/partham20/fw-updater-gui.git
cd fw-updater-gui
pip install -r requirements.txt
```

## Usage

```bash
python fw_sender_gui.py
```

Or use the command-line sender directly:

```bash
python fw_sender.py path/to/firmware.bin [version]
```

## Binary Image Generation

The `.bin` file must be a flat memory image with address gaps filled (not concatenated sections). After building in CCS:

```bash
hex2000 -o CPU1_FLASH/adc_ex2_soc_epwm.bin hex_image.hexcmd CPU1_FLASH/adc_ex2_soc_epwm.out
```

See [hex_image.hexcmd](https://gitlab.deltaww.com/ictbg/cisbu/dinrdmcis/ups/pdu-gen-3/powercalculation/-/blob/FW_OTA/hex_image.hexcmd) in the main firmware repo.

## Protocol

| CAN ID | Direction | Purpose |
|--------|-----------|---------|
| 6 | PC -> MCU | Data frames (64 bytes raw firmware per frame) |
| 7 | PC -> MCU | Commands (FW_START, FW_HEADER, FW_COMPLETE, debug cmds) |
| 8 | MCU -> PC | Responses (ACK, NAK, CRC_PASS, CRC_FAIL, data readback) |

### OTA Flow

1. **CMD_FW_START (0x30)** — MCU erases Bank 2 (128 sectors) -> ACK
2. **CMD_FW_HEADER (0x31)** — MCU parses image size/CRC/version -> ACK
3. **Data frames (ID 6)** — 64 bytes each, ACK every 16 frames
4. **CMD_FW_COMPLETE (0x33)** — MCU verifies CRC32 -> CRC_PASS/CRC_FAIL
5. On CRC_PASS: MCU writes boot flag to Bank 3, resets. Boot manager copies Bank 2 -> Bank 0.

### Extended Debug Commands

| Command | Code | Description |
|---------|------|-------------|
| CMD_READ_FLAG | 0x34 | Read boot flag values from Bank 3 |
| CMD_CLEAR_FLAG | 0x35 | Erase boot flag sector |
| CMD_RESET_DEVICE | 0x36 | Reset the MCU |
| CMD_READ_FLASH | 0x37 | Read 8 words from any flash address |
| CMD_COMPUTE_CRC | 0x38 | CRC32 over arbitrary address range |
| CMD_ERASE_SECTOR | 0x39 | Erase one flash sector |
| CMD_WRITE_FLAG | 0x3A | Manually write boot flag |
| CMD_GET_STATE | 0x3B | Report OTA state machine status |

## CAN-FD Bit Timing

Default settings (30 MHz MCAN clock on F28P55x):

| Parameter | Nominal | Data |
|-----------|---------|------|
| Baud rate | 500 kbps | 2 Mbps |
| BRP | 12 | 3 |
| TSEG1 | 3 | 3 |
| TSEG2 | 1 | 1 |
| SJW | 1 | 1 |
| Sample point | 80% | 80% |

## Related Repositories

- **[S-Board Firmware](https://gitlab.deltaww.com/ictbg/cisbu/dinrdmcis/ups/pdu-gen-3/powercalculation)** — Main application firmware (branch: FW_OTA)
- **[Boot Manager](https://github.com/partham20/boot_manager)** — Bank 0 boot manager for brick-proof OTA

## Author

Parthasarathy M