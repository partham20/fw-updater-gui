# GEN3 S-Board OTA Firmware Update System
### Technical Presentation

---

## Slide 1: The Problem

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│   Updating firmware on 100+ deployed S-Boards requires:     │
│                                                              │
│     ❌  Physical access to each board                       │
│     ❌  JTAG debugger connection                            │
│     ❌  CCS installed on field laptop                       │
│     ❌  15+ minutes per board                               │
│     ❌  Risk of bricking if power lost during flash          │
│                                                              │
│   We need: remote, reliable, brick-proof updates over CAN.  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## Slide 2: The Solution — Dual-Bank OTA

```
┌────────────────────────────────────────────────────────────────────┐
│                                                                    │
│          FLASH BANK 0                 FLASH BANK 2                 │
│       (Running firmware)           (Staging area)                  │
│                                                                    │
│    ┌───────────────────┐        ┌───────────────────┐             │
│    │ Sectors 0-7       │        │                   │             │
│    │ BOOT MANAGER      │        │   New firmware    │             │
│    │ (never erased)    │        │   received over   │◄── CAN-FD  │
│    ├───────────────────┤        │   CAN-FD          │             │
│    │ Sectors 8-127     │        │                   │             │
│    │ APPLICATION       │◄─COPY──│                   │             │
│    │ (old → new)       │        │                   │             │
│    └───────────────────┘        └───────────────────┘             │
│                                                                    │
│    Key: Boot manager is NEVER overwritten.                         │
│    Even if power is lost mid-copy, the boot manager survives       │
│    and can retry on next boot.                                     │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

---

## Slide 3: System Architecture

```
┌─────────────────────┐          CAN-FD Bus           ┌─────────────────┐
│                     │       500 kbps / 2 Mbps        │                 │
│    HOST PC          │◄──────────────────────────────►│    S-BOARD      │
│                     │                                │    (F28P55x)    │
│  ┌───────────────┐  │    ID 7: Commands ──────────►  │  ┌───────────┐  │
│  │  Python GUI   │  │    ID 6: Data (64B) ────────►  │  │ fw_image  │  │
│  │  (8 tabs)     │  │    ID 8: Responses  ◄────────  │  │ _rx.c     │  │
│  └───────────────┘  │                                │  └─────┬─────┘  │
│                     │                                │        │        │
│  PCAN USB Adapter   │                                │   Flash API     │
│                     │                                │   (runs in RAM) │
└─────────────────────┘                                └─────────────────┘
```

---

## Slide 4: Update Protocol (4 Steps)

```
 Step 1: ERASE                 Step 2: HEADER
 ─────────────                 ──────────────
 PC ──CMD_FW_START──► MCU      PC ──CMD_FW_HEADER──► MCU
    (0x30)                        (0x31 + size/CRC/ver)
                      │                                │
              Erase Bank 2                     Parse metadata
              128 sectors                      Set up counters
              ~10 seconds
                      │                                │
 PC ◄─────ACK────────           PC ◄────ACK───────────


 Step 3: DATA                  Step 4: VERIFY
 ────────────                  ──────────────
 PC ──Frame 0-15───► MCU       PC ──CMD_FW_COMPLETE─► MCU
    (64B each, ID 6)                (0x33)
 PC ◄─────ACK (seq=15)──                              │
 PC ──Frame 16-31──► MCU             CRC32 over Bank 2
 PC ◄─────ACK (seq=31)──             Compare with header
    ...repeat...                           │
                                    ┌──────┴───────┐
 633 frames for 40KB         MATCH? │              │ NO MATCH
 ~15 seconds                        │              │
                              Write boot flag  Send CRC_FAIL
                              Reset device     Stay on old FW
                                    │
                              Boot manager
                              copies Bank2→0
                              on next boot
```

---

## Slide 5: Brick-Proof Design

```
┌──────────────────────────────────────────────────────────────┐
│                                                              │
│   FAILURE SCENARIO              RECOVERY                     │
│   ────────────────              ────────                     │
│                                                              │
│   Power lost during      Boot manager is in sectors          │
│   Bank 2 write           0-7, never touched. Device          │
│                          boots normally with old FW.          │
│                          Retry OTA.                           │
│                                                              │
│   Power lost during      Boot manager re-reads flag          │
│   Bank 0 copy            on next boot. If flag still         │
│                          set and CRC valid, retries           │
│                          the copy. If CRC fails,              │
│                          clears flag, boots old FW.           │
│                                                              │
│   CRC mismatch after     Boot manager clears flag,           │
│   transfer               boots old FW. No harm done.         │
│                                                              │
│   CAN bus disconnected   Sender times out and reports        │
│   mid-transfer           failure. MCU stays on old FW.       │
│                          Retry when bus is reconnected.       │
│                                                              │
│   Boot manager NEVER erases itself.                          │
│   The only way to brick: corrupt sectors 0-7 via JTAG.      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## Slide 6: GUI Overview

```
┌──────────────────────────────────────────────────────────────────┐
│  S-Board Firmware Updater                          Theme: [Dark] │
├──────────────────────────────────────────────────────────────────┤
│  Firmware (.bin): [D:\...\adc_ex2_soc_epwm.bin]      [Browse]   │
│  Version: [12]                      [Send Firmware]    [Abort]   │
├─────────┬─────────┬──────────┬────────┬─────────┬────────┬──────┤
│ Manual  │ MCU Ops │ CAN Mon  │CAN Bus │Protocol │Timing  │ ...  │
│ Control │         │          │        │         │        │      │
├─────────┴─────────┴──────────┴────────┴─────────┴────────┴──────┤
│                                                                  │
│  8 TABS:                              TRANSFER LOG:              │
│                                                                  │
│  1. Manual Control                    [INIT] Connected.          │
│     - Connect/Disconnect              [START] Erasing Bank 2... │
│     - Step-by-step OTA                  OK — Bank 2 erased.     │
│     - Single frame send               [HEADER] Size: 40,476B   │
│                                          OK — Header accepted.  │
│  2. MCU Operations                    [DATA] Streaming 633...   │
│     - Read/Write/Clear flag             Burst 10/40 — 25%      │
│     - Read flash / Erase sector         Burst 20/40 — 50%      │
│     - Compute CRC / Reset              ...                      │
│     - Raw CAN frame                   [VERIFY] CRC32 PASSED    │
│                                                                  │
│  3. CAN Monitor (live sniffer)        *** SUCCESS ***            │
│  4. CAN Bus (bit timing)                                        │
│  5. Protocol (IDs, codes)                                       │
│  6. Timing (burst, delays)                                      │
│  7. Header (magic, bank)                                        │
│  8. Settings (save/reset)                                       │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  Done — S-Board resetting.          100%  |  3.2 KB/s           │
│  ████████████████████████████████████████████████████████████░░  │
└──────────────────────────────────────────────────────────────────┘
```

---

## Slide 7: Binary Format Fix

```
                    THE BUG                           THE FIX
              (hex2000 --binary)               (hex2000 --image + ROMS)

         ┌──────────────────┐              ┌──────────────────┐
 Addr    │ codestart        │   0x082000   │ codestart        │
 0x082000│ (2 words)        │              │ (2 words)        │
         ├──────────────────┤              ├──────────────────┤
 0x082002│ FPUmathTables    │◄─ WRONG!     │ 0xFFFF fill      │◄─ Gap preserved!
         │ (should be at    │   No gap!    │ 0xFFFF fill      │
         │  0x082008)       │              │ 0xFFFF fill      │
         │                  │              ├──────────────────┤
         │                  │   0x082008   │ FPUmathTables    │◄─ Correct addr!
         ├──────────────────┤              │ (at right offset)│
         │ .TI.ramfunc      │◄─ WRONG!     ├──────────────────┤
         │ (wrong address)  │              │ .TI.ramfunc      │◄─ Correct addr!
         ├──────────────────┤              ├──────────────────┤
         │ .text            │◄─ WRONG!     │ .text            │◄─ Correct addr!
         │ (SCRAMBLED!)     │              │ (works perfectly)│
         └──────────────────┘              └──────────────────┘

         40,442 bytes                      40,476 bytes
         (34 bytes short)                  (gaps filled with 0xFFFF)
```

---

## Slide 8: Three Critical Bugs Fixed

```
┌───────────────────────────────────────────────────────────────────┐
│                                                                   │
│  BUG 1: Flash functions ran from FLASH                           │
│  ──────────────────────────────────────                          │
│  boot_manager.c: eraseSector(), programEightWords() were in       │
│  .text (flash). During erase, CPU stalls — copy never happens.   │
│                                                                   │
│  FIX: #pragma CODE_SECTION(..., ".TI.ramfunc") on all 5 funcs   │
│                                                                   │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│  BUG 2: Boot flag write failed silently                          │
│  ──────────────────────────────────────                          │
│  fw_image_rx.c: FW_triggerUpdate() ran from flash, called         │
│  FW_writeBootFlag() (RAM). Flash locked during erase, return     │
│  address in flash — unpredictable. Also no Flash API re-init.    │
│                                                                   │
│  FIX: Both functions now .TI.ramfunc. Self-contained Flash API   │
│  init. FSM status checks. Reset called from RAM.                 │
│                                                                   │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│  BUG 3: Binary image format wrong                                │
│  ────────────────────────────────                                │
│  hex2000 --binary without --image concatenates sections,         │
│  destroying address gaps. Firmware scrambled after copy.          │
│                                                                   │
│  FIX: hex_image.hexcmd with --image + ROMS directive.            │
│  Flat binary with gaps filled as 0xFFFF.                         │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

---

## Slide 9: Performance

```
┌────────────────────────────────────────────┐
│                                            │
│  Firmware size:     ~40 KB (tight image)   │
│  CAN-FD data rate:  2 Mbps                 │
│  Frame size:        64 bytes               │
│  Frames:            ~633                    │
│  Burst size:        16 frames per ACK      │
│  Inter-frame delay: 1 ms                   │
│                                            │
│  ┌──────────────────────────────────────┐  │
│  │ Bank 2 erase:        ~10 seconds    │  │
│  │ Data transfer:       ~15 seconds    │  │
│  │ CRC verification:    ~2 seconds     │  │
│  │ Boot manager copy:   ~5 seconds     │  │
│  │                                     │  │
│  │ TOTAL:               ~32 seconds    │  │
│  └──────────────────────────────────────┘  │
│                                            │
│  vs. JTAG: 15+ minutes with physical      │
│  access and CCS running.                  │
│                                            │
└────────────────────────────────────────────┘
```

---

## Slide 10: Repository Structure

```
┌──────────────────────────────────────────────────────────┐
│                                                          │
│  GitHub: partham20/                                      │
│                                                          │
│  ├── S-Board-Firmware/          ◄── Application FW       │
│  │   ├── Firmware Upgrade/                               │
│  │   │   ├── fw_image_rx.c     (OTA receiver + cmds)    │
│  │   │   ├── fw_image_rx.h     (protocol defines)       │
│  │   │   └── fw_update_flash.c (flash test utils)       │
│  │   ├── hex_image.hexcmd      (binary generation)      │
│  │   └── ... (full S-Board project)                      │
│  │                                                       │
│  ├── boot_manager/              ◄── Boot Manager         │
│  │   └── boot_manager.c        (Bank 2 to Bank 0 copy)  │
│  │                                                       │
│  └── fw-updater-gui/            ◄── Python GUI + Docs    │
│      ├── fw_sender_gui.py       (8-tab GUI)             │
│      ├── fw_sender.py           (CLI sender / library)   │
│      ├── docs/                                           │
│      │   ├── ARCHITECTURE.md                             │
│      │   ├── HOWTO.md                                    │
│      │   └── PRESENTATION.md                             │
│      └── requirements.txt                                │
│                                                          │
│  GitLab: pdu-gen-3/                                      │
│  └── powercalculation/          ◄── Primary FW repo      │
│      └── (branch: FW_OTA)                                │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## Slide 11: Future Enhancements

```
┌──────────────────────────────────────────────────────────┐
│                                                          │
│  Planned                                                 │
│  ───────                                                 │
│  [ ]  Automatic hex2000 post-build integration in CCS   │
│  [ ]  Version rollback (keep previous FW in Bank 2)     │
│  [ ]  Encrypted firmware images (AES-256)               │
│  [ ]  Multi-board batch update via M-Board relay        │
│  [ ]  BU-Board OTA (same protocol, different CAN IDs)   │
│  [ ]  Windows installer (.exe) for the GUI              │
│  [ ]  Firmware version readback command                 │
│                                                          │
│  Done                                                    │
│  ────                                                    │
│  [x]  Dual-bank brick-proof architecture                │
│  [x]  CAN-FD 64-byte frame protocol with burst ACK     │
│  [x]  CRC32 end-to-end verification                    │
│  [x]  Boot manager with CAN debug telemetry            │
│  [x]  Professional GUI with 8 configuration tabs        │
│  [x]  Manual step-by-step debug mode                   │
│  [x]  MCU remote operations (flash read/erase/CRC)     │
│  [x]  CAN bus monitor                                  │
│  [x]  Correct flat binary image generation              │
│                                                          │
└──────────────────────────────────────────────────────────┘
```
