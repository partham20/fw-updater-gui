"""
S-Board Firmware Updater - Professional GUI
============================================
Fully-configurable front-end for fw_sender.py with Manual + Auto modes.

Run:
    pip install python-can customtkinter
    python fw_sender_gui.py
"""
from __future__ import annotations

import io, json, queue, re, struct, sys, threading, time, zlib
import tkinter as tk
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import can
import customtkinter as ctk
import fw_sender

# ═══════════════ Abort infrastructure ═══════════════════════════
class _TransferAborted(Exception):
    pass

class _AbortableBus:
    def __init__(self, real_bus: can.BusABC, evt: threading.Event):
        self._bus = real_bus
        self._abort = evt
    def __getattr__(self, n: str):
        return getattr(self._bus, n)
    def send(self, msg, timeout=None):
        if self._abort.is_set(): raise _TransferAborted()
        return self._bus.send(msg, timeout=timeout)
    def recv(self, timeout=None):
        if self._abort.is_set(): raise _TransferAborted()
        if timeout is None or timeout > 0.25:
            deadline = time.monotonic() + (timeout if timeout else 1e9)
            while time.monotonic() < deadline:
                if self._abort.is_set(): raise _TransferAborted()
                m = self._bus.recv(timeout=min(deadline - time.monotonic(), 0.25))
                if m is not None: return m
            return None
        return self._bus.recv(timeout=timeout)
    def shutdown(self):
        self._bus.shutdown()

# ═══════════════ Stdout fan-out ═════════════════════════════════
class _QueueWriter(io.TextIOBase):
    def __init__(self, q): self._q, self._buf = q, ""
    def write(self, s):
        if not s: return 0
        self._buf += s
        while True:
            n, r = self._buf.find("\n"), self._buf.find("\r")
            if n < 0 and r < 0: break
            idx = min(x for x in (n,r) if x >= 0)
            self._q.put(self._buf[:idx+1]); self._buf = self._buf[idx+1:]
        return len(s)
    def flush(self):
        if self._buf: self._q.put(self._buf); self._buf = ""

# ═══════════════ Settings ═══════════════════════════════════════
SETTINGS_PATH = Path(__file__).with_name("fw_sender_gui_settings.json")

@dataclass
class FwSettings:
    bin_path: str = r"D:\GEN3\S board\adc_ex2_soc_epwm\CPU1_FLASH\adc_ex2_soc_epwm.bin"
    version: int = 11
    pcan_channel: str = "PCAN_USBBUS1"
    f_clock_mhz: int = 30
    nom_brp: int = 12; nom_tseg1: int = 3; nom_tseg2: int = 1; nom_sjw: int = 1
    data_brp: int = 3; data_tseg1: int = 3; data_tseg2: int = 1; data_sjw: int = 1
    cmd_can_id: int = 7; data_can_id: int = 6; resp_can_id: int = 8
    cmd_fw_start: int = 0x30; cmd_fw_header: int = 0x31; cmd_fw_complete: int = 0x33
    resp_fw_ack: int = 0x25; resp_fw_nak: int = 0x26
    resp_fw_crc_pass: int = 0x27; resp_fw_crc_fail: int = 0x28
    burst_size: int = 16; data_frame_size: int = 64
    ack_timeout: float = 2.0; erase_timeout: float = 15.0; verify_timeout: float = 10.0
    inter_frame_delay_ms: float = 1.0; max_retries: int = 3
    header_magic: int = 0x4601; header_image_type: int = 0x0001
    header_dest_bank: int = 0x0C0000; header_entry_point: int = 0x082000
    appearance: str = "Dark"; color_theme: str = "blue"
    # Manual mode
    manual_single_frame: bool = False  # send one frame at a time
    manual_burst_pause_ms: int = 0     # pause between bursts (0=no pause)

    # ── Target mode ────────────────────────────────────────────────
    # "S-Board"     → stages the S-Board's own .bin into Bank 2 (self-update)
    # "BU via Bank1" → stages a BU board's .bin into S-Board Bank 1, to be
    #                  streamed to a BU board later by fw_bu_master on the
    #                  S-Board side. Uses a disjoint CAN ID / command set so
    #                  both receivers can coexist on the bus.
    target_mode: str = "S-Board"

    # S-Board preset (snapshot — live fields are restored from this when
    # switching back from BU mode)
    s_cmd_can_id: int = 7;  s_data_can_id: int = 6;  s_resp_can_id: int = 8
    s_cmd_fw_start: int = 0x30
    s_cmd_fw_header: int = 0x31
    s_cmd_fw_complete: int = 0x33
    s_resp_fw_ack: int = 0x25;      s_resp_fw_nak: int = 0x26
    s_resp_fw_crc_pass: int = 0x27; s_resp_fw_crc_fail: int = 0x28
    s_header_dest_bank: int = 0x0C0000
    s_header_image_type: int = 0x0001

    # BU preset (matches fw_bu_image_rx.h on the S-Board firmware)
    bu_cmd_can_id: int = 0x19; bu_data_can_id: int = 0x18; bu_resp_can_id: int = 0x1A
    bu_cmd_fw_start: int = 0x40
    bu_cmd_fw_header: int = 0x41
    bu_cmd_fw_complete: int = 0x42
    bu_resp_fw_ack: int = 0x45;      bu_resp_fw_nak: int = 0x46
    bu_resp_fw_crc_pass: int = 0x47; bu_resp_fw_crc_fail: int = 0x48
    bu_header_dest_bank: int = 0x0A0000
    bu_header_image_type: int = 0x0002

    # ── BU OTA trigger — M-Board -> S-Board "start flashing BU #N" ──
    # Separate from the staging CAN IDs above: this is the second hop
    # of the pipeline, when the S-Board already has a verified image
    # in Bank 1 and needs to push it out to one or more BU boards.
    bu_trigger_can_id: int = 0x03            # S-Board command channel
    bu_status_reply_can_id: int = 0x04       # S-Board -> M-Board reply
    bu_cmd_start_upgrade: int = 0x0E         # CMD_START_BU_FW_UPGRADE
    bu_cmd_status_request: int = 0x0F        # CMD_BU_FW_STATUS_REQUEST
    bu_target_id: int = 11                   # last-used target BU (11..22 or 0xFF)
    bu_auto_poll_ms: int = 1000              # auto-poll period (ms)

    def save(self, p=SETTINGS_PATH):
        p.write_text(json.dumps(asdict(self), indent=2))
    @classmethod
    def load(cls, p=SETTINGS_PATH):
        if not p.exists(): return cls()
        try:
            raw = json.loads(p.read_text())
        except Exception: return cls()
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in valid})

def apply_settings(s: FwSettings):
    for attr in ('PCAN_CHANNEL','CMD_CAN_ID','DATA_CAN_ID','RESP_CAN_ID',
                 'CMD_FW_START','CMD_FW_HEADER','CMD_FW_COMPLETE',
                 'RESP_FW_ACK','RESP_FW_NAK','RESP_FW_CRC_PASS','RESP_FW_CRC_FAIL',
                 'BURST_SIZE','DATA_FRAME_SIZE','ACK_TIMEOUT','ERASE_TIMEOUT',
                 'VERIFY_TIMEOUT','MAX_RETRIES'):
        if attr == 'PCAN_CHANNEL': setattr(fw_sender, attr, s.pcan_channel)
        else: setattr(fw_sender, attr, getattr(s, attr.lower()))
    fw_sender.INTER_FRAME_DELAY = s.inter_frame_delay_ms / 1000.0
    fw_sender.PCAN_FD_PARAMS = dict(
        f_clock_mhz=s.f_clock_mhz,
        nom_brp=s.nom_brp, nom_tseg1=s.nom_tseg1, nom_tseg2=s.nom_tseg2, nom_sjw=s.nom_sjw,
        data_brp=s.data_brp, data_tseg1=s.data_tseg1, data_tseg2=s.data_tseg2, data_sjw=s.data_sjw)

# ═══════════════ Labeled entry widget ═══════════════════════════
class LE(ctk.CTkFrame):
    def __init__(self, master, label, var, *, unit="", tip="", w=140):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        r = 0
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=r, column=0, sticky="ew"); r += 1
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text=label, anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="w", padx=2)
        if unit:
            ctk.CTkLabel(top, text=unit, anchor="e", text_color=("#666","#888"),
                         font=ctk.CTkFont(size=11)).grid(row=0, column=1, sticky="e", padx=2)
        ctk.CTkEntry(self, textvariable=var, width=w, height=30).grid(row=r, column=0, sticky="ew", pady=(2,0)); r += 1
        if tip:
            ctk.CTkLabel(self, text=tip, anchor="w", text_color=("#888","#777"),
                         font=ctk.CTkFont(size=10)).grid(row=r, column=0, sticky="ew", padx=2)

# ═══════════════ Main App ══════════════════════════════════════
class App(ctk.CTk):
    BURST_RE = re.compile(r"Burst\s+(\d+)\s*/\s*(\d+).*?(\d+)%.*?\(([\d.]+)s\)")

    def __init__(self):
        super().__init__()
        self.settings = FwSettings.load()
        ctk.set_appearance_mode(self.settings.appearance)
        ctk.set_default_color_theme(self.settings.color_theme)
        self.title("S-Board Firmware Updater"); self.geometry("1200x820"); self.minsize(1000, 720)
        self.vars = self._mk_vars()
        self._q: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread|None = None
        self._abort = threading.Event()
        self._manual_bus: can.BusABC|None = None
        self._build(); self.after(60, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_quit)

    def _mk_vars(self):
        out = {}
        for f in fields(FwSettings):
            v = getattr(self.settings, f.name)
            if isinstance(v, bool): out[f.name] = tk.BooleanVar(value=v)
            elif isinstance(v, int): out[f.name] = tk.IntVar(value=v)
            elif isinstance(v, float): out[f.name] = tk.DoubleVar(value=v)
            else: out[f.name] = tk.StringVar(value=str(v))
        return out

    def _v2s(self):
        kw = {}
        for f in fields(FwSettings):
            v = self.vars[f.name].get()
            try:
                if f.type in ("int", int): kw[f.name] = int(v,0) if isinstance(v,str) else int(v)
                elif f.type in ("float", float): kw[f.name] = float(v)
                elif f.type in ("bool", bool): kw[f.name] = bool(v)
                else: kw[f.name] = v
            except: kw[f.name] = getattr(self.settings, f.name)

        # ── Force live mode-specific fields to the ACTIVE preset.
        # target_mode is the single source of truth for which set of
        # CAN IDs / command codes / dest bank get pushed down to
        # fw_sender (and the manual / MCU-op handlers). The "live"
        # cmd_can_id / cmd_fw_start / etc. fields are bound to the
        # Protocol/Header tabs and CAN drift away from the active
        # mode in several ways:
        #   - settings.json from a pre-target_mode build of the GUI
        #     leaves only the old live values populated, and they may
        #     have been BU values when last saved
        #   - a prior session was in BU mode, quit-saved live BU values,
        #     and on next launch target_mode reverts to default "S-Board"
        #   - Tk var init order races with the startup preset reload
        #
        # Whatever the cause, we re-derive live = preset[target_mode]
        # right here so the send path can never send the wrong-mode
        # commands. (User edits made directly in the Protocol/Header
        # tabs while a mode is active should also be mirrored into
        # the matching s_*/bu_* preset — see _on_target_change.)
        target_mode = kw.get("target_mode", "S-Board")
        slot_idx = 2 if target_mode == "BU via Bank1" else 1
        for entry in self._TARGET_FIELD_MAP:
            live_name   = entry[0]
            preset_name = entry[slot_idx]
            kw[live_name] = kw[preset_name]
        return FwSettings(**kw)

    # ═══════════ Layout ════════════════════════════════════════
    def _build(self):
        self.grid_columnconfigure(0, weight=1); self.grid_rowconfigure(2, weight=1)
        self._build_header()
        self._build_transfer_card()
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0,8))
        body.grid_columnconfigure(0, weight=1); body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_tabs(body)
        self._build_log(body)
        self._build_status()

    def _build_header(self):
        h = ctk.CTkFrame(self, height=56, corner_radius=0)
        h.grid(row=0, column=0, sticky="ew"); h.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(h, text="  S-Board Firmware Updater",
                     font=ctk.CTkFont(size=20, weight="bold")).grid(row=0, column=0, padx=20, pady=12, sticky="w")
        ctk.CTkLabel(h, text="CAN-FD OTA", text_color=("#666","#999"),
                     font=ctk.CTkFont(size=12)).grid(row=0, column=1, sticky="w", pady=(18,0))
        r = ctk.CTkFrame(h, fg_color="transparent"); r.grid(row=0, column=2, padx=16, pady=10, sticky="e")
        ctk.CTkLabel(r, text="Theme:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0,6))
        self.theme_menu = ctk.CTkOptionMenu(r, values=["Dark","Light","System"],
                                             command=lambda c: ctk.set_appearance_mode(c), width=100)
        self.theme_menu.set(self.settings.appearance); self.theme_menu.pack(side="left")

    def _build_transfer_card(self):
        c = ctk.CTkFrame(self, corner_radius=10)
        c.grid(row=1, column=0, sticky="ew", padx=16, pady=(8,8)); c.grid_columnconfigure(1, weight=1)

        # ── Row 0: Target mode selector ────────────────────────────
        ctk.CTkLabel(c, text="Target", font=ctk.CTkFont(size=12, weight="bold")
                     ).grid(row=0, column=0, padx=(16,8), pady=(14,4), sticky="w")
        tf = ctk.CTkFrame(c, fg_color="transparent")
        tf.grid(row=0, column=1, columnspan=2, sticky="w", pady=(14,4))
        self.target_seg = ctk.CTkSegmentedButton(tf,
            values=["S-Board (Bank 2)", "BU via Bank 1"],
            command=self._on_target_change, height=32)
        self.target_seg.pack(side="left", padx=(0,8))
        self.target_hint = tk.StringVar(value="")
        ctk.CTkLabel(tf, textvariable=self.target_hint, text_color=("#666","#888"),
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(4,0))

        # ── Row 1: bin path + browse ───────────────────────────────
        ctk.CTkLabel(c, text="Firmware (.bin)", font=ctk.CTkFont(size=12, weight="bold")
                     ).grid(row=1, column=0, padx=(16,8), pady=(6,4), sticky="w")
        ctk.CTkEntry(c, textvariable=self.vars["bin_path"], height=34
                     ).grid(row=1, column=1, sticky="ew", pady=(6,4))
        ctk.CTkButton(c, text="Browse...", width=90, height=34, command=self._browse
                      ).grid(row=1, column=2, padx=(8,8), pady=(6,4))
        self.file_info = tk.StringVar(value="")
        ctk.CTkLabel(c, textvariable=self.file_info, text_color=("#666","#888"),
                     font=ctk.CTkFont(size=11)).grid(row=2, column=1, columnspan=2, sticky="w")
        self.vars["bin_path"].trace_add("write", lambda *_: self._refresh_info())
        self._refresh_info()

        # ── Row 3: version + action buttons ────────────────────────
        ctk.CTkLabel(c, text="Version", font=ctk.CTkFont(size=12, weight="bold")
                     ).grid(row=3, column=0, padx=(16,8), pady=(8,14), sticky="w")
        ctk.CTkEntry(c, textvariable=self.vars["version"], width=100, height=34
                     ).grid(row=3, column=1, sticky="w", pady=(8,14))
        btns = ctk.CTkFrame(c, fg_color="transparent")
        btns.grid(row=3, column=2, padx=(8,16), pady=(8,14), sticky="e")
        self.abort_btn = ctk.CTkButton(btns, text="Abort", width=90, height=38,
            fg_color=("#b33","#a33"), hover_color=("#d44","#c44"),
            font=ctk.CTkFont(size=13, weight="bold"), command=self._on_abort, state="disabled")
        self.abort_btn.pack(side="right", padx=(8,0))
        self.send_btn_text = tk.StringVar(value="Send Firmware")
        self.send_btn = ctk.CTkButton(btns, textvariable=self.send_btn_text, width=210, height=38,
            font=ctk.CTkFont(size=13, weight="bold"), command=self._on_send)
        self.send_btn.pack(side="right")

        # Initialize the selector to whatever the settings say, then
        # force the live vars to match the active preset so a
        # mismatched settings.json (e.g. cmd_can_id from a previous
        # mode left in the live field) gets reconciled on launch.
        self.target_seg.set("BU via Bank 1" if self.settings.target_mode == "BU via Bank1"
                            else "S-Board (Bank 2)")
        self._load_preset_into_active(self.settings.target_mode)
        self._refresh_target_labels()

    # ═══════════ Tabs ══════════════════════════════════════════
    def _build_tabs(self, parent):
        tabs = ctk.CTkTabview(parent, corner_radius=10)
        tabs.grid(row=0, column=0, sticky="nsew", padx=(0,8))
        for n in ("Manual Control", "MCU Operations", "BU OTA", "CAN Monitor", "CAN Bus", "Protocol", "Timing", "Header", "Settings"):
            tabs.add(n)
        self._build_manual_tab(tabs.tab("Manual Control"))
        self._build_mcu_tab(tabs.tab("MCU Operations"))
        self._build_bu_ota_tab(tabs.tab("BU OTA"))
        self._build_monitor_tab(tabs.tab("CAN Monitor"))
        self._build_can_tab(tabs.tab("CAN Bus"))
        self._build_protocol_tab(tabs.tab("Protocol"))
        self._build_timing_tab(tabs.tab("Timing"))
        self._build_header_tab(tabs.tab("Header"))
        self._build_settings_tab(tabs.tab("Settings"))

    def _build_manual_tab(self, tab):
        s = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True)
        s.grid_columnconfigure(0, weight=1)

        self._sec(s, "CONNECTION").grid(row=0, column=0, sticky="ew", pady=(0,6))
        cf = ctk.CTkFrame(s, fg_color="transparent")
        cf.grid(row=1, column=0, sticky="ew", padx=4, pady=4)
        self.conn_btn = ctk.CTkButton(cf, text="Connect PCAN", width=160, height=34,
                                       command=self._manual_connect)
        self.conn_btn.pack(side="left", padx=(0,8))
        self.disconn_btn = ctk.CTkButton(cf, text="Disconnect", width=120, height=34,
                                          state="disabled", command=self._manual_disconnect,
                                          fg_color=("#666","#444"), hover_color=("#777","#555"))
        self.disconn_btn.pack(side="left")
        self.conn_status = tk.StringVar(value="Disconnected")
        ctk.CTkLabel(cf, textvariable=self.conn_status, text_color=("#a33","#f66"),
                     font=ctk.CTkFont(size=12)).pack(side="left", padx=12)

        self._sec(s, "STEP-BY-STEP OTA").grid(row=2, column=0, sticky="ew", pady=(14,6))
        info = ctk.CTkLabel(s, text=(
            "Run each step individually. The S-Board responds on CAN.\n"
            "All timeouts / IDs / codes use the values from the other tabs."),
            text_color=("#888","#aaa"), font=ctk.CTkFont(size=11), justify="left")
        info.grid(row=3, column=0, sticky="ew", padx=8, pady=(0,8))

        steps = [
            ("1. Erase Bank 2",     "Send CMD_FW_START. S-Board erases 128 sectors of Bank 2.\n"
                                     "Waits up to [erase_timeout] seconds for ACK.",
             self._manual_start),
            ("2. Send Header",      "Send CMD_FW_HEADER with image size, CRC, version.\n"
                                     "S-Board parses and ACKs.",
             self._manual_header),
            ("3. Stream Data",      "Send firmware data frames in bursts of [burst_size].\n"
                                     "ACK after each burst. Retries up to [max_retries].",
             self._manual_data),
            ("4. Verify CRC",       "Send CMD_FW_COMPLETE. S-Board computes CRC32 over Bank 2.\n"
                                     "Returns CRC_PASS or CRC_FAIL.",
             self._manual_complete),
            ("5. Send Single Frame","Send exactly ONE data frame (next in sequence).\n"
                                     "For debugging byte-level issues.",
             self._manual_one_frame),
        ]
        self._manual_btns = []
        for i, (title, desc, cmd) in enumerate(steps):
            row = 4 + i * 2
            bf = ctk.CTkFrame(s, corner_radius=8)
            bf.grid(row=row, column=0, sticky="ew", padx=4, pady=4)
            bf.grid_columnconfigure(1, weight=1)
            btn = ctk.CTkButton(bf, text=title, width=200, height=36, command=cmd,
                                font=ctk.CTkFont(size=12, weight="bold"))
            btn.grid(row=0, column=0, padx=10, pady=10, sticky="w")
            ctk.CTkLabel(bf, text=desc, justify="left", text_color=("#666","#aaa"),
                         font=ctk.CTkFont(size=11), wraplength=350).grid(
                row=0, column=1, padx=(0,10), pady=10, sticky="w")
            self._manual_btns.append(btn)

        # Manual state
        self._manual_padded = None
        self._manual_total_frames = 0
        self._manual_frame_idx = 0

        self._sec(s, "MANUAL DATA STATUS").grid(row=14, column=0, sticky="ew", pady=(14,6))
        self.manual_data_status = tk.StringVar(value="No data loaded.")
        ctk.CTkLabel(s, textvariable=self.manual_data_status,
                     font=ctk.CTkFont(size=12)).grid(row=15, column=0, sticky="ew", padx=8)

    def _build_mcu_tab(self, tab):
        s = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True); s.grid_columnconfigure(0, weight=1)

        self._sec(s, "BOOT FLAG (Bank 3, 0x0E0000)").grid(row=0, column=0, sticky="ew", pady=(0,6))
        bf = ctk.CTkFrame(s, fg_color="transparent")
        bf.grid(row=1, column=0, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(bf, text="Read Boot Flag", width=160, height=34,
                      command=self._mcu_read_flag).pack(side="left", padx=(0,8))
        ctk.CTkButton(bf, text="Clear Boot Flag", width=160, height=34,
                      command=self._mcu_clear_flag,
                      fg_color=("#a55","#a44"), hover_color=("#c66","#c55")).pack(side="left", padx=(0,8))
        ctk.CTkButton(bf, text="Write Boot Flag", width=160, height=34,
                      command=self._mcu_write_flag).pack(side="left")

        self._sec(s, "FLASH OPERATIONS").grid(row=2, column=0, sticky="ew", pady=(14,6))
        ff = ctk.CTkFrame(s, corner_radius=8)
        ff.grid(row=3, column=0, sticky="ew", padx=4, pady=4)
        ff.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(ff, text="Address (hex):", font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, padx=(10,4), pady=8, sticky="w")
        self.mcu_addr_var = tk.StringVar(value="0x0E0000")
        ctk.CTkEntry(ff, textvariable=self.mcu_addr_var, width=160, height=32).grid(
            row=0, column=1, padx=4, pady=8, sticky="w")
        fb = ctk.CTkFrame(ff, fg_color="transparent")
        fb.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=(0,8))
        ctk.CTkButton(fb, text="Read 8 Words", width=140, height=34,
                      command=self._mcu_read_flash).pack(side="left", padx=4)
        ctk.CTkButton(fb, text="Erase Sector", width=140, height=34,
                      command=self._mcu_erase_sector,
                      fg_color=("#a55","#a44"), hover_color=("#c66","#c55")).pack(side="left", padx=4)

        self._sec(s, "CRC VERIFICATION").grid(row=4, column=0, sticky="ew", pady=(14,6))
        cf = ctk.CTkFrame(s, corner_radius=8)
        cf.grid(row=5, column=0, sticky="ew", padx=4, pady=4)
        cf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(cf, text="Start addr:", font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, padx=(10,4), pady=4, sticky="w")
        self.mcu_crc_addr = tk.StringVar(value="0x0C0000")
        ctk.CTkEntry(cf, textvariable=self.mcu_crc_addr, width=140, height=32).grid(
            row=0, column=1, padx=4, pady=4, sticky="w")
        ctk.CTkLabel(cf, text="Size (bytes):", font=ctk.CTkFont(size=12)).grid(
            row=1, column=0, padx=(10,4), pady=4, sticky="w")
        self.mcu_crc_size = tk.StringVar(value="0")
        ctk.CTkEntry(cf, textvariable=self.mcu_crc_size, width=140, height=32).grid(
            row=1, column=1, padx=4, pady=4, sticky="w")
        ctk.CTkButton(cf, text="Compute CRC32", width=160, height=34,
                      command=self._mcu_compute_crc).grid(
            row=2, column=0, columnspan=2, padx=10, pady=(4,8), sticky="w")

        self._sec(s, "DEVICE CONTROL").grid(row=6, column=0, sticky="ew", pady=(14,6))
        df = ctk.CTkFrame(s, fg_color="transparent")
        df.grid(row=7, column=0, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(df, text="Get State", width=140, height=34,
                      command=self._mcu_get_state).pack(side="left", padx=(0,8))
        ctk.CTkButton(df, text="Reset Device", width=160, height=34,
                      command=self._mcu_reset,
                      fg_color=("#a55","#a44"), hover_color=("#c66","#c55")).pack(side="left", padx=(0,8))

        self._sec(s, "RAW CAN FRAME").grid(row=8, column=0, sticky="ew", pady=(14,6))
        rf = ctk.CTkFrame(s, corner_radius=8)
        rf.grid(row=9, column=0, sticky="ew", padx=4, pady=4)
        rf.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(rf, text="CAN ID:", font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, padx=(10,4), pady=4, sticky="w")
        self.raw_id_var = tk.StringVar(value="7")
        ctk.CTkEntry(rf, textvariable=self.raw_id_var, width=80, height=32).grid(
            row=0, column=1, padx=4, pady=4, sticky="w")
        ctk.CTkLabel(rf, text="Data (hex):", font=ctk.CTkFont(size=12)).grid(
            row=1, column=0, padx=(10,4), pady=4, sticky="w")
        self.raw_data_var = tk.StringVar(value="30 01 00 00")
        ctk.CTkEntry(rf, textvariable=self.raw_data_var, width=400, height=32).grid(
            row=1, column=1, padx=4, pady=4, sticky="ew")
        ctk.CTkLabel(rf, text="64-byte CAN-FD frame. Pad with 00.", text_color=("#888","#777"),
                     font=ctk.CTkFont(size=10)).grid(row=2, column=1, padx=4, sticky="w")
        ctk.CTkButton(rf, text="Send Raw Frame", width=160, height=34,
                      command=self._mcu_send_raw).grid(
            row=3, column=0, columnspan=2, padx=10, pady=(4,8), sticky="w")

    def _build_monitor_tab(self, tab):
        s = ctk.CTkFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True)
        s.grid_rowconfigure(1, weight=1); s.grid_columnconfigure(0, weight=1)
        top = ctk.CTkFrame(s, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8,4))
        self.monitor_btn = ctk.CTkButton(top, text="Start Monitor", width=140, height=34,
                                          command=self._monitor_toggle)
        self.monitor_btn.pack(side="left", padx=(0,8))
        ctk.CTkButton(top, text="Clear", width=80, height=34, command=self._monitor_clear,
                      fg_color=("#666","#444"), hover_color=("#777","#555")).pack(side="left")
        self.monitor_status = tk.StringVar(value="Stopped")
        ctk.CTkLabel(top, textvariable=self.monitor_status, font=ctk.CTkFont(size=12)).pack(side="left", padx=12)
        self.monitor_text = tk.Text(s, wrap="none", relief="flat", borderwidth=0,
                                     bg="#0e1116", fg="#d6dde6", font=("Consolas", 10))
        self.monitor_text.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0,8))
        self.monitor_text.tag_config("rx", foreground="#69b1ff")
        self.monitor_text.tag_config("tx", foreground="#7adf7a")
        self.monitor_text.tag_config("hdr", foreground="#f1c40f")
        self.monitor_text.configure(state="disabled")
        self._monitoring = False
        self._monitor_thread = None

    # ═══════════ BU OTA Trigger tab ═══════════════════════════════
    # Sends CMD_START_BU_FW_UPGRADE (0x0E) on CAN ID 3 to the S-Board
    # to kick off streaming of the staged Bank 1 image to a BU board,
    # and polls CMD_BU_FW_STATUS_REQUEST (0x0F) for progress. Status
    # replies come back on CAN ID 0x04 with a parsed layout.
    #
    # Prerequisite: an image must already be staged into the S-Board
    # Bank 1 via the "BU via Bank 1" target mode on the main card.
    _BU_MASTER_STATES = {
        0: "IDLE",
        1: "PREPARING",
        2: "SEND_HEADER",
        3: "SENDING_DATA",
        4: "VERIFYING",
        5: "ACTIVATING",
        6: "DONE",
        7: "FAILED",
    }
    _BU_MASTER_ERRORS = {
        0: "NONE",
        1: "NO_IMAGE",
        2: "NAK",
        3: "VERIFY",
        4: "TIMEOUT",
        5: "RETRIES_EXHAUSTED",
    }

    def _build_bu_ota_tab(self, tab):
        s = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True)
        s.grid_columnconfigure(0, weight=1)

        # ── Intro / instructions ──────────────────────────────────
        self._sec(s, "WHAT THIS DOES").grid(row=0, column=0, sticky="ew", pady=(0, 6))
        intro = ctk.CTkLabel(
            s,
            text=(
                "Triggers a BU-Board firmware upgrade from an image you\n"
                "already staged into S-Board Bank 1 (use the main card in\n"
                "'BU via Bank 1' mode first — the staging must be complete\n"
                "with CRC PASS before triggering).\n\n"
                "Steps:\n"
                "  1. Connect PCAN (Manual Control tab)\n"
                "  2. Pick target BU id (11..22, or 0xFF for all)\n"
                "  3. Click 'Trigger Upgrade'\n"
                "  4. Either poll once or enable auto-poll to watch progress"
            ),
            justify="left",
            text_color=("#888", "#aaa"),
            font=ctk.CTkFont(size=11),
        )
        intro.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 8))

        # ── Target selector ──────────────────────────────────────
        self._sec(s, "TARGET").grid(row=2, column=0, sticky="ew", pady=(8, 6))
        tf = ctk.CTkFrame(s, corner_radius=8)
        tf.grid(row=3, column=0, sticky="ew", padx=4, pady=4)
        tf.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(tf, text="BU board id:", font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, padx=(10, 4), pady=10, sticky="w"
        )
        ctk.CTkEntry(
            tf,
            textvariable=self.vars["bu_target_id"],
            width=90,
            height=32,
        ).grid(row=0, column=1, padx=4, pady=10, sticky="w")
        ctk.CTkLabel(
            tf,
            text="(11..22 = single BU   |   0xFF = all sequentially)",
            text_color=("#888", "#777"),
            font=ctk.CTkFont(size=10),
        ).grid(row=0, column=2, padx=(4, 10), pady=10, sticky="w")

        # Quick-pick buttons for common BU ids
        qp = ctk.CTkFrame(s, fg_color="transparent")
        qp.grid(row=4, column=0, sticky="ew", padx=4, pady=(0, 4))
        for i, bid in enumerate(
            [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 0xFF]
        ):
            label = "all" if bid == 0xFF else str(bid)
            ctk.CTkButton(
                qp,
                text=label,
                width=44,
                height=28,
                fg_color=("#444", "#333"),
                hover_color=("#555", "#444"),
                command=lambda b=bid: self.vars["bu_target_id"].set(b),
            ).grid(row=0, column=i, padx=2, pady=2)

        # ── Action buttons ───────────────────────────────────────
        self._sec(s, "ACTIONS").grid(row=5, column=0, sticky="ew", pady=(14, 6))
        ab = ctk.CTkFrame(s, fg_color="transparent")
        ab.grid(row=6, column=0, sticky="ew", padx=4, pady=4)
        ctk.CTkButton(
            ab,
            text="Trigger Upgrade",
            width=180,
            height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._bu_trigger_upgrade,
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            ab,
            text="Poll Status",
            width=140,
            height=38,
            command=self._bu_poll_status,
        ).pack(side="left", padx=(0, 8))
        self.bu_auto_btn = ctk.CTkButton(
            ab,
            text="Start Auto-poll",
            width=160,
            height=38,
            fg_color=("#357", "#357"),
            hover_color=("#468", "#468"),
            command=self._bu_auto_poll_toggle,
        )
        self.bu_auto_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            ab,
            text="Abort (send 0x55)",
            width=150,
            height=38,
            fg_color=("#a44", "#a44"),
            hover_color=("#c55", "#c55"),
            command=self._bu_abort,
        ).pack(side="left")

        # ── Status display ───────────────────────────────────────
        self._sec(s, "STATUS").grid(row=7, column=0, sticky="ew", pady=(14, 6))
        self.bu_status_state = tk.StringVar(value="(no reply yet)")
        self.bu_status_error = tk.StringVar(value="")
        self.bu_status_target = tk.StringVar(value="")
        self.bu_status_progress = tk.StringVar(value="")
        self.bu_status_bulk = tk.StringVar(value="")
        sf = ctk.CTkFrame(s, corner_radius=8)
        sf.grid(row=8, column=0, sticky="ew", padx=4, pady=4)
        sf.grid_columnconfigure(1, weight=1)
        rows = [
            ("Master state:", self.bu_status_state),
            ("Error:", self.bu_status_error),
            ("Target BU:", self.bu_status_target),
            ("Progress:", self.bu_status_progress),
            ("Bulk sweep:", self.bu_status_bulk),
        ]
        for i, (label, var) in enumerate(rows):
            ctk.CTkLabel(
                sf, text=label, anchor="e",
                font=ctk.CTkFont(size=12, weight="bold"),
            ).grid(row=i, column=0, padx=(10, 6), pady=4, sticky="e")
            ctk.CTkLabel(
                sf, textvariable=var, anchor="w",
                font=ctk.CTkFont(size=12, family="Consolas"),
            ).grid(row=i, column=1, padx=(0, 10), pady=4, sticky="ew")

        # ── Configurable CAN ids / command codes ─────────────────
        self._sec(s, "CONFIGURATION (hex OK)").grid(row=9, column=0, sticky="ew", pady=(14, 6))
        cf = ctk.CTkFrame(s, corner_radius=8)
        cf.grid(row=10, column=0, sticky="ew", padx=4, pady=4)
        cf.grid_columnconfigure((0, 1), weight=1)
        LE(cf, "Trigger CAN ID (M→S)", self.vars["bu_trigger_can_id"],
           tip="Usually 3 — M-Board command channel.").grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        LE(cf, "Status reply CAN ID (S→M)", self.vars["bu_status_reply_can_id"],
           tip="Usually 0x04 — FW_BU_STATUS_REPLY_ID in fw_upgrade_config.h.").grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        LE(cf, "Start-upgrade opcode", self.vars["bu_cmd_start_upgrade"],
           tip="CMD_START_BU_FW_UPGRADE — byte 0 of the command frame.").grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        LE(cf, "Status-request opcode", self.vars["bu_cmd_status_request"],
           tip="CMD_BU_FW_STATUS_REQUEST — byte 0 of the poll frame.").grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        LE(cf, "Auto-poll period", self.vars["bu_auto_poll_ms"], unit="ms",
           tip="How often the auto-poll timer sends a status request.").grid(row=2, column=0, padx=4, pady=4, sticky="ew")

        self._bu_auto_poll = False
        self._bu_auto_poll_job = None

    # ═══════════ BU OTA handlers ══════════════════════════════════
    def _bu_send_cmd(self, opcode: int, target: int) -> bool:
        """Send a 64-byte command frame on the configured trigger ID.
        Frame layout (matches the S-Board main() handler in
        adc_ex2_soc_epwm.c):
            byte 0 = opcode (0x0E or 0x0F)
            byte 1 = target BU id (ignored for status poll)
            byte 2 = options bitmap (bit0 = continue_on_fail)
        """
        if not self._require_bus():
            return False
        s = self._v2s()
        frame = bytearray(64)
        frame[0] = opcode & 0xFF
        frame[1] = target & 0xFF
        frame[2] = 0x01  # default: continue_on_fail
        msg = can.Message(
            arbitration_id=s.bu_trigger_can_id,
            data=bytes(frame),
            is_extended_id=False,
            is_fd=True,
            bitrate_switch=True,
        )
        try:
            self._manual_bus.send(msg)
            return True
        except Exception as e:
            self._log_line(f"[ERROR] BU OTA send failed: {e}\n", "err")
            return False

    def _bu_wait_status_reply(self, timeout: float = 1.0):
        """Read frames from the bus until one matches the status reply
        CAN ID. Other frames are ignored. Returns the parsed dict or
        None on timeout."""
        s = self._v2s()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._manual_bus.recv(
                timeout=max(deadline - time.monotonic(), 0.01)
            )
            if msg is None:
                continue
            if msg.arbitration_id != s.bu_status_reply_can_id:
                continue
            if len(msg.data) < 15:
                continue
            d = msg.data
            return {
                "echo":        d[0],
                "target":      d[1],
                "state":       d[2],
                "error":       d[3],
                "bytes_sent":  d[4] | (d[5] << 8) | (d[6] << 16) | (d[7] << 24),
                "image_size":  d[8] | (d[9] << 8) | (d[10] << 16) | (d[11] << 24),
                "last_done":   d[12],
                "bulk_bitmap": d[13] | (d[14] << 8),
            }
        return None

    def _bu_update_status_labels(self, reply):
        if reply is None:
            self.bu_status_state.set("(no reply)")
            self.bu_status_error.set("")
            self.bu_status_target.set("")
            self.bu_status_progress.set("")
            self.bu_status_bulk.set("")
            return
        state_name = self._BU_MASTER_STATES.get(
            reply["state"], f"UNKNOWN({reply['state']})"
        )
        err_name = self._BU_MASTER_ERRORS.get(
            reply["error"], f"UNKNOWN({reply['error']})"
        )
        target_txt = (
            "(idle)" if reply["target"] == 0xFF
            else f"BU #{reply['target']} (0x{reply['target']:02X})"
        )
        self.bu_status_state.set(f"{state_name} (0x{reply['state']:02X})")
        self.bu_status_error.set(f"{err_name} (0x{reply['error']:02X})")
        self.bu_status_target.set(target_txt)
        if reply["image_size"] > 0:
            pct = reply["bytes_sent"] * 100 / reply["image_size"]
            self.bu_status_progress.set(
                f"{reply['bytes_sent']:,} / {reply['image_size']:,} bytes  ({pct:.1f}%)"
            )
        else:
            self.bu_status_progress.set(f"{reply['bytes_sent']:,} bytes")
        # Bulk bitmap: bit 0 = BU 11, bit 11 = BU 22
        done_ids = [
            11 + b for b in range(12) if (reply["bulk_bitmap"] >> b) & 1
        ]
        last = (
            "none" if reply["last_done"] == 0xFF else f"BU #{reply['last_done']}"
        )
        self.bu_status_bulk.set(
            f"last done: {last}   completed: "
            + (", ".join(f"#{x}" for x in done_ids) if done_ids else "(none)")
        )

    def _bu_trigger_upgrade(self):
        try:
            target = int(self.vars["bu_target_id"].get())
        except Exception:
            self._log_line("[ERROR] Invalid BU target id.\n", "err"); return
        s = self._v2s()
        self._log_line(
            f"\n[BU OTA] Trigger upgrade: target=0x{target:02X} "
            f"(opcode 0x{s.bu_cmd_start_upgrade:02X} on CAN ID "
            f"0x{s.bu_trigger_can_id:03X})\n",
            "info",
        )
        if not self._bu_send_cmd(s.bu_cmd_start_upgrade, target):
            return
        # The S-Board replies with a status frame immediately after
        # kicking off fw_bu_master. Read it to confirm acceptance.
        reply = self._bu_wait_status_reply(timeout=1.5)
        self._bu_update_status_labels(reply)
        if reply is None:
            self._log_line(
                "  No immediate status reply — check connection, "
                "Bank 1 staging state, or the S-Board firmware build.\n",
                "warn",
            )
        else:
            self._log_line(
                f"  Accepted. Master state = "
                f"{self._BU_MASTER_STATES.get(reply['state'], 'UNKNOWN')}\n",
                "ok",
            )

    def _bu_poll_status(self):
        s = self._v2s()
        if not self._bu_send_cmd(s.bu_cmd_status_request, 0):
            return
        reply = self._bu_wait_status_reply(timeout=1.0)
        self._bu_update_status_labels(reply)
        if reply is None:
            self._log_line("[BU OTA] Poll: no reply.\n", "warn")
        else:
            state = self._BU_MASTER_STATES.get(reply["state"], "UNKNOWN")
            self._log_line(
                f"[BU OTA] Poll: {state} "
                f"target=0x{reply['target']:02X} "
                f"bytes={reply['bytes_sent']}/{reply['image_size']}\n",
                "muted",
            )

    def _bu_auto_poll_toggle(self):
        if self._bu_auto_poll:
            self._bu_auto_poll = False
            self.bu_auto_btn.configure(
                text="Start Auto-poll",
                fg_color=("#357", "#357"),
                hover_color=("#468", "#468"),
            )
            if self._bu_auto_poll_job is not None:
                try:
                    self.after_cancel(self._bu_auto_poll_job)
                except Exception:
                    pass
                self._bu_auto_poll_job = None
            self._log_line("[BU OTA] Auto-poll stopped.\n", "muted")
        else:
            if not self._require_bus():
                return
            self._bu_auto_poll = True
            self.bu_auto_btn.configure(
                text="Stop Auto-poll",
                fg_color=("#a55", "#a44"),
                hover_color=("#c66", "#c55"),
            )
            self._log_line("[BU OTA] Auto-poll started.\n", "ok")
            self._bu_auto_poll_tick()

    def _bu_auto_poll_tick(self):
        if not self._bu_auto_poll:
            return
        # Polls happen on the Tk main thread via after(). Safe because
        # the python-can recv we do inside _bu_poll_status has a short
        # timeout (<= 1 s) so the UI stays responsive.
        self._bu_poll_status()
        if self._bu_auto_poll:
            try:
                period = max(100, int(self.vars["bu_auto_poll_ms"].get()))
            except Exception:
                period = 1000
            self._bu_auto_poll_job = self.after(
                period, self._bu_auto_poll_tick
            )

    def _bu_abort(self):
        if not messagebox.askyesno(
            "Abort BU Upgrade",
            "Send CMD_BU_FW_ABORT (0x55) as a raw frame on the BU-side\n"
            "CAN (ID 0x31). This tells whichever BU board is currently\n"
            "receiving to drop the transfer and resume normal ops.\n\n"
            "Use this if a BU upgrade is stuck.",
        ):
            return
        if not self._require_bus():
            return
        frame = bytearray(64)
        frame[0] = 0x55  # BU_CMD_FW_ABORT
        frame[1] = 0xFF  # broadcast target
        try:
            self._manual_bus.send(
                can.Message(
                    arbitration_id=0x31,
                    data=bytes(frame),
                    is_extended_id=False,
                    is_fd=True,
                    bitrate_switch=True,
                )
            )
            self._log_line("[BU OTA] Sent ABORT (0x55) on ID 0x31.\n", "ok")
        except Exception as e:
            self._log_line(f"[BU OTA] ABORT send failed: {e}\n", "err")

    def _build_can_tab(self, tab):
        s = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True); s.grid_columnconfigure((0,1), weight=1)
        self._sec(s, "PCAN ADAPTER").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0,6))
        ctk.CTkLabel(s, text="Channel", anchor="w").grid(row=1, column=0, sticky="w", padx=4)
        ctk.CTkComboBox(s, variable=self.vars["pcan_channel"],
                        values=[f"PCAN_USBBUS{i}" for i in range(1,9)], width=200
                        ).grid(row=1, column=1, sticky="w", padx=4, pady=(0,8))
        self._sec(s, "BIT TIMING").grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8,6))
        LE(s, "Clock", self.vars["f_clock_mhz"], unit="MHz").grid(row=3, column=0, columnspan=2, sticky="ew", padx=4, pady=4)
        nc = self._card(s, "Nominal"); nc.grid(row=4, column=0, sticky="nsew", padx=4, pady=4)
        LE(nc, "BRP", self.vars["nom_brp"]).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        LE(nc, "TSEG1", self.vars["nom_tseg1"]).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        LE(nc, "TSEG2", self.vars["nom_tseg2"]).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        LE(nc, "SJW", self.vars["nom_sjw"]).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        nc.grid_columnconfigure((0,1), weight=1)
        dc = self._card(s, "Data (BRS)"); dc.grid(row=4, column=1, sticky="nsew", padx=4, pady=4)
        LE(dc, "BRP", self.vars["data_brp"]).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        LE(dc, "TSEG1", self.vars["data_tseg1"]).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        LE(dc, "TSEG2", self.vars["data_tseg2"]).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        LE(dc, "SJW", self.vars["data_sjw"]).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        dc.grid_columnconfigure((0,1), weight=1)
        self.bitrate_var = tk.StringVar()
        ctk.CTkLabel(s, textvariable=self.bitrate_var, text_color=("#357","#7af"),
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=8, pady=(10,6))
        for n in ("f_clock_mhz","nom_brp","nom_tseg1","nom_tseg2","data_brp","data_tseg1","data_tseg2"):
            self.vars[n].trace_add("write", lambda *_: self._upd_br())
        self._upd_br()

    def _build_protocol_tab(self, tab):
        s = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True); s.grid_columnconfigure((0,1,2), weight=1)
        self._sec(s, "CAN IDs").grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0,6))
        LE(s, "Command (PC->MCU)", self.vars["cmd_can_id"]).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "Data (PC->MCU)", self.vars["data_can_id"]).grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        LE(s, "Response (MCU->PC)", self.vars["resp_can_id"]).grid(row=1, column=2, padx=4, pady=4, sticky="ew")
        self._sec(s, "COMMAND CODES (hex OK)").grid(row=2, column=0, columnspan=3, sticky="ew", pady=(12,6))
        LE(s, "FW_START", self.vars["cmd_fw_start"]).grid(row=3, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "FW_HEADER", self.vars["cmd_fw_header"]).grid(row=3, column=1, padx=4, pady=4, sticky="ew")
        LE(s, "FW_COMPLETE", self.vars["cmd_fw_complete"]).grid(row=3, column=2, padx=4, pady=4, sticky="ew")
        self._sec(s, "RESPONSE CODES").grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12,6))
        LE(s, "ACK", self.vars["resp_fw_ack"]).grid(row=5, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "NAK", self.vars["resp_fw_nak"]).grid(row=5, column=1, padx=4, pady=4, sticky="ew")
        LE(s, "CRC PASS", self.vars["resp_fw_crc_pass"]).grid(row=5, column=2, padx=4, pady=4, sticky="ew")
        LE(s, "CRC FAIL", self.vars["resp_fw_crc_fail"]).grid(row=6, column=0, padx=4, pady=4, sticky="ew")

    def _build_timing_tab(self, tab):
        s = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True); s.grid_columnconfigure((0,1), weight=1)
        self._sec(s, "BURST & FRAME").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0,6))
        LE(s, "Burst size", self.vars["burst_size"], unit="frames",
           tip="ACK requested after every N data frames.").grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "Frame size", self.vars["data_frame_size"], unit="bytes",
           tip="CAN-FD payload (64 for full FD).").grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        LE(s, "Inter-frame delay", self.vars["inter_frame_delay_ms"], unit="ms",
           tip="Pause between frames so MCU ISR drains RX buffer.").grid(row=2, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "Max retries", self.vars["max_retries"], unit="per burst",
           tip="Failed burst retransmission attempts.").grid(row=2, column=1, padx=4, pady=4, sticky="ew")
        self._sec(s, "TIMEOUTS").grid(row=3, column=0, columnspan=2, sticky="ew", pady=(12,6))
        LE(s, "ACK timeout", self.vars["ack_timeout"], unit="s").grid(row=4, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "Erase timeout", self.vars["erase_timeout"], unit="s",
           tip="128 sectors, can take >5s.").grid(row=4, column=1, padx=4, pady=4, sticky="ew")
        LE(s, "Verify timeout", self.vars["verify_timeout"], unit="s",
           tip="CRC32 over full image.").grid(row=5, column=0, padx=4, pady=4, sticky="ew")
        self._sec(s, "MANUAL MODE").grid(row=6, column=0, columnspan=2, sticky="ew", pady=(12,6))
        LE(s, "Burst pause", self.vars["manual_burst_pause_ms"], unit="ms",
           tip="Extra pause between bursts in manual data step (0=none).").grid(row=7, column=0, padx=4, pady=4, sticky="ew")

    def _build_header_tab(self, tab):
        s = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        s.pack(fill="both", expand=True); s.grid_columnconfigure((0,1), weight=1)
        self._sec(s, "IMAGE HEADER (CMD_FW_HEADER payload)").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0,6))
        LE(s, "Magic", self.vars["header_magic"], tip="0x4601 - must match firmware parser.").grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "Image type", self.vars["header_image_type"], tip="0x0001 = S-Board.").grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        LE(s, "Dest bank", self.vars["header_dest_bank"], unit="addr", tip="0x0C0000 = Bank 2.").grid(row=2, column=0, padx=4, pady=4, sticky="ew")
        LE(s, "Entry point", self.vars["header_entry_point"], unit="addr", tip="0x082000 (sector 8).").grid(row=2, column=1, padx=4, pady=4, sticky="ew")

    def _build_settings_tab(self, tab):
        w = ctk.CTkFrame(tab, fg_color="transparent"); w.pack(fill="both", expand=True, padx=8, pady=8)
        ctk.CTkLabel(w, text="S-Board Firmware Updater", font=ctk.CTkFont(size=18, weight="bold")).pack(anchor="w", pady=(0,4))
        ctk.CTkLabel(w, text=f"Settings: {SETTINGS_PATH}\nHex values OK in numeric fields (0x30).",
                     justify="left", font=ctk.CTkFont(size=12)).pack(anchor="w")
        br = ctk.CTkFrame(w, fg_color="transparent"); br.pack(anchor="w", pady=(16,0))
        ctk.CTkButton(br, text="Save Settings", width=150, command=self._save).pack(side="left", padx=(0,8))
        ctk.CTkButton(br, text="Reset Defaults", width=160, command=self._reset,
                      fg_color=("#a55","#a44"), hover_color=("#c66","#c55")).pack(side="left")

    # ═══════════ Log ═══════════════════════════════════════════
    def _build_log(self, parent):
        lc = ctk.CTkFrame(parent, corner_radius=10)
        lc.grid(row=0, column=1, sticky="nsew", padx=(8,0))
        lc.grid_rowconfigure(1, weight=1); lc.grid_columnconfigure(0, weight=1)
        hdr = ctk.CTkFrame(lc, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10,4)); hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text="Transfer Log", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(hdr, text="Clear", width=70, height=26, command=self._clear_log,
                      fg_color=("#666","#444"), hover_color=("#777","#555")).grid(row=0, column=1, sticky="e")
        self.log = tk.Text(lc, wrap="none", relief="flat", borderwidth=0, bg="#0e1116", fg="#d6dde6",
                           insertbackground="#fff", font=("Consolas", 10))
        self.log.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0,12))
        for t, c in [("ok","#7adf7a"),("err","#ff6b6b"),("warn","#f1c40f"),("info","#69b1ff"),("muted","#7d8590")]:
            self.log.tag_config(t, foreground=c)
        self.log.configure(state="disabled")

    def _build_status(self):
        bar = ctk.CTkFrame(self, height=80, corner_radius=0)
        bar.grid(row=3, column=0, sticky="ew"); bar.grid_columnconfigure(0, weight=1)
        self.state_var = tk.StringVar(value="Idle."); self.detail_var = tk.StringVar(value="")
        top = ctk.CTkFrame(bar, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(10,2)); top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, textvariable=self.state_var, font=ctk.CTkFont(size=13, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(top, textvariable=self.detail_var, text_color=("#666","#aaa"),
                     font=ctk.CTkFont(size=12)).grid(row=0, column=1, sticky="e")
        self.pb = ctk.CTkProgressBar(bar, height=14, corner_radius=4); self.pb.set(0)
        self.pb.grid(row=1, column=0, sticky="ew", padx=20, pady=(2,14))

    # ═══════════ Helpers ═══════════════════════════════════════
    def _sec(self, p, t):
        return ctk.CTkLabel(p, text=t, anchor="w", font=ctk.CTkFont(size=11, weight="bold"),
                            text_color=("#357","#7af"))
    def _card(self, p, t):
        f = ctk.CTkFrame(p, corner_radius=8)
        ctk.CTkLabel(f, text=t, anchor="w", font=ctk.CTkFont(size=12, weight="bold")
                     ).grid(row=99, column=0, columnspan=2, sticky="ew", padx=8, pady=(6,0))
        return f

    def _upd_br(self):
        try:
            f=float(self.vars["f_clock_mhz"].get())
            nb,nt1,nt2=int(self.vars["nom_brp"].get()),int(self.vars["nom_tseg1"].get()),int(self.vars["nom_tseg2"].get())
            db,dt1,dt2=int(self.vars["data_brp"].get()),int(self.vars["data_tseg1"].get()),int(self.vars["data_tseg2"].get())
            nom=(f*1e6)/(max(nb,1)*(1+nt1+nt2)); dat=(f*1e6)/(max(db,1)*(1+dt1+dt2))
            self.bitrate_var.set(f"Nominal {nom/1e3:.0f} kbps  |  Data {dat/1e6:.2f} Mbps")
        except: self.bitrate_var.set("")

    def _browse(self):
        p = Path(self.vars["bin_path"].get())
        path = filedialog.askopenfilename(title="Select .bin",
            initialdir=str(p.parent if p.parent.exists() else Path.cwd()),
            filetypes=[("Firmware binary","*.bin"),("All","*.*")])
        if path: self.vars["bin_path"].set(path)

    def _refresh_info(self):
        p = Path(self.vars["bin_path"].get())
        if not p.exists(): self.file_info.set("File not found."); return
        sz = p.stat().st_size
        self.file_info.set(f"{p.name}  |  {sz:,} bytes  |  {sz/1024:.1f} KB")

    def _save(self):
        try: self._v2s().save(); self._log_line("[OK] Settings saved.\n", "ok")
        except Exception as e: messagebox.showerror("Save failed", str(e))

    def _reset(self):
        if not messagebox.askyesno("Reset", "Reset all settings to defaults?"): return
        d = FwSettings()
        for f in fields(FwSettings): self.vars[f.name].set(getattr(d, f.name))
        self._log_line("[OK] Reset to defaults.\n", "ok")
        # After resetting we are back in S-Board mode by default — make
        # sure the live fields match what the user now sees.
        self.target_seg.set("S-Board (Bank 2)")
        self._on_target_change("S-Board (Bank 2)")

    # ── Target mode (S-Board self-update vs BU-via-Bank-1) ─────
    # The Protocol / Header tabs always show the *active* preset.
    # Switching modes snapshots the current tab values back into the
    # outgoing preset and loads the incoming one.

    # Field pairs that get swapped between modes. Left = live var name,
    # Right = (s_*, bu_*) preset names.
    _TARGET_FIELD_MAP = (
        ("cmd_can_id",        "s_cmd_can_id",        "bu_cmd_can_id"),
        ("data_can_id",       "s_data_can_id",       "bu_data_can_id"),
        ("resp_can_id",       "s_resp_can_id",       "bu_resp_can_id"),
        ("cmd_fw_start",      "s_cmd_fw_start",      "bu_cmd_fw_start"),
        ("cmd_fw_header",     "s_cmd_fw_header",     "bu_cmd_fw_header"),
        ("cmd_fw_complete",   "s_cmd_fw_complete",   "bu_cmd_fw_complete"),
        ("resp_fw_ack",       "s_resp_fw_ack",       "bu_resp_fw_ack"),
        ("resp_fw_nak",       "s_resp_fw_nak",       "bu_resp_fw_nak"),
        ("resp_fw_crc_pass",  "s_resp_fw_crc_pass",  "bu_resp_fw_crc_pass"),
        ("resp_fw_crc_fail",  "s_resp_fw_crc_fail",  "bu_resp_fw_crc_fail"),
        ("header_dest_bank",  "s_header_dest_bank",  "bu_header_dest_bank"),
        ("header_image_type", "s_header_image_type", "bu_header_image_type"),
    )

    def _stash_active_into(self, mode: str):
        """Copy the live tab values back into the s_*/bu_* preset slots."""
        slot = 1 if mode == "S-Board" else 2
        for live, *presets in self._TARGET_FIELD_MAP:
            try:
                v = self.vars[live].get()
                # IntVar.get() can raise on empty/garbage — fall back to
                # parsing the string form so we accept "0x18" too.
                if isinstance(v, str):
                    v = int(v, 0)
            except Exception:
                continue
            self.vars[presets[slot - 1]].set(v)

    def _load_preset_into_active(self, mode: str):
        """Copy the s_*/bu_* preset slot values into the live tab vars."""
        slot = 1 if mode == "S-Board" else 2
        for live, *presets in self._TARGET_FIELD_MAP:
            try:
                v = self.vars[presets[slot - 1]].get()
            except Exception:
                continue
            self.vars[live].set(v)

    def _on_target_change(self, choice: str):
        """User clicked the segmented Target button."""
        new_mode = "BU via Bank1" if choice.startswith("BU") else "S-Board"
        old_mode = self.settings.target_mode
        if new_mode == old_mode:
            self._refresh_target_labels()
            return
        # Snapshot the values currently on screen back into the OUTGOING
        # preset so any tweaks the user made survive the swap.
        self._stash_active_into(old_mode)
        # Bring the incoming preset's values into the live vars.
        self._load_preset_into_active(new_mode)
        # Persist the new mode in BOTH places — the dataclass snapshot
        # AND the Tk var. _v2s reads the Tk var as the source of truth,
        # so forgetting this side leaves the send path stuck on the
        # previous mode regardless of what the segmented button shows.
        self.settings.target_mode = new_mode
        self.vars["target_mode"].set(new_mode)
        self._refresh_target_labels()
        self._log_line(f"[OK] Target mode → {new_mode}\n", "ok")

    def _refresh_target_labels(self):
        """Update button label + hint text to reflect the current mode."""
        mode = self.settings.target_mode
        if mode == "BU via Bank1":
            self.send_btn_text.set("Send BU Firmware")
            try:
                bank = int(self.vars["header_dest_bank"].get(), 0) \
                       if isinstance(self.vars["header_dest_bank"].get(), str) \
                       else int(self.vars["header_dest_bank"].get())
            except Exception:
                bank = 0x0A0000
            self.target_hint.set(
                f"BU image → S-Board Bank 1 (0x{bank:06X}). "
                f"Stages on the S-Board; fw_bu_master will push it to a BU board next.")
        else:
            self.send_btn_text.set("Send Firmware")
            try:
                bank = int(self.vars["header_dest_bank"].get(), 0) \
                       if isinstance(self.vars["header_dest_bank"].get(), str) \
                       else int(self.vars["header_dest_bank"].get())
            except Exception:
                bank = 0x0C0000
            self.target_hint.set(
                f"S-Board self-update → Bank 2 (0x{bank:06X}). Boot manager copies on reset.")

    # ═══════════ Log queue ═════════════════════════════════════
    def _log_line(self, text, tag=""):
        self.log.configure(state="normal")
        if text.endswith("\r"):
            ls = self.log.index("end-1c linestart"); self.log.delete(ls, "end-1c")
            self.log.insert(ls, text.rstrip("\r"), tag) if tag else self.log.insert(ls, text.rstrip("\r"))
        else:
            self.log.insert("end", text, tag) if tag else self.log.insert("end", text)
        self.log.see("end"); self.log.configure(state="disabled")
        self._upd_progress(text)

    def _classify(self, l):
        s = l.strip()
        if "ABORTED" in s: return "warn"
        if "FAIL" in s or "ERROR" in s: return "err"
        if "OK" in s or "PASS" in s or "COMPLETE" in s: return "ok"
        if s.startswith("["): return "info"
        if "Retry" in s or "mismatch" in s: return "warn"
        if s.startswith("  "): return "muted"
        return ""

    def _drain(self):
        try:
            while True:
                c = self._q.get_nowait(); self._log_line(c, self._classify(c))
        except queue.Empty: pass
        self.after(60, self._drain)

    def _clear_log(self):
        self.log.configure(state="normal"); self.log.delete("1.0","end"); self.log.configure(state="disabled")

    def _upd_progress(self, text):
        m = self.BURST_RE.search(text)
        if m:
            pct, elapsed = int(m.group(3)), float(m.group(4))
            self.pb.set(pct/100.0)
            p = Path(self.vars["bin_path"].get())
            tput = (p.stat().st_size * pct / 100.0 / max(elapsed,0.001) / 1024) if p.exists() else 0
            eta = (elapsed/max(pct,1))*(100-pct)
            self.detail_var.set(f"{pct}%  |  {tput:.1f} KB/s  |  ETA {eta:.1f}s")
        line = text.strip()
        bank_label = "Bank 1" if self.settings.target_mode == "BU via Bank1" else "Bank 2"
        if line.startswith("[START]"): self.state_var.set(f"Erasing {bank_label}..."); self.pb.set(0)
        elif line.startswith("[HEADER]"): self.state_var.set("Sending header...")
        elif line.startswith("[DATA]"): self.state_var.set("Streaming firmware...")
        elif line.startswith("[VERIFY]"): self.state_var.set("Verifying CRC32..."); self.pb.set(1.0)
        elif "FIRMWARE TRANSFER COMPLETE" in line:
            done = "BU image staged in Bank 1." \
                if self.settings.target_mode == "BU via Bank1" \
                else "Done - S-Board resetting."
            self.state_var.set(done); self.detail_var.set("")
        elif "FAILED" in line and "***" in line: self.state_var.set("Transfer failed."); self.detail_var.set("")
        elif "SUCCESS" in line and "***" in line: self.state_var.set("Transfer complete.")
        elif "ABORTED" in line: self.state_var.set("Transfer aborted."); self.detail_var.set("")

    # ═══════════ Auto send / abort ═════════════════════════════
    def _on_send(self):
        if self._worker and self._worker.is_alive():
            messagebox.showinfo("Busy", "Transfer running."); return
        p = Path(self.vars["bin_path"].get())
        if not p.exists(): messagebox.showerror("File not found", str(p)); return
        try: s = self._v2s()
        except Exception as e: messagebox.showerror("Invalid settings", str(e)); return
        try: s.save()
        except: pass
        apply_settings(s)
        self._clear_log(); self._abort.clear()
        self.send_btn.configure(state="disabled", text="Sending...")
        self.abort_btn.configure(state="normal")
        self.pb.set(0); self.state_var.set("Connecting..."); self.detail_var.set("")
        self._worker = threading.Thread(target=self._auto_worker, args=(s,), daemon=True)
        self._worker.start()

    def _on_abort(self):
        if not self._worker or not self._worker.is_alive(): return
        self._abort.set(); self.abort_btn.configure(state="disabled", text="Aborting...")
        self.state_var.set("Aborting...")

    def _finish(self):
        self.send_btn.configure(state="normal", text="Send Firmware")
        self.abort_btn.configure(state="disabled", text="Abort")

    def _auto_worker(self, s):
        old = sys.stdout; sys.stdout = _QueueWriter(self._q)
        try:
            try: sender = fw_sender.FirmwareSender(channel=s.pcan_channel)
            except Exception as e:
                print(f"\nERROR opening {s.pcan_channel}: {e}\n"); self.after(0, self._finish); return
            sender.bus = _AbortableBus(sender.bus, self._abort)
            try:
                ok = sender.send_firmware(s.bin_path, version=int(s.version))
                self._q.put("\n*** SUCCESS ***\n" if ok else "\n*** FAILED ***\n")
            except _TransferAborted: self._q.put("\n*** ABORTED by user ***\n")
            finally: sender.close()
        finally: sys.stdout = old; self.after(0, self._finish)

    # ═══════════ Manual control ════════════════════════════════
    def _manual_connect(self):
        if self._manual_bus:
            self._log_line("[WARN] Already connected.\n", "warn"); return
        s = self._v2s()
        apply_settings(s)
        try:
            self._manual_bus = can.Bus(interface='pcan', channel=s.pcan_channel, fd=True,
                                        **fw_sender.PCAN_FD_PARAMS)
            self.conn_status.set("Connected")
            self.conn_btn.configure(state="disabled"); self.disconn_btn.configure(state="normal")
            self._log_line(f"[OK] Connected to {s.pcan_channel}.\n", "ok")
        except Exception as e:
            self._log_line(f"[ERROR] {e}\n", "err")

    def _manual_disconnect(self):
        if self._manual_bus:
            self._manual_bus.shutdown(); self._manual_bus = None
        self.conn_status.set("Disconnected")
        self.conn_btn.configure(state="normal"); self.disconn_btn.configure(state="disabled")
        self._log_line("[OK] Disconnected.\n", "ok")

    def _require_bus(self):
        if not self._manual_bus:
            self._log_line("[ERROR] Not connected. Click 'Connect PCAN' first.\n", "err"); return False
        return True

    def _manual_send_cmd(self, cmd, payload=b''):
        s = self._v2s()
        frame = bytearray(64)
        frame[0] = cmd; frame[1] = 0x01
        frame[4:4+len(payload)] = payload[:60]
        msg = can.Message(arbitration_id=s.cmd_can_id, data=bytes(frame),
                          is_extended_id=False, is_fd=True, bitrate_switch=True)
        self._manual_bus.send(msg)

    def _manual_wait_resp(self, timeout=2.0):
        s = self._v2s()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self._manual_bus.recv(timeout=max(deadline - time.monotonic(), 0.01))
            if msg and msg.arbitration_id == s.resp_can_id:
                return msg
        return None

    def _manual_start(self):
        if not self._require_bus(): return
        s = self._v2s()
        self._log_line("\n[MANUAL] Step 1: Sending CMD_FW_START...\n", "info")
        self._manual_send_cmd(s.cmd_fw_start)
        self._log_line(f"  Waiting up to {s.erase_timeout}s for ACK...\n", "muted")
        resp = self._manual_wait_resp(timeout=s.erase_timeout)
        if resp and len(resp.data) >= 1 and resp.data[0] == s.resp_fw_ack:
            bank = "Bank 1" if s.target_mode == "BU via Bank1" else "Bank 2"
            self._log_line(f"  OK - {bank} erased, ready to receive.\n", "ok")
        elif resp and len(resp.data) >= 1:
            self._log_line(f"  Got response 0x{resp.data[0]:02X} (expected ACK 0x{s.resp_fw_ack:02X})\n", "err")
        else:
            self._log_line("  FAIL - No response.\n", "err")

    def _manual_header(self):
        if not self._require_bus(): return
        s = self._v2s()
        p = Path(s.bin_path)
        if not p.exists(): self._log_line(f"[ERROR] File not found: {p}\n", "err"); return
        raw = p.read_bytes()
        padded = fw_sender.pad_to_64(raw)
        crc = fw_sender.crc32_firmware(padded)
        total = len(padded) // s.data_frame_size

        self._manual_padded = padded
        self._manual_total_frames = total
        self._manual_frame_idx = 0
        self.manual_data_status.set(f"Loaded: {len(raw):,}B raw, {len(padded):,}B padded, {total} frames, CRC 0x{crc:08X}")

        self._log_line(f"\n[MANUAL] Step 2: Sending CMD_FW_HEADER...\n", "info")
        self._log_line(f"  Size: {len(padded):,}B  Frames: {total}  CRC: 0x{crc:08X}  Ver: {s.version}\n", "muted")

        payload = struct.pack('<HHIIIIII', s.header_magic, s.header_image_type,
            len(padded), crc, s.version, s.header_dest_bank, s.header_entry_point, total)
        self._manual_send_cmd(s.cmd_fw_header, payload)
        resp = self._manual_wait_resp(timeout=s.ack_timeout)
        if resp and len(resp.data) >= 1 and resp.data[0] == s.resp_fw_ack:
            self._log_line("  OK - Header accepted.\n", "ok")
        else:
            self._log_line("  FAIL - No ACK.\n", "err")

    def _manual_data(self):
        if not self._require_bus(): return
        if self._manual_padded is None:
            self._log_line("[ERROR] No data loaded. Run Step 2 first.\n", "err"); return
        s = self._v2s()
        total = self._manual_total_frames
        burst = s.burst_size
        delay = s.inter_frame_delay_ms / 1000.0
        self._log_line(f"\n[MANUAL] Step 3: Streaming {total} frames, burst={burst}...\n", "info")

        # Run in thread so UI stays responsive
        def worker():
            idx = self._manual_frame_idx
            t0 = time.monotonic()
            while idx < total:
                frames_in_burst = min(burst, total - idx)
                for i in range(frames_in_burst):
                    off = idx * s.data_frame_size
                    data = self._manual_padded[off:off+s.data_frame_size]
                    msg = can.Message(arbitration_id=s.data_can_id, data=data,
                                      is_extended_id=False, is_fd=True, bitrate_switch=True)
                    self._manual_bus.send(msg)
                    idx += 1
                    if i < frames_in_burst - 1: time.sleep(delay)
                # Wait for ACK
                resp = self._manual_wait_resp(timeout=s.ack_timeout)
                if not resp or resp.data[0] != s.resp_fw_ack:
                    self._q.put(f"  FAIL at frame {idx} - no ACK.\n"); break
                pct = idx * 100 // total
                elapsed = time.monotonic() - t0
                self._q.put(f"  Burst {idx//burst}/{(total+burst-1)//burst} - {pct}% ({elapsed:.1f}s)\r")
                if s.manual_burst_pause_ms > 0:
                    time.sleep(s.manual_burst_pause_ms / 1000.0)
            self._manual_frame_idx = idx
            elapsed = time.monotonic() - t0
            self._q.put(f"\n  Done: {idx}/{total} frames in {elapsed:.1f}s\n")
            self.after(0, lambda: self.manual_data_status.set(
                f"Sent {idx}/{total} frames."))
        threading.Thread(target=worker, daemon=True).start()

    def _manual_complete(self):
        if not self._require_bus(): return
        s = self._v2s()
        self._log_line(f"\n[MANUAL] Step 4: Sending CMD_FW_COMPLETE...\n", "info")

        # Drain any stale frames the data phase may have left behind
        # (notably a duplicate burst-ACK from a retransmitted partial burst).
        drained = 0
        while True:
            m = self._manual_bus.recv(timeout=0.05)
            if m is None: break
            drained += 1
        if drained:
            self._log_line(f"  (drained {drained} stale frame(s) before verify)\n", "muted")

        self._manual_send_cmd(s.cmd_fw_complete)
        self._log_line(f"  Waiting up to {s.verify_timeout}s...\n", "muted")

        # Filter for a CRC verify result. Skip past any unrelated
        # frames (e.g. late burst-ACKs) until we either find the
        # CRC_PASS / CRC_FAIL response or hit the verify timeout.
        deadline = time.monotonic() + s.verify_timeout
        resp = None
        while time.monotonic() < deadline:
            msg = self._manual_bus.recv(timeout=max(deadline - time.monotonic(), 0.01))
            if msg is None or msg.arbitration_id != s.resp_can_id:
                continue
            if len(msg.data) < 1:
                continue
            code = msg.data[0]
            if code == s.resp_fw_crc_pass or code == s.resp_fw_crc_fail:
                resp = msg
                break
            self._log_line(f"  (ignoring stale 0x{code:02X}, waiting for CRC result)\n", "muted")

        if resp and len(resp.data) >= 1:
            code = resp.data[0]
            if code == s.resp_fw_crc_pass:
                self._log_line("  CRC PASSED - S-Board will write boot flag and reset.\n", "ok")
            elif code == s.resp_fw_crc_fail:
                self._log_line("  CRC FAILED - data corruption or wrong .bin file.\n", "err")
        else:
            self._log_line("  FAIL - No verify response.\n", "err")

    def _manual_one_frame(self):
        if not self._require_bus(): return
        if self._manual_padded is None:
            self._log_line("[ERROR] No data loaded. Run Step 2 first.\n", "err"); return
        s = self._v2s()
        idx = self._manual_frame_idx
        if idx >= self._manual_total_frames:
            self._log_line(f"[WARN] All {self._manual_total_frames} frames already sent.\n", "warn"); return
        off = idx * s.data_frame_size
        data = self._manual_padded[off:off+s.data_frame_size]
        msg = can.Message(arbitration_id=s.data_can_id, data=data,
                          is_extended_id=False, is_fd=True, bitrate_switch=True)
        self._manual_bus.send(msg)
        self._manual_frame_idx = idx + 1
        hex_preview = ' '.join(f'{b:02X}' for b in data[:16])
        self._log_line(f"  Frame {idx}: [{hex_preview} ...] ({self._manual_frame_idx}/{self._manual_total_frames})\n", "muted")
        self.manual_data_status.set(f"Sent {self._manual_frame_idx}/{self._manual_total_frames} frames.")

    # ═══════════ MCU Operations ═══════════════════════════════
    def _mcu_send_cmd(self, cmd, payload=b''):
        if not self._require_bus(): return None
        s = self._v2s()
        frame = bytearray(64)
        frame[0] = cmd; frame[1] = 0x01
        frame[4:4+len(payload)] = payload[:60]
        msg = can.Message(arbitration_id=s.cmd_can_id, data=bytes(frame),
                          is_extended_id=False, is_fd=True, bitrate_switch=True)
        self._manual_bus.send(msg)
        return self._manual_wait_resp(timeout=s.ack_timeout)

    def _mcu_read_flag(self):
        self._log_line("\n[MCU] Reading boot flag...\n", "info")
        resp = self._mcu_send_cmd(0x34)  # CMD_READ_FLAG
        if resp and len(resp.data) >= 20 and resp.data[0] == 0x29:
            d = resp.data[4:]
            pending = d[0] | (d[1] << 8)
            crcflag = d[2] | (d[3] << 8)
            imgsize = d[4] | (d[5] << 8) | (d[6] << 16) | (d[7] << 24)
            imgcrc  = d[8] | (d[9] << 8) | (d[10] << 16) | (d[11] << 24)
            self._log_line(f"  updatePending: 0x{pending:04X} {'(SET)' if pending == 0xA5A5 else '(not set)'}\n",
                           "ok" if pending == 0xA5A5 else "muted")
            self._log_line(f"  crcValid:      0x{crcflag:04X} {'(SET)' if crcflag == 0x5A5A else '(not set)'}\n",
                           "ok" if crcflag == 0x5A5A else "muted")
            self._log_line(f"  imageSize:     {imgsize} bytes (0x{imgsize:08X})\n", "muted")
            self._log_line(f"  imageCRC:      0x{imgcrc:08X}\n", "muted")
        elif resp:
            self._log_line(f"  Unexpected response: 0x{resp.data[0]:02X}\n", "warn")
        else:
            self._log_line("  No response.\n", "err")

    def _mcu_clear_flag(self):
        if not messagebox.askyesno("Clear Flag", "Erase Bank 3 sector 0 (boot flag)?"): return
        self._log_line("\n[MCU] Clearing boot flag...\n", "info")
        resp = self._mcu_send_cmd(0x35)  # CMD_CLEAR_FLAG
        if resp and resp.data[0] == 0x25:
            self._log_line("  OK - Boot flag cleared.\n", "ok")
        else:
            self._log_line("  FAIL - No ACK.\n", "err")

    def _mcu_write_flag(self):
        s = self._v2s()
        p = Path(s.bin_path)
        if not p.exists():
            self._log_line("[ERROR] .bin file not found for CRC calculation.\n", "err"); return
        raw = p.read_bytes()
        padded = fw_sender.pad_to_64(raw)
        crc = fw_sender.crc32_firmware(padded)
        size = len(padded)
        if not messagebox.askyesno("Write Flag",
            f"Write boot flag?\n\nSize: {size}\nCRC: 0x{crc:08X}\n\nThis will trigger update on next reset."): return
        self._log_line(f"\n[MCU] Writing boot flag (size={size}, CRC=0x{crc:08X})...\n", "info")
        payload = struct.pack('<II', size, crc)
        resp = self._mcu_send_cmd(0x3A, payload)  # CMD_WRITE_FLAG
        if resp and resp.data[0] == 0x25:
            self._log_line("  OK - Boot flag written.\n", "ok")
        else:
            self._log_line("  FAIL - No ACK.\n", "err")

    def _mcu_read_flash(self):
        if not self._require_bus(): return
        try: addr = int(self.mcu_addr_var.get(), 0)
        except: self._log_line("[ERROR] Invalid address.\n", "err"); return
        self._log_line(f"\n[MCU] Reading 8 words from 0x{addr:08X}...\n", "info")
        payload = struct.pack('<I', addr)
        resp = self._mcu_send_cmd(0x37, payload)  # CMD_READ_FLASH
        if resp and len(resp.data) >= 20 and resp.data[0] == 0x2A:
            d = resp.data[4:]
            words = []
            for i in range(8):
                w = d[i*2] | (d[i*2+1] << 8)
                words.append(w)
            hex_str = ' '.join(f'{w:04X}' for w in words)
            self._log_line(f"  [{hex_str}]\n", "muted")
            all_ff = all(w == 0xFFFF for w in words)
            if all_ff: self._log_line("  (all 0xFFFF = erased flash)\n", "warn")
        elif resp:
            self._log_line(f"  Unexpected: 0x{resp.data[0]:02X}\n", "warn")
        else:
            self._log_line("  No response.\n", "err")

    def _mcu_erase_sector(self):
        try: addr = int(self.mcu_addr_var.get(), 0)
        except: self._log_line("[ERROR] Invalid address.\n", "err"); return
        if not messagebox.askyesno("Erase", f"Erase sector at 0x{addr:08X}?"): return
        self._log_line(f"\n[MCU] Erasing sector at 0x{addr:08X}...\n", "info")
        payload = struct.pack('<I', addr)
        resp = self._mcu_send_cmd(0x39, payload)  # CMD_ERASE_SECTOR
        if resp and resp.data[0] == 0x25:
            self._log_line("  OK - Sector erased.\n", "ok")
        else:
            self._log_line("  FAIL.\n", "err")

    def _mcu_compute_crc(self):
        if not self._require_bus(): return
        try: addr = int(self.mcu_crc_addr.get(), 0)
        except: self._log_line("[ERROR] Invalid address.\n", "err"); return
        try: size = int(self.mcu_crc_size.get(), 0)
        except: self._log_line("[ERROR] Invalid size.\n", "err"); return
        self._log_line(f"\n[MCU] Computing CRC32 at 0x{addr:08X}, {size} bytes...\n", "info")
        payload = struct.pack('<II', addr, size)
        s = self._v2s()
        resp = self._mcu_send_cmd(0x38, payload)  # CMD_COMPUTE_CRC
        if resp and len(resp.data) >= 8 and resp.data[0] == 0x2B:
            d = resp.data[4:]
            crc = d[0] | (d[1] << 8) | (d[2] << 16) | (d[3] << 24)
            self._log_line(f"  MCU CRC32: 0x{crc:08X}\n", "ok")
            # Also compute locally if we have the bin
            p = Path(s.bin_path)
            if p.exists():
                raw = p.read_bytes()
                padded = fw_sender.pad_to_64(raw)
                local_crc = fw_sender.crc32_firmware(padded)
                match = "MATCH" if crc == local_crc else "MISMATCH"
                tag = "ok" if crc == local_crc else "err"
                self._log_line(f"  Local CRC: 0x{local_crc:08X} ({match})\n", tag)
        elif resp:
            self._log_line(f"  Unexpected: 0x{resp.data[0]:02X}\n", "warn")
        else:
            self._log_line("  No response.\n", "err")

    def _mcu_get_state(self):
        self._log_line("\n[MCU] Querying state...\n", "info")
        resp = self._mcu_send_cmd(0x3B)  # CMD_GET_STATE
        if resp and len(resp.data) >= 12 and resp.data[0] == 0x2C:
            d = resp.data[4:]
            states = {0:"IDLE", 1:"ERASING", 2:"WAITING_HEADER", 3:"RECEIVING", 4:"VERIFYING"}
            st = d[0]; frames = d[1] | (d[2] << 8); burst = d[3]
            waddr = d[4] | (d[5] << 8) | (d[6] << 16) | (d[7] << 24)
            self._log_line(f"  State:     {states.get(st, f'UNKNOWN({st})')}\n", "ok")
            self._log_line(f"  Frames:    {frames}\n", "muted")
            self._log_line(f"  Burst cnt: {burst}\n", "muted")
            self._log_line(f"  Write addr: 0x{waddr:08X}\n", "muted")
        elif resp:
            self._log_line(f"  Unexpected: 0x{resp.data[0]:02X}\n", "warn")
        else:
            self._log_line("  No response.\n", "err")

    def _mcu_reset(self):
        if not messagebox.askyesno("Reset", "Reset the S-Board MCU?"): return
        self._log_line("\n[MCU] Resetting device...\n", "info")
        resp = self._mcu_send_cmd(0x36)  # CMD_RESET_DEVICE
        if resp and resp.data[0] == 0x25:
            self._log_line("  OK - Device resetting.\n", "ok")
        else:
            self._log_line("  Sent (no ACK expected after reset).\n", "warn")

    def _mcu_send_raw(self):
        if not self._require_bus(): return
        try: cid = int(self.raw_id_var.get(), 0)
        except: self._log_line("[ERROR] Invalid CAN ID.\n", "err"); return
        try:
            hex_str = self.raw_data_var.get().replace(',', ' ').strip()
            data_bytes = bytes.fromhex(hex_str.replace(' ', ''))
        except: self._log_line("[ERROR] Invalid hex data.\n", "err"); return
        frame = bytearray(64)
        frame[:len(data_bytes)] = data_bytes[:64]
        msg = can.Message(arbitration_id=cid, data=bytes(frame),
                          is_extended_id=False, is_fd=True, bitrate_switch=True)
        self._manual_bus.send(msg)
        preview = ' '.join(f'{b:02X}' for b in data_bytes[:16])
        self._log_line(f"[TX] ID={cid} [{preview}...]\n", "ok")

    # ═══════════ CAN Monitor ═══════════════════════════════════
    def _monitor_toggle(self):
        if self._monitoring:
            self._monitoring = False
            self.monitor_btn.configure(text="Start Monitor")
            self.monitor_status.set("Stopped")
        else:
            if not self._manual_bus:
                self._log_line("[ERROR] Connect PCAN first.\n", "err"); return
            self._monitoring = True
            self.monitor_btn.configure(text="Stop Monitor")
            self.monitor_status.set("Running...")
            self._monitor_thread = threading.Thread(target=self._monitor_worker, daemon=True)
            self._monitor_thread.start()

    def _monitor_worker(self):
        while self._monitoring and self._manual_bus:
            try:
                msg = self._manual_bus.recv(timeout=0.1)
            except: break
            if msg is None: continue
            ts = time.strftime("%H:%M:%S")
            d = ' '.join(f'{b:02X}' for b in msg.data[:min(len(msg.data), 16)])
            direction = "RX"
            line = f"[{ts}] ID=0x{msg.arbitration_id:03X} DLC={msg.dlc:2d} [{d}]\n"
            self.after(0, lambda l=line: self._monitor_append(l))

    def _monitor_append(self, line):
        self.monitor_text.configure(state="normal")
        tag = "rx" if "RX" in line[:10] else ""
        self.monitor_text.insert("end", line, tag)
        self.monitor_text.see("end")
        self.monitor_text.configure(state="disabled")

    def _monitor_clear(self):
        self.monitor_text.configure(state="normal")
        self.monitor_text.delete("1.0", "end")
        self.monitor_text.configure(state="disabled")

    # ═══════════ Quit ══════════════════════════════════════════
    def _on_quit(self):
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno("Busy", "Transfer running. Quit anyway?"): return
        if self._manual_bus:
            self._manual_bus.shutdown()
        try: self._v2s().save()
        except: pass
        self.destroy()

def main():
    App().mainloop()

if __name__ == "__main__":
    main()
