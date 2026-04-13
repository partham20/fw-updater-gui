# OTA Firmware Update System — Architecture

## System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         HOST PC                                  │
│                                                                  │
│  ┌─────────────────────┐    ┌────────────────────────────────┐  │
│  │  fw_sender_gui.py   │    │  fw_sender.py (CLI / library)  │  │
│  │                     │───>│                                │  │
│  │  CustomTkinter GUI  │    │  CAN-FD state machine          │  │
│  │  8 tabs, full ctrl  │    │  Burst TX + ACK protocol       │  │
│  └─────────────────────┘    └───────────────┬────────────────┘  │
│                                             │                    │
│                              ┌──────────────▼──────────────┐    │
│                              │  python-can + PCAN driver    │    │
│                              │  500 kbps / 2 Mbps CAN-FD   │    │
│                              └──────────────┬──────────────┘    │
└─────────────────────────────────────────────┼────────────────────┘
                                              │ CAN-FD Bus
┌─────────────────────────────────────────────┼────────────────────┐
│                      S-BOARD (F28P55x)      │                    │
│                                             │                    │
│  ┌──────────────────────────────────────────▼─────────────────┐  │
│  │                     MCANA (CAN-FD)                         │  │
│  │  ID 6: Data (64B)  ID 7: Commands  ID 8: Responses        │  │
│  └──────────┬────────────────┬────────────────┬───────────────┘  │
│             │                │                │                   │
│  ┌──────────▼─────┐  ┌──────▼───────┐  ┌─────▼──────────────┐  │
│  │ Ring Buffer    │  │ Command      │  │ Response TX        │  │
│  │ (ISR → main)  │  │ Dispatcher   │  │ (ACK/NAK/CRC)     │  │
│  └──────────┬─────┘  └──────┬───────┘  └────────────────────┘  │
│             │                │                                   │
│  ┌──────────▼────────────────▼───────────────────────────────┐  │
│  │                   fw_image_rx.c                            │  │
│  │                                                            │  │
│  │  State Machine:                                            │  │
│  │  IDLE → ERASING → WAITING_HEADER → RECEIVING → VERIFYING  │  │
│  │                                                            │  │
│  │  Extended Commands (0x34-0x3B):                            │  │
│  │  Read/Write/Clear Boot Flag, Read Flash, Erase Sector,    │  │
│  │  Compute CRC, Reset Device, Get State                     │  │
│  └──────────────────────────┬────────────────────────────────┘  │
│                             │                                    │
│  ┌──────────────────────────▼────────────────────────────────┐  │
│  │                    Flash API (.TI.ramfunc)                 │  │
│  │  Runs from RAM — flash module locked during erase/program │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │                    FLASH MEMORY MAP                         │  │
│  │                                                             │  │
│  │  Bank 0: 0x080000-0x09FFFF                                 │  │
│  │    Sectors 0-7:  Boot Manager (JTAG only, never OTA'd)     │  │
│  │    Sectors 8-127: Application (copied from Bank 2)         │  │
│  │                                                             │  │
│  │  Bank 1: 0x0A0000-0x0BFFFF  (unused)                      │  │
│  │                                                             │  │
│  │  Bank 2: 0x0C0000-0x0DFFFF  (OTA staging area)            │  │
│  │    Full 128 sectors — receives new firmware over CAN       │  │
│  │                                                             │  │
│  │  Bank 3: 0x0E0000-0x0FFFFF                                 │  │
│  │    Sector 0: Boot flag (0xA5A5/0x5A5A + size + CRC)       │  │
│  │                                                             │  │
│  │  Bank 4: 0x100000-0x17FFFF  (calibration data)            │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

## OTA Update Flow

```
    HOST PC                          S-BOARD                    BOOT MANAGER
       │                                │                            │
       │  1. CMD_FW_START (0x30)        │                            │
       │───────────────────────────────>│                            │
       │                                │  Erase Bank 2              │
       │                                │  (128 sectors)             │
       │              ACK               │                            │
       │<───────────────────────────────│                            │
       │                                │                            │
       │  2. CMD_FW_HEADER (0x31)       │                            │
       │───────────────────────────────>│                            │
       │                                │  Parse size/CRC/ver        │
       │              ACK               │                            │
       │<───────────────────────────────│                            │
       │                                │                            │
       │  3. Data frames (ID 6)         │                            │
       │  ┌─ Frame 0 (64 bytes) ──────>│──┐                         │
       │  │  Frame 1 ─────────────────>│  │ Write to                │
       │  │  ...                        │  │ Bank 2 flash            │
       │  │  Frame 15 ────────────────>│  │ (via ring buffer)       │
       │  │         ACK (seq=15)        │<─┘                         │
       │  │<────────────────────────────│                            │
       │  │                             │                            │
       │  │  Frame 16-31 ─────────────>│  (repeat burst+ACK)       │
       │  │         ACK (seq=31)        │                            │
       │  │<────────────────────────────│                            │
       │  └─ ... until all frames sent  │                            │
       │                                │                            │
       │  4. CMD_FW_COMPLETE (0x33)     │                            │
       │───────────────────────────────>│                            │
       │                                │  CRC32 over Bank 2        │
       │          CRC_PASS              │                            │
       │<───────────────────────────────│                            │
       │                                │                            │
       │                                │  Write boot flag           │
       │                                │  to Bank 3 (RAM func)     │
       │                                │                            │
       │                                │  SysCtl_resetDevice()      │
       │                                │         ╔═══════╗          │
       │                                │         ║ RESET ║          │
       │                                │         ╚═══╤═══╝          │
       │                                │             │              │
       │                                │             ▼              │
       │                                │    ┌────────────────────┐  │
       │                                │    │ Boot Manager runs  │──┤
       │                                │    │ LED + CAN hello    │  │
       │                                │    │ Read flag: 0xA5A5  │  │
       │                                │    │ Verify CRC: MATCH  │  │
       │                                │    │ Erase Bank 0 app   │  │
       │                                │    │ Copy Bank 2→Bank 0 │  │
       │                                │    │ Clear flag          │  │
       │                                │    │ Reset again         │  │
       │                                │    └────────────────────┘  │
       │                                │             │              │
       │                                │             ▼              │
       │                                │    ┌────────────────────┐  │
       │                                │    │ Boot Manager runs  │──┤
       │                                │    │ No flag → jump app │  │
       │                                │    └────────┬───────────┘  │
       │                                │             │              │
       │                                │             ▼              │
       │                                │    ┌────────────────────┐  │
       │                                │    │  NEW APPLICATION   │  │
       │                                │    │  running from      │  │
       │                                │    │  Bank 0 (0x082000) │  │
       │                                │    └────────────────────┘  │
```

## Binary Image Format

```
┌─────────────────────────────────────────────────────────────┐
│                CORRECT (.bin with --image)                    │
│                                                              │
│  Offset 0     ┌──────────────────┐  Word addr 0x082000      │
│  (2 bytes)    │ codestart[0]     │  Branch to _c_int00      │
│  Offset 2     │ codestart[1]     │                          │
│               ├──────────────────┤                          │
│  Offset 4     │ 0xFFFF (fill)    │  Gap between sections    │
│  ...          │ 0xFFFF (fill)    │  preserved as erased     │
│  Offset 14    │ 0xFFFF (fill)    │  flash value             │
│               ├──────────────────┤                          │
│  Offset 16    │ FPUmathTables[0] │  Word addr 0x082008      │
│  ...          │ ...              │  sin/cos lookup tables   │
│               ├──────────────────┤                          │
│               │ .TI.ramfunc      │  Copied to RAM at boot   │
│               ├──────────────────┤                          │
│               │ .text            │  Application code        │
│               ├──────────────────┤                          │
│               │ .const           │  Constants               │
│               ├──────────────────┤                          │
│               │ .cinit           │  C runtime init data     │
│               ├──────────────────┤                          │
│               │ 0xFFFF (fill)    │  Trailing empty space    │
│               │ ...              │                          │
└───────────────┴──────────────────┘
```

```
┌─────────────────────────────────────────────────────────────┐
│              BROKEN (.bin without --image)                    │
│                                                              │
│  Offset 0     ┌──────────────────┐                          │
│               │ codestart[0]     │  OK                       │
│  Offset 2     │ codestart[1]     │                          │
│               ├──────────────────┤                          │
│  Offset 4     │ FPUmathTables[0] │  WRONG! Should be at 16 │
│  ...          │ ...              │  Every section shifted!  │
│               ├──────────────────┤                          │
│               │ .TI.ramfunc      │  Wrong address!          │
│               ├──────────────────┤                          │
│               │ .text            │  Wrong address!          │
│               │ ...              │  Firmware is SCRAMBLED   │
└───────────────┴──────────────────┘
```

## Boot Flag Layout (Bank 3, 0x0E0000)

```
Word 0:  0xA5A5  ── updatePending
Word 1:  0x5A5A  ── crcValid
Word 2:  size[15:0]  ── image size (low 16 bits)
Word 3:  size[31:16] ── image size (high 16 bits)
Word 4:  crc[15:0]   ── CRC32 (low 16 bits)
Word 5:  crc[31:16]  ── CRC32 (high 16 bits)
Word 6:  0xFFFF      ── padding
Word 7:  0xFFFF      ── padding
```

## CAN-FD Frame Formats

### Command Frame (ID 7, 64 bytes)
```
Byte 0:   Command code (0x30-0x3B)
Byte 1:   Target ID (0x01 = S-Board)
Byte 2-3: Sequence number (LE, 0 for commands)
Byte 4-63: Payload (command-specific)
```

### Response Frame (ID 8, 64 bytes)
```
Byte 0:   Response code (0x25-0x2C)
Byte 1:   Source ID (0x01 = S-Board)
Byte 2-3: Sequence number (LE)
Byte 4-63: Payload (response-specific)
```

### Data Frame (ID 6, 64 bytes)
```
Byte 0-63: Raw firmware bytes (no header)
```

## Critical Implementation Details

### Why Flash Functions Must Run from RAM

The F28P55x uses a single Flash State Machine (FSM) that controls ALL flash banks.
During an erase or program operation, the **entire flash module** is inaccessible.

```
CPU fetches instruction → Flash pipeline → Flash module
                                              │
                                    ┌─────────▼──────────┐
                                    │ FSM BUSY (erasing)  │
                                    │ ALL banks locked    │
                                    │ CPU STALLS if code  │
                                    │ executes from flash │
                                    └─────────────────────┘
```

**Solution:** All flash-touching functions use `#pragma CODE_SECTION(..., ".TI.ramfunc")`.
The linker loads them into flash, but the C runtime copies them to RAM at boot.
The CPU executes from RAM while flash is busy.

### Why EALLOW is Required

C2000 uses a register protection mechanism. Flash controller registers (`CMDWEPROTA`,
`CMDWEPROTB`) are EALLOW-protected. Without `EALLOW` active, writes to these registers
are **silently ignored** — the flash operation never starts, but no error is raised.

## Repository Map

| Repository | Platform | Content |
|------------|----------|---------|
| [S-Board-Firmware](https://github.com/partham20/S-Board-Firmware) | GitHub | Application firmware (FW_OTA branch) |
| [boot_manager](https://github.com/partham20/boot_manager) | GitHub | Boot manager (Bank 0 sectors 0-7) |
| [fw-updater-gui](https://github.com/partham20/fw-updater-gui) | GitHub | Python GUI + CLI sender |
| [powercalculation](https://gitlab.deltaww.com/ictbg/cisbu/dinrdmcis/ups/pdu-gen-3/powercalculation) | GitLab | Primary S-Board firmware repo |