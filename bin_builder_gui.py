"""
C2000 Binary Image Builder — GUI
================================
Convert a TI C2000 .out file to a flat .bin suitable for OTA streaming.
Auto-detects the firmware footprint from the matching .map file and
generates a tight-fit binary via hex2000 — no hand-editing
hex_image.hexcmd required.

Workflow:
  1. Pick a .out file
  2. The matching .map is auto-detected next to it
  3. Flash sections in Bank 0 are parsed and listed
  4. Footprint (start word, end word, length bytes) is computed
  5. Click "Build .bin" — temporary hex_image.cmd is created, hex2000
     runs, and the .bin is written next to the .out

Run:
    pip install customtkinter
    python bin_builder_gui.py
"""
from __future__ import annotations

import glob
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk


# ═══════════════════════════════════════════════════════════════════
#  hex2000 auto-detection
# ═══════════════════════════════════════════════════════════════════
def find_hex2000() -> str:
    """Try a few common TI install paths for hex2000.exe."""
    patterns = [
        "C:/ti/ccs*/ccs/tools/compiler/ti-cgt-c2000_*/bin/hex2000.exe",
        "C:/ti/ccs*/tools/compiler/ti-cgt-c2000_*/bin/hex2000.exe",
        "C:/ti/c2000Ware_*/utilities/flash_programmers/hex2000.exe",
        "/usr/local/ti/ccs*/ccs/tools/compiler/ti-cgt-c2000_*/bin/hex2000",
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]  # newest version when multiple match
    return ""


# ═══════════════════════════════════════════════════════════════════
#  .map file parser
# ═══════════════════════════════════════════════════════════════════
@dataclass
class Section:
    name: str
    origin: int   # word address
    length: int   # words

    @property
    def end(self) -> int:
        return self.origin + self.length


# Sections that live in flash and contribute bytes to the OTA image.
# Anything not in this set (e.g. .bss, .stack, .data) is RAM and
# should NOT be in the binary.
FLASH_SECTIONS = {
    "codestart",
    ".text",
    ".cinit",
    ".const",
    ".switch",
    ".init_array",
    ".pinit",
    ".econst",
    "FPUmathTables",
    # .TI.ramfunc has a flash LOAD address even though it RUNs in RAM —
    # the load image must be in the .bin so the C runtime can copy it.
    ".TI.ramfunc",
}

# Bank 0 flash range for F28P55x (word addresses).
BANK0_START_WORD = 0x080000
BANK0_END_WORD = 0x0A0000


# Section-table line formats in TI C2000 .map files:
#
#  Form A — name + pad + origin + length all on one line:
#     .text      0    00082c98    00002b55
#     .cinit     0    000857f0    000000b8     UNINITIALIZED
#
#  Form B — name on its own line, then "*  pad  origin  length" on the
#  next line. Used by codestart, .TI.ramfunc, .init_array, FPUmathTables:
#     codestart
#     *          0    00082000    00000002
#     .TI.ramfunc
#     *          0    00082008    00000520     RUN ADDR = 00009000
#
# We need both — Form A captures the bulk of the footprint, Form B
# captures `codestart` (the entry vector we MUST keep) and the LOAD
# image of `.TI.ramfunc` (otherwise the runtime copy step reads 0xFF
# into RAM and the firmware crashes the moment a ramfunc is called).
_SECTION_FORM_A = re.compile(
    r"^(?P<name>\S+)\s+\d+\s+"
    r"(?P<origin>[0-9a-fA-F]{8})\s+"
    r"(?P<length>[0-9a-fA-F]{8})"
)
_SECTION_FORM_B_BODY = re.compile(
    r"^\s*\*\s+\d+\s+"
    r"(?P<origin>[0-9a-fA-F]{8})\s+"
    r"(?P<length>[0-9a-fA-F]{8})"
)


def parse_map_sections(map_path: Path) -> list[Section]:
    """Pull flash-resident sections out of a .map file.

    Returns sections that:
      - are in our FLASH_SECTIONS set
      - have origin in Bank 0 word range
      - have nonzero length

    Handles both single-line and split (name + asterisk continuation)
    section header formats — see comments above for examples.
    """
    text = map_path.read_text(encoding="utf-8", errors="replace")
    out: list[Section] = []
    pending_name: str | None = None  # Form B: name from the previous line

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            pending_name = None
            continue

        # ── Form A: "name pad origin length [attr]" on one line ──
        m = _SECTION_FORM_A.match(raw)
        if m:
            name = m.group("name")

            # If this is actually the asterisk continuation of a Form B
            # header, swap in the name from the previous line.
            if name == "*" and pending_name is not None:
                m_b = _SECTION_FORM_B_BODY.match(raw)
                if m_b:
                    name = pending_name
                    origin = int(m_b.group("origin"), 16)
                    length = int(m_b.group("length"), 16)
                    pending_name = None
                    if (
                        name in FLASH_SECTIONS
                        and length > 0
                        and BANK0_START_WORD <= origin < BANK0_END_WORD
                    ):
                        out.append(Section(name, origin, length))
                    continue

            # Standard Form A line.
            pending_name = None
            origin = int(m.group("origin"), 16)
            length = int(m.group("length"), 16)
            if (
                name in FLASH_SECTIONS
                and length > 0
                and BANK0_START_WORD <= origin < BANK0_END_WORD
            ):
                out.append(Section(name, origin, length))
            continue

        # ── Form B: bare section name on its own line ──
        # Only count it as a pending header if it's a name we recognize
        # — otherwise random tokens elsewhere in the .map would confuse
        # the next iteration.
        if stripped in FLASH_SECTIONS:
            pending_name = stripped
            continue

        pending_name = None

    return out


def compute_footprint(sections: list[Section]) -> tuple[int, int]:
    """Return (start_word, end_word) covering all flash-resident sections."""
    if not sections:
        raise ValueError("No flash sections found in .map (is the build current?)")
    start = min(s.origin for s in sections)
    end = max(s.end for s in sections)
    return start, end


def round_up(value: int, multiple: int) -> int:
    return ((value + multiple - 1) // multiple) * multiple


# ═══════════════════════════════════════════════════════════════════
#  hex_image.cmd generator + hex2000 runner
# ═══════════════════════════════════════════════════════════════════
def build_hex_cmd(start_word: int, length_bytes: int, fill: int = 0xFFFF) -> str:
    start_byte = start_word * 2
    return (
        "/* Auto-generated by bin_builder_gui — do not edit by hand. */\n"
        "--binary\n"
        "--image\n"
        "--order=LS\n"
        "\n"
        "ROMS {\n"
        f"    APPLICATION : o = 0x{start_byte:06X}, l = 0x{length_bytes:X}, fill = 0x{fill:04X}\n"
        "}\n"
    )


def run_hex2000(
    hex2000_exe: Path,
    out_file: Path,
    cmd_file: Path,
    bin_file: Path,
) -> tuple[bool, str]:
    """Invoke hex2000 and capture its stdout/stderr."""
    cmd = [str(hex2000_exe), "-o", str(bin_file), str(cmd_file), str(out_file)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, cwd=str(out_file.parent.parent)
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        ok = (proc.returncode == 0) and bin_file.exists()
        return ok, log
    except FileNotFoundError:
        return False, f"hex2000 not found at: {hex2000_exe}"
    except Exception as e:
        return False, f"hex2000 launch error: {e}"


# ═══════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")
        self.title("C2000 Binary Image Builder")
        self.geometry("980x780")
        self.minsize(880, 660)

        # State
        self.out_path = tk.StringVar()
        self.map_path = tk.StringVar()
        self.bin_path = tk.StringVar()
        self.hex2000_path = tk.StringVar(value=find_hex2000())
        self.start_addr = tk.StringVar(value="auto")
        self.fill_byte = tk.StringVar(value="0xFFFF")
        self.headroom = tk.StringVar(value="256")
        self.full_bank_mode = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Pick a .out file to begin.")

        self._sections: list[Section] = []
        self._footprint: tuple[int, int] | None = None  # (start_word, end_word)

        self._build_layout()

        # If a path was passed on command line, prefill it
        if len(sys.argv) > 1:
            self._set_out_path(sys.argv[1])

    # ── Layout ──────────────────────────────────────────────────
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        self._build_header()
        self._build_files_card()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_left_panel(body)
        self._build_log_panel(body)

        self._build_status_bar()

    def _build_header(self):
        h = ctk.CTkFrame(self, height=56, corner_radius=0)
        h.grid(row=0, column=0, sticky="ew")
        h.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            h, text="  C2000 Binary Image Builder",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=12, sticky="w")
        ctk.CTkLabel(
            h, text="auto-fits the firmware footprint  •  no hand-edited hex_image.cmd",
            text_color=("#666", "#999"),
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=1, sticky="w", pady=(18, 0))

    def _build_files_card(self):
        c = ctk.CTkFrame(self, corner_radius=10)
        c.grid(row=1, column=0, sticky="ew", padx=16, pady=(8, 8))
        c.grid_columnconfigure(1, weight=1)

        rows = [
            ("Input  .out", self.out_path, self._browse_out),
            ("Map  .map",   self.map_path, self._browse_map),
            ("Output  .bin", self.bin_path, self._browse_bin),
            ("hex2000.exe", self.hex2000_path, self._browse_hex2000),
        ]
        for r, (label, var, browse) in enumerate(rows):
            ctk.CTkLabel(c, text=label, font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=r, column=0, padx=(16, 8), pady=(12 if r == 0 else 6, 6),
                sticky="w",
            )
            ctk.CTkEntry(c, textvariable=var, height=32).grid(
                row=r, column=1, sticky="ew",
                pady=(12 if r == 0 else 6, 6),
            )
            ctk.CTkButton(c, text="Browse...", width=90, height=32, command=browse).grid(
                row=r, column=2, padx=(8, 16),
                pady=(12 if r == 0 else 6, 6),
            )

        # Build row + options
        opts = ctk.CTkFrame(c, fg_color="transparent")
        opts.grid(row=4, column=0, columnspan=3, sticky="ew", padx=12, pady=(8, 14))
        opts.grid_columnconfigure(6, weight=1)

        ctk.CTkLabel(opts, text="Start (word)", font=ctk.CTkFont(size=11)).grid(
            row=0, column=0, padx=(4, 4)
        )
        ctk.CTkEntry(opts, textvariable=self.start_addr, width=110, height=30).grid(
            row=0, column=1
        )
        ctk.CTkLabel(opts, text="Headroom (bytes)", font=ctk.CTkFont(size=11)).grid(
            row=0, column=2, padx=(12, 4)
        )
        ctk.CTkEntry(opts, textvariable=self.headroom, width=80, height=30).grid(
            row=0, column=3
        )
        ctk.CTkLabel(opts, text="Fill (16-bit)", font=ctk.CTkFont(size=11)).grid(
            row=0, column=4, padx=(12, 4)
        )
        ctk.CTkEntry(opts, textvariable=self.fill_byte, width=90, height=30).grid(
            row=0, column=5
        )
        ctk.CTkCheckBox(
            opts, text="Full Bank 0 (240 KB, no maintenance ever)",
            variable=self.full_bank_mode, font=ctk.CTkFont(size=11),
            command=self._on_full_bank_toggle,
        ).grid(row=0, column=6, padx=(16, 4), sticky="w")

        # Action buttons
        btns = ctk.CTkFrame(c, fg_color="transparent")
        btns.grid(row=5, column=0, columnspan=3, sticky="e", padx=16, pady=(0, 12))
        ctk.CTkButton(
            btns, text="Re-parse .map", width=140, height=36,
            fg_color=("#357", "#357"), hover_color=("#468", "#468"),
            command=self._reparse,
        ).pack(side="left", padx=(0, 8))
        self.build_btn = ctk.CTkButton(
            btns, text="Build .bin", width=160, height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._build_bin,
        )
        self.build_btn.pack(side="left")

    def _build_left_panel(self, parent):
        f = ctk.CTkFrame(parent, corner_radius=10)
        f.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            f, text="DETECTED FLASH SECTIONS",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#357", "#7af"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))

        # Header row
        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.grid(row=1, column=0, sticky="ew", padx=12)
        for i, (label, w) in enumerate(
            [("Section", 130), ("Origin (word)", 110), ("Length (word)", 110), ("End (word)", 110)]
        ):
            ctk.CTkLabel(
                hdr, text=label, anchor="w",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=("#888", "#aaa"), width=w,
            ).grid(row=0, column=i, padx=2, sticky="w")

        # Scrollable section list
        self.sections_list = tk.Text(
            f, wrap="none", relief="flat", borderwidth=0,
            bg="#0e1116", fg="#d6dde6", height=10,
            font=("Consolas", 11),
        )
        self.sections_list.grid(row=2, column=0, sticky="nsew", padx=12, pady=(4, 8))
        self.sections_list.configure(state="disabled")

        # Footprint summary
        ctk.CTkLabel(
            f, text="FOOTPRINT",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#357", "#7af"),
        ).grid(row=3, column=0, sticky="w", padx=12, pady=(8, 6))

        self.footprint_text = tk.Text(
            f, wrap="word", relief="flat", borderwidth=0,
            bg="#0e1116", fg="#d6dde6", height=8,
            font=("Consolas", 11),
        )
        self.footprint_text.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 12))
        self.footprint_text.configure(state="disabled")

    def _build_log_panel(self, parent):
        f = ctk.CTkFrame(parent, corner_radius=10)
        f.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(f, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr, text="LOG",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#357", "#7af"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            hdr, text="Clear", width=70, height=26,
            fg_color=("#666", "#444"), hover_color=("#777", "#555"),
            command=self._clear_log,
        ).grid(row=0, column=1, sticky="e")

        self.log = tk.Text(
            f, wrap="word", relief="flat", borderwidth=0,
            bg="#0e1116", fg="#d6dde6",
            font=("Consolas", 10),
        )
        self.log.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        for tag, color in [
            ("ok", "#7adf7a"), ("err", "#ff6b6b"),
            ("warn", "#f1c40f"), ("info", "#69b1ff"),
            ("muted", "#7d8590"),
        ]:
            self.log.tag_config(tag, foreground=color)
        self.log.configure(state="disabled")

    def _build_status_bar(self):
        bar = ctk.CTkFrame(self, height=40, corner_radius=0)
        bar.grid(row=3, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            bar, textvariable=self.status,
            font=ctk.CTkFont(size=12),
        ).grid(row=0, column=0, padx=20, pady=10, sticky="w")

    # ── Browse handlers ─────────────────────────────────────────
    def _browse_out(self):
        p = filedialog.askopenfilename(
            title="Select .out file",
            filetypes=[("CCS executable", "*.out"), ("All files", "*.*")],
        )
        if p:
            self._set_out_path(p)

    def _set_out_path(self, p: str):
        path = Path(p)
        self.out_path.set(str(path))
        # Auto-fill .map and .bin paths next to it
        guess_map = path.with_suffix(".map")
        if guess_map.exists():
            self.map_path.set(str(guess_map))
        else:
            self.map_path.set("")
            self._log(f"warn: no .map next to {path.name}\n", "warn")
        self.bin_path.set(str(path.with_suffix(".bin")))
        self._reparse()

    def _browse_map(self):
        p = filedialog.askopenfilename(
            title="Select .map file",
            filetypes=[("CCS map file", "*.map"), ("All files", "*.*")],
        )
        if p:
            self.map_path.set(p)
            self._reparse()

    def _browse_bin(self):
        p = filedialog.asksaveasfilename(
            title="Save .bin as",
            defaultextension=".bin",
            filetypes=[("Binary image", "*.bin"), ("All files", "*.*")],
        )
        if p:
            self.bin_path.set(p)

    def _browse_hex2000(self):
        p = filedialog.askopenfilename(
            title="Locate hex2000.exe",
            filetypes=[("Executable", "hex2000.exe"), ("All files", "*.*")],
        )
        if p:
            self.hex2000_path.set(p)

    # ── Parse & display ─────────────────────────────────────────
    def _reparse(self):
        m = self.map_path.get().strip()
        if not m:
            return
        path = Path(m)
        if not path.exists():
            self._log(f"err: map not found: {path}\n", "err")
            self.status.set("Map file not found.")
            return
        try:
            self._sections = parse_map_sections(path)
        except Exception as e:
            self._log(f"err: parse failed: {e}\n", "err")
            self.status.set("Parse failed.")
            return

        if not self._sections:
            self._log("warn: no flash sections found in map\n", "warn")
            self.status.set("No flash sections found.")
            self._update_section_list([])
            self._footprint = None
            self._update_footprint_display(None)
            return

        # Sort by origin so the table reads top-to-bottom
        self._sections.sort(key=lambda s: s.origin)
        self._update_section_list(self._sections)

        try:
            start, end = compute_footprint(self._sections)
        except Exception as e:
            self._log(f"err: footprint calc failed: {e}\n", "err")
            self._footprint = None
            return

        self._footprint = (start, end)
        self._update_footprint_display(self._footprint)
        self.status.set(
            f"Parsed {len(self._sections)} sections — "
            f"footprint 0x{start:06X}..0x{end:06X} ({(end - start) * 2} bytes)."
        )

    def _update_section_list(self, sections: list[Section]):
        self.sections_list.configure(state="normal")
        self.sections_list.delete("1.0", "end")
        if not sections:
            self.sections_list.insert("end", "  (no flash sections)\n")
        else:
            for s in sections:
                self.sections_list.insert(
                    "end",
                    f"  {s.name:<14}  0x{s.origin:06X}     "
                    f"0x{s.length:06X}     0x{s.end:06X}\n",
                )
        self.sections_list.configure(state="disabled")

    def _update_footprint_display(self, fp: tuple[int, int] | None):
        self.footprint_text.configure(state="normal")
        self.footprint_text.delete("1.0", "end")
        if fp is None:
            self.footprint_text.insert("end", "  (no footprint)\n")
            self.footprint_text.configure(state="disabled")
            return

        start, end = fp
        words = end - start
        bytes_raw = words * 2

        try:
            head = max(0, int(self.headroom.get(), 0))
        except Exception:
            head = 0
        bytes_padded = round_up(bytes_raw + head, 0x100)

        full_bank_words = BANK0_END_WORD - start
        full_bank_bytes = full_bank_words * 2

        active_bytes = full_bank_bytes if self.full_bank_mode.get() else bytes_padded
        pct = active_bytes * 100.0 / full_bank_bytes if full_bank_bytes else 0

        lines = [
            f"  Start  (word)   :  0x{start:06X}    →  byte 0x{start*2:06X}",
            f"  End    (word)   :  0x{end:06X}    →  byte 0x{end*2:06X}",
            f"  Length (words)  :  0x{words:06X}    ({words:,} words)",
            f"  Length (bytes)  :  0x{bytes_raw:06X}    ({bytes_raw:,} bytes raw)",
            f"  + headroom      :  +{head} bytes  →  rounded up to 0x{bytes_padded:X}",
            "",
            f"  Will write       : 0x{active_bytes:X} bytes "
            f"({active_bytes:,} B,  {pct:.1f}% of Bank 0 app area)",
        ]
        if self.full_bank_mode.get():
            lines.append("  Mode             : FULL BANK (no maintenance)")
        else:
            lines.append("  Mode             : TIGHT (recompute when firmware grows)")

        self.footprint_text.insert("end", "\n".join(lines) + "\n")
        self.footprint_text.configure(state="disabled")

    def _on_full_bank_toggle(self):
        self._update_footprint_display(self._footprint)

    # ── Build .bin ─────────────────────────────────────────────
    def _build_bin(self):
        if self._footprint is None:
            messagebox.showerror("No footprint", "Parse a .map file first.")
            return
        out = Path(self.out_path.get().strip())
        if not out.exists():
            messagebox.showerror("Missing .out", f"File not found:\n{out}")
            return
        bin_p = Path(self.bin_path.get().strip())
        if not bin_p:
            messagebox.showerror("Missing output", "Set the output .bin path.")
            return
        h = Path(self.hex2000_path.get().strip())
        if not h.exists():
            messagebox.showerror("hex2000 not found", f"hex2000 not at:\n{h}")
            return

        # Decide start address
        if self.start_addr.get().strip().lower() == "auto":
            start, _ = self._footprint
        else:
            try:
                start = int(self.start_addr.get(), 0)
            except Exception:
                messagebox.showerror("Bad start", "Use 'auto' or 0xNNNNNN.")
                return

        # Decide length
        end = self._footprint[1]
        if self.full_bank_mode.get():
            length_bytes = (BANK0_END_WORD - start) * 2
        else:
            try:
                head = max(0, int(self.headroom.get(), 0))
            except Exception:
                head = 0
            length_bytes = round_up((end - start) * 2 + head, 0x100)

        try:
            fill = int(self.fill_byte.get(), 0) & 0xFFFF
        except Exception:
            fill = 0xFFFF

        # Generate .cmd, run hex2000 in a worker thread
        self.build_btn.configure(state="disabled", text="Building...")
        self.status.set("Running hex2000...")
        self._log(
            f"\n[BUILD] start=0x{start:06X}  length=0x{length_bytes:X}  "
            f"fill=0x{fill:04X}\n",
            "info",
        )

        def worker():
            # Tk widgets are not thread-safe — every call into the
            # GUI from this thread must be marshalled to the main
            # loop via self.after(0, ...).
            with tempfile.TemporaryDirectory() as td:
                cmd_file = Path(td) / "hex_image.cmd"
                cmd_file.write_text(build_hex_cmd(start, length_bytes, fill))
                self.after(0, lambda p=cmd_file: self._log(
                    f"[BUILD] wrote temp cmd file: {p}\n", "muted"))
                ok, log = run_hex2000(h, out, cmd_file, bin_p)
            self.after(0, lambda: self._build_done(ok, log, bin_p, length_bytes))

        threading.Thread(target=worker, daemon=True).start()

    def _build_done(self, ok: bool, log: str, bin_p: Path, expected: int):
        self.build_btn.configure(state="normal", text="Build .bin")
        if log:
            self._log(log if log.endswith("\n") else log + "\n", "muted")
        if ok:
            try:
                actual = bin_p.stat().st_size
            except Exception:
                actual = -1
            self._log(
                f"[OK] {bin_p}  ({actual:,} bytes  /  expected 0x{expected:X})\n",
                "ok",
            )
            self.status.set(f"Built {bin_p.name} — {actual:,} bytes.")
        else:
            self._log("[FAIL] hex2000 returned an error.\n", "err")
            self.status.set("Build failed — see log.")

    # ── Log helpers ─────────────────────────────────────────────
    def _log(self, text: str, tag: str = ""):
        self.log.configure(state="normal")
        if tag:
            self.log.insert("end", text, tag)
        else:
            self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
