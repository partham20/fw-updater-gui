"""
CAN-FD Firmware Sender
======================
Sends a .bin firmware image to S-Board over CAN-FD via PCAN adapter.

Protocol:
  1. CMD_FW_START  (ID 7) → S-Board erases Bank 2 → ACK (ID 8)
  2. CMD_FW_HEADER (ID 7) → S-Board parses size/CRC → ACK (ID 8)
  3. Data frames   (ID 6) → 64 bytes each, bursts of 16, ACK after each burst
  4. CMD_FW_COMPLETE (ID 7) → S-Board verifies CRC → CRC_PASS/CRC_FAIL (ID 8)
  5. On CRC_PASS: S-Board writes boot flag to Bank 3 and resets.
     Boot manager copies Bank 2 → Bank 0 on next boot.

CAN IDs:
  6 = Data     (PC → S-Board, 64 bytes raw firmware per frame)
  7 = Command  (PC → S-Board)
  8 = Response (S-Board → PC)

Usage:
  1. pip install python-can
  2. Edit BIN_PATH below to point to your .bin file
  3. python fw_sender.py
"""

import can
import struct
import time
import zlib
import sys
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════════════════════

PCAN_CHANNEL    = 'PCAN_USBBUS1'

CMD_CAN_ID      = 7             # Commands:  PC → S-Board
DATA_CAN_ID     = 6             # Data:      PC → S-Board
RESP_CAN_ID     = 8             # Responses: S-Board → PC

# PCAN FD bit timing — must produce 500kbps nominal / 2Mbps data
# to match S-Board MCANAConfig() in can_driver.c
#
# MCU:    30MHz / ((5+1) * (1+7+2)) = 500kbps,  30MHz / ((0+1) * (1+11+3)) = 2Mbps
# PCAN:   30MHz / (12 * (1+3+1))    = 500kbps,  30MHz / (3 * (1+3+1))      = 2Mbps
#
# Different prescaler/segment combos but same bit rate and ~80% sample point.
PCAN_FD_PARAMS = dict(
    f_clock_mhz = 30,
    nom_brp     = 12,
    nom_tseg1   = 3,
    nom_tseg2   = 1,
    nom_sjw     = 1,
    data_brp    = 3,
    data_tseg1  = 3,
    data_tseg2  = 1,
    data_sjw    = 1,
)

# Protocol constants — must match fw_image_rx.h on S-Board
CMD_FW_START            = 0x30
CMD_FW_HEADER           = 0x31
CMD_FW_COMPLETE         = 0x33

RESP_FW_ACK             = 0x25
RESP_FW_NAK             = 0x26
RESP_FW_CRC_PASS        = 0x27
RESP_FW_CRC_FAIL        = 0x28

BURST_SIZE              = 16        # ACK after every 16 data frames
DATA_FRAME_SIZE         = 64        # Bytes per CAN-FD data frame
ACK_TIMEOUT             = 2.0       # Seconds to wait for ACK
MAX_RETRIES             = 3         # Retries per burst
ERASE_TIMEOUT           = 15.0      # Bank 2 erase (128 sectors) can take a while
VERIFY_TIMEOUT          = 10.0      # CRC over full image
INTER_FRAME_DELAY       = 0.001     # 1ms between data frames within a burst


# ═══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ═══════════════════════════════════════════════════════════════════════════════

def crc32_firmware(data: bytes) -> int:
    """CRC32 matching zlib.crc32 — same algorithm as S-Board FW_calculateCRC32."""
    return zlib.crc32(data) & 0xFFFFFFFF


def pad_to_64(data: bytes) -> bytes:
    """Pad data to a multiple of 64 bytes (one CAN-FD frame) with 0xFF."""
    remainder = len(data) % DATA_FRAME_SIZE
    if remainder != 0:
        data += b'\xFF' * (DATA_FRAME_SIZE - remainder)
    return data


def build_cmd_frame(cmd, payload=b''):
    """Build a 64-byte command frame on CAN ID 7.

    Frame layout:
      byte 0: command code
      byte 1: target ID (0x01 = S-Board)
      byte 2-3: sequence number (little-endian, 0 for commands)
      byte 4-63: payload (up to 60 bytes)
    """
    frame_data = bytearray(64)
    frame_data[0] = cmd
    frame_data[1] = 0x01        # target = S-Board
    frame_data[2] = 0x00        # seq low
    frame_data[3] = 0x00        # seq high
    frame_data[4:4+len(payload)] = payload[:60]
    return can.Message(
        arbitration_id=CMD_CAN_ID, data=bytes(frame_data),
        is_extended_id=False, is_fd=True, bitrate_switch=True,
    )


def build_data_frame(data_64):
    """Build a 64-byte data frame on CAN ID 6 (raw firmware bytes)."""
    assert len(data_64) == 64
    return can.Message(
        arbitration_id=DATA_CAN_ID, data=data_64,
        is_extended_id=False, is_fd=True, bitrate_switch=True,
    )


def parse_response(msg):
    """Parse a response frame received on CAN ID 8 from S-Board.

    Response layout:
      byte 0: response code (ACK/NAK/CRC_PASS/CRC_FAIL)
      byte 1: source ID (0x01 = S-Board)
      byte 2-3: sequence number (little-endian)
      byte 4-63: payload
    """
    if msg is None or len(msg.data) < 4:
        return None
    return {
        'cmd':     msg.data[0],
        'src_id':  msg.data[1],
        'seq':     struct.unpack_from('<H', msg.data, 2)[0],
        'payload': bytes(msg.data[4:]),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Firmware Sender
# ═══════════════════════════════════════════════════════════════════════════════

class FirmwareSender:

    def __init__(self, channel=PCAN_CHANNEL):
        print(f"[INIT] Connecting to {channel}...")
        self.bus = can.Bus(
            interface='pcan', channel=channel, fd=True, **PCAN_FD_PARAMS,
        )
        print(f"[INIT] Connected.")

    def close(self):
        self.bus.shutdown()
        print("[INIT] Disconnected.")

    def send(self, msg):
        self.bus.send(msg)

    def wait_response(self, expected_cmd=None, timeout=ACK_TIMEOUT):
        """Wait for a response on CAN ID 8, optionally filtering by command."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = self.bus.recv(timeout=max(remaining, 0.001))
            if msg is None or msg.arbitration_id != RESP_CAN_ID:
                continue
            resp = parse_response(msg)
            if resp is None:
                continue
            if expected_cmd is not None and resp['cmd'] != expected_cmd:
                # Log unexpected responses for debugging
                print(f"    (got 0x{resp['cmd']:02X}, waiting for 0x{expected_cmd:02X})")
                continue
            return resp
        return None

    # ── Step 1: Start transfer (S-Board erases Bank 2) ────────────────

    def send_start(self):
        print("\n[START] Sending CMD_FW_START (0x30)...")
        print("  S-Board will erase all 128 sectors of Bank 2.")
        print(f"  Waiting up to {ERASE_TIMEOUT}s for ACK...")
        self.send(build_cmd_frame(CMD_FW_START))
        resp = self.wait_response(expected_cmd=RESP_FW_ACK, timeout=ERASE_TIMEOUT)
        if resp:
            print("  OK — Bank 2 erased, ready to receive")
            return True
        print(f"  FAIL — No ACK within {ERASE_TIMEOUT}s")
        print("  Check: Is S-Board powered? Is CAN bus connected? Is MCANA configured?")
        return False

    # ── Step 2: Send image header ─────────────────────────────────────

    def send_header(self, image_data, version=1):
        padded = pad_to_64(image_data)
        image_crc = crc32_firmware(padded)
        total_frames = len(padded) // DATA_FRAME_SIZE

        print(f"\n[HEADER] Firmware image:")
        print(f"  Raw size:    {len(image_data):,} bytes ({len(image_data)/1024:.1f} KB)")
        print(f"  Padded size: {len(padded):,} bytes")
        print(f"  Frames:      {total_frames}")
        print(f"  CRC32:       0x{image_crc:08X}")
        print(f"  Version:     {version}")

        # Pack header payload — must match S-Board FW_handleHeader() parser
        # in fw_image_rx.c which reads from fwCmdMsg.data[4] onwards:
        #   p[0..1]   magic     (uint16, LE)
        #   p[2..3]   imageType (uint16, LE)
        #   p[4..7]   imageSize (uint32, LE)
        #   p[8..11]  imageCRC  (uint32, LE)
        #   p[12..15] version   (uint32, LE)
        #   p[16..19] destBank  (uint32, LE)
        #   p[20..23] entryPoint(uint32, LE)
        #   p[24..27] totalFrames(uint32, LE)
        payload = struct.pack('<HHIIIIII',
            0x4601,             # magic
            0x0001,             # imageType = S-Board
            len(padded),        # imageSize (padded, in bytes)
            image_crc,          # CRC32
            version,            # version
            0x0C0000,           # destBank = Bank 2
            0x082000,           # entryPoint (app starts at sector 8)
            total_frames,       # totalFrames
        )

        self.send(build_cmd_frame(CMD_FW_HEADER, payload))
        resp = self.wait_response(expected_cmd=RESP_FW_ACK, timeout=ACK_TIMEOUT)
        if resp:
            print("  OK — Header accepted")
            return padded, total_frames
        print("  FAIL — No ACK for header")
        return None, 0

    # ── Step 3: Send data frames in bursts of 16 ─────────────────────

    def send_data(self, padded_data, total_frames):
        total_bursts = (total_frames + BURST_SIZE - 1) // BURST_SIZE
        est_time = total_frames * INTER_FRAME_DELAY + total_bursts * ACK_TIMEOUT * 0.1
        print(f"\n[DATA] Sending {total_frames} frames in {total_bursts} bursts...")
        print(f"  Inter-frame delay: {INTER_FRAME_DELAY*1000:.1f}ms")
        print(f"  Estimated time:    ~{est_time:.1f}s")

        t_start = time.monotonic()
        frame_idx = 0

        while frame_idx < total_frames:
            burst_start = frame_idx
            burst_num = burst_start // BURST_SIZE + 1
            frames_in_burst = min(BURST_SIZE, total_frames - frame_idx)

            # Send burst (with inter-frame delay for MCU ISR processing)
            for i in range(frames_in_burst):
                offset = frame_idx * DATA_FRAME_SIZE
                self.send(build_data_frame(padded_data[offset : offset + DATA_FRAME_SIZE]))
                frame_idx += 1
                if i < frames_in_burst - 1:
                    time.sleep(INTER_FRAME_DELAY)

            # Wait for ACK with sequence number = last frame index in burst
            last_seq = frame_idx - 1
            ack_ok = False

            for retry in range(MAX_RETRIES):
                resp = self.wait_response(expected_cmd=RESP_FW_ACK, timeout=ACK_TIMEOUT)
                if resp and resp['seq'] == last_seq:
                    ack_ok = True
                    break
                if resp:
                    print(f"  ACK seq mismatch: got {resp['seq']}, expected {last_seq}")
                print(f"  Retry burst {burst_num} (attempt {retry+1}/{MAX_RETRIES})")
                # Retransmit entire burst
                frame_idx = burst_start
                for i in range(frames_in_burst):
                    offset = frame_idx * DATA_FRAME_SIZE
                    self.send(build_data_frame(padded_data[offset : offset + DATA_FRAME_SIZE]))
                    frame_idx += 1
                    if i < frames_in_burst - 1:
                        time.sleep(INTER_FRAME_DELAY)

            if not ack_ok:
                print(f"\n  FAIL — Burst {burst_num} failed after {MAX_RETRIES} retries")
                return False

            pct = frame_idx * 100 // total_frames
            elapsed = time.monotonic() - t_start
            print(f"  Burst {burst_num}/{total_bursts} — {pct}% ({elapsed:.1f}s)", end='\r')

        elapsed = time.monotonic() - t_start
        throughput = len(padded_data) / elapsed / 1024 if elapsed > 0 else 0
        print(f"\n  OK — All {total_frames} frames sent in {elapsed:.1f}s ({throughput:.1f} KB/s)")
        return True

    # ── Step 4: Request CRC verification ──────────────────────────────

    def send_complete(self):
        print(f"\n[VERIFY] Requesting CRC verification...")
        print(f"  S-Board will compute CRC32 over the staging bank.")
        print(f"  Waiting up to {VERIFY_TIMEOUT}s...")

        # Drain any stale frames left in the bus queue from the data
        # phase (in particular: a duplicate burst-ACK left behind when
        # the last partial burst hit a retransmission).
        drained = 0
        while True:
            m = self.bus.recv(timeout=0.05)
            if m is None:
                break
            drained += 1
        if drained:
            print(f"  (drained {drained} stale frame(s) before verify)")

        self.send(build_cmd_frame(CMD_FW_COMPLETE))

        # Wait specifically for a CRC verify result. Anything else
        # (late burst-ACK, debug frame, etc.) is logged and ignored.
        deadline = time.monotonic() + VERIFY_TIMEOUT
        resp = None
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = self.bus.recv(timeout=max(remaining, 0.001))
            if msg is None or msg.arbitration_id != RESP_CAN_ID:
                continue
            r = parse_response(msg)
            if r is None:
                continue
            if r['cmd'] in (RESP_FW_CRC_PASS, RESP_FW_CRC_FAIL):
                resp = r
                break
            print(f"    (ignoring stale 0x{r['cmd']:02X}, waiting for CRC result)")

        if resp is None:
            print("  FAIL — No verify response from S-Board")
            return False
        if resp['cmd'] == RESP_FW_CRC_PASS:
            print("  OK — CRC PASSED")
            print("  S-Board will now write boot flag and reset.")
            print("  Boot manager will copy staging bank → Bank 0 on next boot.")
            return True
        if resp['cmd'] == RESP_FW_CRC_FAIL:
            print("  FAIL — CRC MISMATCH")
            print("  Possible causes: data corruption, byte ordering issue, wrong .bin file")
            return False
        print(f"  FAIL — Unexpected response 0x{resp['cmd']:02X}")
        return False

    # ── Full sequence ─────────────────────────────────────────────────

    def send_firmware(self, bin_path, version=1):
        """Send a complete firmware image: start → header → data → verify."""
        path = Path(bin_path)
        if not path.exists():
            print(f"\nERROR: File not found: {bin_path}")
            return False

        image_data = path.read_bytes()
        if len(image_data) == 0:
            print(f"\nERROR: File is empty: {bin_path}")
            return False

        # Bank 2 max size: 128 sectors × 2KB = 256KB = 262144 bytes
        max_size = 128 * 2048
        if len(image_data) > max_size:
            print(f"\nERROR: Image too large ({len(image_data)} bytes > {max_size} bytes)")
            return False

        print(f"\n{'='*50}")
        print(f"  CAN-FD Firmware Update")
        print(f"{'='*50}")
        print(f"  File:    {path.name}")
        print(f"  Size:    {len(image_data):,} bytes ({len(image_data)/1024:.1f} KB)")
        print(f"  Version: {version}")
        print(f"  CRC32:   0x{crc32_firmware(pad_to_64(image_data)):08X}")

        if not self.send_start():
            return False

        padded, total_frames = self.send_header(image_data, version)
        if padded is None:
            return False

        if not self.send_data(padded, total_frames):
            return False

        if not self.send_complete():
            return False

        print(f"\n{'='*50}")
        print(f"  FIRMWARE TRANSFER COMPLETE")
        print(f"  S-Board is resetting with new firmware...")
        print(f"{'='*50}\n")
        return True


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    # ── Edit these ──────────────────────────────────────────────────
    BIN_PATH = r"D:\GEN3\S board\adc_ex2_soc_epwm\CPU1_FLASH\adc_ex2_soc_epwm.bin"
    VERSION  = 11
    # ────────────────────────────────────────────────────────────────

    # Allow command-line override: python fw_sender.py <path> [version]
    if len(sys.argv) >= 2:
        BIN_PATH = sys.argv[1]
    if len(sys.argv) >= 3:
        VERSION = int(sys.argv[2])

    sender = FirmwareSender()
    try:
        ok = sender.send_firmware(BIN_PATH, version=VERSION)
        sys.exit(0 if ok else 1)
    except KeyboardInterrupt:
        print("\n\nAborted by user.")
        sys.exit(2)
    finally:
        sender.close()
