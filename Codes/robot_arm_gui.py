import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import serial.tools.list_ports
import time
import threading
from datetime import datetime
import json
import os
import ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(1)

# ── Motor math ────────────────────────────────────────────────────────────────
STEPS_PER_REV    = 200
MICROSTEPPING    = 16
GEAR_RATIO       = 38.4
STEPS_PER_DEGREE = (STEPS_PER_REV * MICROSTEPPING * GEAR_RATIO) / 360.0
# ──────────────────────────────────────────────────────────────────────────────

JOINT_NAMES  = ["J1 – Base", "J2 – Shoulder", "J3 – Elbow",
                "J4 – Wrist Pitch", "J5 – Wrist Roll", "J6 – Gripper"]
JOINT_LIMITS = [(-180, 180), (-180, 180), (-180, 180),
                (-180, 180),   (-180, 180), (-180, 180)]

# Pin map (DIR, PUL) — matches .ino PINS array
JOINT_PINS = [
    (3,  2),   # J1 Base
    (5,  4),   # J2 Shoulder
    (7,  6),   # J3 Elbow
    (9,  8),   # J4 Wrist Pitch
    (11, 10),  # J5 Wrist Roll
    (13, 12),  # J6 Gripper
]

SPEED_SLOW = 3000
SPEED_FAST = 200

C = {
    "bg":        "#E5E7EB",
    "card":      "#F9FAFB",
    "border":    "#D1D5DB",
    "border2":   "#9CA3AF",
    "fg":        "#111827",
    "fg2":       "#4B5563",
    "fg3":       "#6B7280",
    "acc":       "#0284C7",
    "acc2":      "#D97706",
    "on_bg":     "#DCFCE7",
    "on_fg":     "#15803D",
    "off_bg":    "#FEE2E2",
    "off_fg":    "#B91C1C",
    "send_bg":   "#E0F2FE",
    "entry_bg":  "#FFFFFF",
    "log_bg":    "#FFFFFF",
    "stop_bg":   "#FEF3C7",
    "stop_fg":   "#92400E",
    "ustop_bg":  "#FEE2E2",
    "ustop_fg":  "#B91C1C",
    "rec_bg":    "#F3E8FF",
    "rec_fg":    "#7C3AED",
    "play_bg":   "#DCFCE7",
    "play_fg":   "#15803D",
}

FONT_TITLE  = ("Roboto", 20, "bold")
FONT_HEAD   = ("Roboto", 10, "bold")
FONT_BODY   = ("Roboto", 11)
FONT_SMALL  = ("Roboto", 9)
FONT_ANGLE  = ("Roboto", 13, "bold")
FONT_LOG    = ("Roboto", 9)


class RobotArmGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Robot Arm Controller")
        self.root.configure(bg=C["bg"])
        self.root.resizable(False, False)
        self.conn_popup = None

        self.serial_port = None

        self.angles       = [tk.DoubleVar(value=0.0)   for _ in range(6)]
        self.speed_vars   = [tk.IntVar(value=800)       for _ in range(6)]
        self.enabled_vars = [tk.BooleanVar(value=True)  for _ in range(6)]
        self.master_speed = tk.IntVar(value=800)

        self.motor_steps  = [0] * 6
        self.home_steps   = [0] * 6
        self._serial_lock = threading.Lock()
        self._stop_flags  = [False] * 6   # per-joint stop
        self._stop_all    = False          # universal stop

        # Sequence recording / playback
        self.sequence         = []           # list of step dicts
        self.seq_file_path    = tk.StringVar(value="")
        self._playback_thread = None

        self.joint_entries    = []
        self.joint_sliders    = []
        self.speed_labels     = []
        self.stop_btns        = []

        self._build_ui()

        self.root.bind("<Button-1>", self._defocus)
        self.root.bind("<Escape>",   lambda e: self.root.focus_set())

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=C["bg"])
        hdr.pack(fill="x", padx=20, pady=(16, 6))

        tk.Label(hdr, text="6-DOF ARM", font=FONT_TITLE,
                 fg=C["acc"], bg=C["bg"]).pack(side="left")
        tk.Label(hdr, text="stepper controller", font=FONT_SMALL,
                 fg=C["fg3"], bg=C["bg"]).pack(side="left", padx=(10, 0), pady=(8, 0))

        # Connection info button — top right
        tk.Button(hdr, text="CONNECTIONS", font=FONT_SMALL,
                  bg=C["border"], fg=C["fg2"], relief="flat", bd=0,
                  padx=10, pady=4, cursor="hand2",
                  activebackground=C["border2"],
                  command=self._show_connections).pack(side="right")

        # ── Serial bar ────────────────────────────────────────────────────────
        self._section(self.root)
        sbar = self._card(self.root)

        tk.Label(sbar, text="PORT", font=FONT_SMALL,
                 fg=C["fg2"], bg=C["card"]).pack(side="left", padx=(14, 6), pady=8)

        self.port_var = tk.StringVar()
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TCombobox",
                        fieldbackground=C["entry_bg"],
                        background=C["card"],
                        foreground=C["fg"],
                        arrowcolor=C["fg2"],
                        bordercolor=C["border2"],
                        lightcolor=C["border"],
                        darkcolor=C["border"])
        self.port_cb = ttk.Combobox(sbar, textvariable=self.port_var,
                                    width=16, font=FONT_SMALL,
                                    state="readonly", style="Dark.TCombobox")
        self.port_cb.pack(side="left", pady=8)
        self._refresh_ports()

        tk.Button(sbar, text="↻", font=FONT_SMALL, bg=C["card"], fg=C["fg2"],
                  relief="flat", bd=0, cursor="hand2", activebackground=C["border"],
                  command=self._refresh_ports).pack(side="left", padx=4)

        self.conn_btn = tk.Button(sbar, text="CONNECT", font=FONT_HEAD,
                                  bg=C["border"], fg=C["acc"], relief="flat",
                                  bd=0, padx=14, cursor="hand2",
                                  activebackground=C["border2"],
                                  command=self._toggle_connect)
        self.conn_btn.pack(side="left", padx=10)

        self.status_dot = tk.Label(sbar, text="●", font=("Courier New", 14),
                                   fg=C["off_fg"], bg=C["card"])
        self.status_dot.pack(side="left")
        self.status_lbl = tk.Label(sbar, text="disconnected",
                                   font=FONT_SMALL, fg=C["fg2"], bg=C["card"])
        self.status_lbl.pack(side="left", padx=6)

        # ── Master speed ──────────────────────────────────────────────────────
        self._section(self.root)
        ms = self._card(self.root)

        tk.Label(ms, text="MASTER SPEED", font=FONT_HEAD,
                 fg=C["acc2"], bg=C["card"]).pack(side="left", padx=(14, 10), pady=8)
        tk.Label(ms, text="slow", font=FONT_SMALL,
                 fg=C["fg3"], bg=C["card"]).pack(side="left")

        self.master_spd_lbl = tk.Label(ms, text=self._speed_label(800),
                                       font=FONT_SMALL, fg=C["fg"],
                                       bg=C["card"], width=10)

        tk.Scale(ms, from_=SPEED_SLOW, to=SPEED_FAST,
                 orient="horizontal", variable=self.master_speed,
                 resolution=50, showvalue=False, length=340,
                 bg=C["card"], fg=C["fg"], troughcolor=C["border"],
                 highlightthickness=0, sliderrelief="flat",
                 activebackground=C["acc2"],
                 command=self._on_master_speed).pack(side="left", padx=6, pady=6)

        tk.Label(ms, text="fast", font=FONT_SMALL,
                 fg=C["fg3"], bg=C["card"]).pack(side="left")
        self.master_spd_lbl.pack(side="left", padx=(10, 0))

        # ── Joints ────────────────────────────────────────────────────────────
        self._section(self.root)

        for i, (name, (lo, hi)) in enumerate(zip(JOINT_NAMES, JOINT_LIMITS)):
            card = tk.Frame(self.root, bg=C["card"],
                            highlightbackground=C["border"],
                            highlightthickness=1)
            card.pack(fill="x", padx=20, pady=3)

            # row 0 ── name | range | enable | angle °
            tk.Label(card, text=name, font=FONT_HEAD, fg=C["acc"],
                     bg=C["card"], width=20, anchor="w"
                     ).grid(row=0, column=0, padx=(14, 6), pady=(10, 2), sticky="w")

            tk.Label(card, text=f"{lo}° → {hi}°", font=FONT_SMALL,
                     fg=C["fg3"], bg=C["card"]
                     ).grid(row=0, column=1, sticky="w")

            stop_btn = tk.Button(card, text="STOP", font=FONT_SMALL,
                                bg=C["stop_bg"], fg=C["stop_fg"],
                                relief="flat", bd=0, padx=10, cursor="hand2",
                                activebackground=C["border"],
                                command=lambda idx=i: self._stop_joint(idx))
            stop_btn.grid(row=0, column=2, padx=12)
            self.stop_btns.append(stop_btn)

            # angle display
            ang_frame = tk.Frame(card, bg=C["card"])
            ang_frame.grid(row=0, column=7, padx=(0, 14), sticky="e")
            tk.Label(ang_frame, textvariable=self.angles[i],
                     font=FONT_ANGLE, fg=C["fg"],
                     bg=C["card"], width=7, anchor="e").pack(side="left")
            tk.Label(ang_frame, text="°", font=FONT_HEAD,
                     fg=C["fg2"], bg=C["card"]).pack(side="left")

            # row 1 ── slider (full range) | entry | SEND
            sl = tk.Scale(card, from_=lo, to=hi, orient="horizontal",
                          variable=self.angles[i], resolution=1,
                          showvalue=False, length=400,
                          bg=C["card"], fg=C["fg"], troughcolor=C["border"],
                          highlightthickness=0, sliderrelief="flat",
                          activebackground=C["acc"],
                          command=lambda v, idx=i: self._on_slider(idx, v))
            sl.grid(row=1, column=0, columnspan=4, padx=14, pady=(0, 4), sticky="ew")
            self.joint_sliders.append(sl)

            bf = tk.Frame(card, bg=C["card"])
            bf.grid(row=1, column=4, columnspan=4, padx=(0, 14), pady=(0, 4), sticky="e")

            entry = tk.Entry(bf, font=FONT_BODY, bg=C["entry_bg"], fg=C["acc"],
                             insertbackground=C["acc"], relief="flat", bd=0,
                             width=6, justify="center",
                             highlightthickness=1,
                             highlightcolor=C["acc"],
                             highlightbackground=C["border"])
            entry.insert(0, "0")
            entry.pack(side="left", padx=6)
            entry.bind("<Return>",   lambda e, idx=i: self._on_entry(idx))
            entry.bind("<Escape>",   lambda e, idx=i: self._cancel_entry(idx, e))
            entry.bind("<FocusOut>", lambda e, idx=i: self._cancel_entry(idx, e))
            self.joint_entries.append(entry)

            tk.Label(bf, text="°", font=FONT_SMALL,
                     fg=C["fg2"], bg=C["card"]).pack(side="left")

            tk.Button(bf, text="SEND", font=FONT_SMALL, bg=C["send_bg"],
                      fg=C["acc"], relief="flat", bd=0, padx=12, cursor="hand2",
                      activebackground=C["border2"],
                      command=lambda idx=i: self._send_joint(idx)
                      ).pack(side="left", padx=(10, 0))

            # row 2 ── per-joint speed
            sr = tk.Frame(card, bg=C["card"])
            sr.grid(row=2, column=0, columnspan=8, padx=14, pady=(0, 8), sticky="w")

            tk.Label(sr, text="speed", font=FONT_SMALL,
                     fg=C["fg3"], bg=C["card"]).pack(side="left")
            tk.Label(sr, text="slow", font=FONT_SMALL,
                     fg=C["fg3"], bg=C["card"]).pack(side="left", padx=(8, 2))

            tk.Scale(sr, from_=SPEED_SLOW, to=SPEED_FAST,
                     orient="horizontal", variable=self.speed_vars[i],
                     resolution=50, showvalue=False, length=260,
                     bg=C["card"], fg=C["fg"], troughcolor=C["border"],
                     highlightthickness=0, sliderrelief="flat",
                     activebackground=C["acc"],
                     command=lambda v, idx=i: self._on_joint_speed(idx, v)
                     ).pack(side="left", padx=2)

            tk.Label(sr, text="fast", font=FONT_SMALL,
                     fg=C["fg3"], bg=C["card"]).pack(side="left", padx=(2, 8))

            spd_lbl = tk.Label(sr, text=self._speed_label(800),
                               font=FONT_SMALL, fg=C["fg"], bg=C["card"], width=10)
            spd_lbl.pack(side="left")
            self.speed_labels.append(spd_lbl)

        # ── Footer ────────────────────────────────────────────────────────────
        self._section(self.root)
        ftr = tk.Frame(self.root, bg=C["bg"])
        ftr.pack(fill="x", padx=20, pady=(6, 8))

        btn_cfg = dict(font=FONT_HEAD, relief="flat", bd=0,
                       padx=16, pady=7, cursor="hand2")

        tk.Button(ftr, text="SET HOME", bg=C["border"], fg=C["fg"],
                  activebackground=C["border2"],
                  command=self._set_home, **btn_cfg
                  ).pack(side="left", padx=(0, 8))

        tk.Button(ftr, text="GO HOME", bg=C["border"], fg=C["acc2"],
                  activebackground=C["border2"],
                  command=self._go_home, **btn_cfg
                  ).pack(side="left", padx=(0, 8))

        tk.Button(ftr, text="SEND ALL", bg=C["send_bg"], fg=C["acc"],
                  activebackground=C["border2"],
                  command=self._send_all, **btn_cfg
                  ).pack(side="left", padx=(0, 8))

        tk.Button(ftr, text="⬛ STOP ALL", bg=C["ustop_bg"], fg=C["ustop_fg"],
                  activebackground=C["border2"],
                  command=self._stop_all_joints, **btn_cfg
                  ).pack(side="left")

        tk.Label(ftr, text=f"{STEPS_PER_DEGREE:.2f} steps/°",
                 font=FONT_SMALL, fg=C["fg3"], bg=C["bg"]).pack(side="right")

        # ── Sequence panel ────────────────────────────────────────────────────
        self._section(self.root)
        seq_outer = tk.Frame(self.root, bg=C["bg"])
        seq_outer.pack(fill="x", padx=20, pady=(2, 8))

        seq_hdr = tk.Frame(seq_outer, bg=C["bg"])
        seq_hdr.pack(fill="x")
        tk.Label(seq_hdr, text="SEQUENCE", font=FONT_HEAD,
                 fg=C["fg3"], bg=C["bg"]).pack(side="left")

        seq_btn_row = tk.Frame(seq_outer, bg=C["bg"])
        seq_btn_row.pack(fill="x", pady=(4, 0))

        sbtn = dict(font=FONT_SMALL, relief="flat", bd=0,
                    padx=12, pady=5, cursor="hand2")

        tk.Button(seq_btn_row, text="＋ ADD STEP", bg=C["rec_bg"], fg=C["rec_fg"],
                  activebackground=C["border"], command=self._seq_add_step, **sbtn
                  ).pack(side="left", padx=(0, 6))

        tk.Button(seq_btn_row, text="EXPORT FILE", bg=C["border"], fg=C["fg"],
                  activebackground=C["border2"], command=self._seq_export, **sbtn
                  ).pack(side="left", padx=(0, 6))

        tk.Button(seq_btn_row, text="IMPORT FILE", bg=C["border"], fg=C["fg"],
                  activebackground=C["border2"], command=self._seq_import, **sbtn
                  ).pack(side="left", padx=(0, 6))

        tk.Button(seq_btn_row, text="▶ START", bg=C["play_bg"], fg=C["play_fg"],
                  activebackground=C["border2"], command=self._seq_start, **sbtn
                  ).pack(side="left", padx=(0, 6))

        tk.Button(seq_btn_row, text="CLEAR", bg=C["bg"], fg=C["fg3"],
                  activebackground=C["border"], command=self._seq_clear, **sbtn
                  ).pack(side="left", padx=(0, 12))

        # File path chooser
        path_frame = tk.Frame(seq_btn_row, bg=C["bg"])
        path_frame.pack(side="left", fill="x", expand=True)
        tk.Label(path_frame, text="file:", font=FONT_SMALL,
                 fg=C["fg3"], bg=C["bg"]).pack(side="left")
        self.seq_path_entry = tk.Entry(path_frame, textvariable=self.seq_file_path,
                                       font=FONT_SMALL, bg=C["entry_bg"], fg=C["fg"],
                                       relief="flat", bd=0, width=32,
                                       highlightthickness=1,
                                       highlightbackground=C["border"],
                                       highlightcolor=C["acc"])
        self.seq_path_entry.pack(side="left", padx=(4, 4))
        tk.Button(path_frame, text="…", font=FONT_SMALL,
                  bg=C["border"], fg=C["fg"], relief="flat", bd=0,
                  padx=8, cursor="hand2", activebackground=C["border2"],
                  command=self._seq_choose_path).pack(side="left")

        # Step count label
        self.seq_count_lbl = tk.Label(seq_outer, text="0 steps recorded",
                                      font=FONT_SMALL, fg=C["fg3"], bg=C["bg"])
        self.seq_count_lbl.pack(anchor="w", pady=(4, 0))

        # ── Log panel ─────────────────────────────────────────────────────────
        self._section(self.root)
        log_frame = tk.Frame(self.root, bg=C["bg"])
        log_frame.pack(fill="x", padx=20, pady=(2, 14))

        log_header = tk.Frame(log_frame, bg=C["bg"])
        log_header.pack(fill="x")
        tk.Label(log_header, text="LOG", font=FONT_HEAD,
                 fg=C["fg3"], bg=C["bg"]).pack(side="left")
        tk.Button(log_header, text="CLEAR", font=FONT_SMALL,
                  bg=C["bg"], fg=C["fg3"], relief="flat", bd=0,
                  cursor="hand2", activebackground=C["border"],
                  command=self._clear_log).pack(side="right")

        log_box_frame = tk.Frame(log_frame, bg=C["border"], bd=1)
        log_box_frame.pack(fill="x", pady=(4, 0))

        self.log_text = tk.Text(log_box_frame, height=5, bg=C["log_bg"],
                                fg=C["fg2"], font=FONT_LOG,
                                relief="flat", bd=0, state="disabled",
                                wrap="word", insertbackground=C["acc"])
        scrollbar = tk.Scrollbar(log_box_frame, orient="vertical",
                                 command=self.log_text.yview,
                                 bg=C["border"], troughcolor=C["bg"])
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(fill="x", padx=6, pady=4)

        # Tag colors for log levels
        self.log_text.tag_configure("info",    foreground=C["fg2"])
        self.log_text.tag_configure("ok",      foreground=C["on_fg"])
        self.log_text.tag_configure("warn",    foreground=C["acc2"])
        self.log_text.tag_configure("error",   foreground=C["off_fg"])
        self.log_text.tag_configure("move",    foreground=C["acc"])

        self._log("System ready.", "info")

    # ── Log helpers ───────────────────────────────────────────────────────────
    def _log(self, msg, level="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.config(state="normal")
        self.log_text.insert("end", line, level)
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    # ── Connection info popup ─────────────────────────────────────────────────
    def _show_connections(self):
        # If already open → just bring it to front
        if self.conn_popup and self.conn_popup.winfo_exists():
            self.conn_popup.lift()
            return
        self.conn_popup = tk.Toplevel(self.root)
        popup = self.conn_popup
        popup.title("Pin Connections")
        popup.configure(bg=C["bg"])
        popup.resizable(False, False)
        # popup.grab_set()
        popup.attributes("-topmost", True)
        
        popup.update_idletasks()

        # main window position
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_w = self.root.winfo_width()

        # popup size
        popup_w = popup.winfo_width()
        popup_h = popup.winfo_height()

        # place popup to the RIGHT of main window
        x = root_x + root_w + 10
        y = root_y

        popup.geometry(f"+{x}+{y}")

        tk.Label(popup, text="ARDUINO MEGA PIN MAP", font=FONT_HEAD,
                 fg=C["acc"], bg=C["bg"]).pack(pady=(16, 8), padx=20)

        table = tk.Frame(popup, bg=C["card"],
                         highlightbackground=C["border"], highlightthickness=1)
        table.pack(padx=20, pady=(0, 8))

        headers = ["Joint", "Name", "DIR pin", "PUL pin"]
        col_widths = [6, 16, 9, 9]
        for c, (h, w) in enumerate(zip(headers, col_widths)):
            tk.Label(table, text=h, font=FONT_HEAD, fg=C["fg3"],
                     bg=C["card"], width=w, anchor="w"
                     ).grid(row=0, column=c, padx=8, pady=(10, 4), sticky="w")

        tk.Frame(table, bg=C["border"], height=1).grid(
            row=1, column=0, columnspan=4, sticky="ew", padx=8)

        short_names = ["Base", "Shoulder", "Elbow", "Wrist Pitch", "Wrist Roll", "Gripper"]
        for i, (name, (dir_pin, pul_pin)) in enumerate(zip(short_names, JOINT_PINS)):
            row_bg = C["card"] if i % 2 == 0 else C["bg"]
            tk.Label(table, text=f"J{i+1}", font=FONT_BODY,
                     fg=C["acc"], bg=row_bg, width=col_widths[0], anchor="w"
                     ).grid(row=i+2, column=0, padx=8, pady=3, sticky="w")
            tk.Label(table, text=name, font=FONT_BODY,
                     fg=C["fg"], bg=row_bg, width=col_widths[1], anchor="w"
                     ).grid(row=i+2, column=1, padx=8, pady=3, sticky="w")
            tk.Label(table, text=f"{dir_pin}", font=FONT_BODY,
                     fg=C["acc2"], bg=row_bg, width=col_widths[2], anchor="w"
                     ).grid(row=i+2, column=2, padx=8, pady=3, sticky="w")
            tk.Label(table, text=f"{pul_pin}", font=FONT_BODY,
                     fg=C["on_fg"], bg=row_bg, width=col_widths[3], anchor="w"
                     ).grid(row=i+2, column=3, padx=8, pady=3, sticky="w")

        tk.Label(popup, text="All DIR pins = amber  |  All PUL pins = green",
                 font=FONT_SMALL, fg=C["fg3"], bg=C["bg"]).pack(pady=(0, 4))
        tk.Label(popup, text="Baud rate: 115200", font=FONT_SMALL,
                 fg=C["fg3"], bg=C["bg"]).pack()
        tk.Button(popup, text="CLOSE", font=FONT_HEAD,
                  bg=C["border"], fg=C["fg"], relief="flat", bd=0,
                  padx=20, pady=6, cursor="hand2",
                  activebackground=C["border2"],
                  command=popup.destroy).pack(pady=14)

    # ── Layout helpers ────────────────────────────────────────────────────────
    def _card(self, parent):
        f = tk.Frame(parent, bg=C["card"], highlightbackground=C["border"],
                     highlightthickness=1)
        f.pack(fill="x", padx=20, pady=2)
        return f

    def _section(self, parent):
        tk.Frame(parent, bg=C["border"], height=1).pack(fill="x", padx=20, pady=4)

    # ── Speed helpers ─────────────────────────────────────────────────────────
    def _speed_label(self, us):
        us = int(us)
        if us >= 2500:   return "very slow"
        elif us >= 1800: return "slow"
        elif us >= 1000: return "medium"
        elif us >= 500:  return "fast"
        else:            return "very fast"

    def _on_master_speed(self, val):
        v = int(float(val))
        self.master_spd_lbl.config(text=self._speed_label(v))
        for i in range(6):
            self.speed_vars[i].set(v)
            self.speed_labels[i].config(text=self._speed_label(v))

    def _on_joint_speed(self, idx, val):
        self.speed_labels[idx].config(text=self._speed_label(int(float(val))))

    # ── Per-joint stop ────────────────────────────────────────────────────────
    def _stop_joint(self, idx):
        self._stop_flags[idx] = True
        self._log(f"STOP J{idx+1} {JOINT_NAMES[idx]}", "warn")
        # Send S command to Arduino
        if self.serial_port and self.serial_port.is_open:
            try:
                self.serial_port.write(f"S{idx}\n".encode())
            except Exception:
                pass

    def _stop_all_joints(self):
        self._stop_all = True
        for i in range(6):
            self._stop_flags[i] = True
        self._log("STOP ALL — all joints halted.", "error")
        if self.serial_port and self.serial_port.is_open:
            try:
                self.serial_port.write(b"X\n")
            except Exception:
                pass

    def _reset_stop_flags(self):
        self._stop_all = False
        for i in range(6):
            self._stop_flags[i] = False

    # ── Entry focus helpers ───────────────────────────────────────────────────
    def _defocus(self, event):
        widget = event.widget
        if not isinstance(widget, tk.Entry):
            self.root.focus_set()

    def _cancel_entry(self, idx, event=None):
        self.joint_entries[idx].delete(0, "end")
        self.joint_entries[idx].insert(0, str(int(self.angles[idx].get())))
        self.root.focus_set()

    # ── Slider / entry sync (UI only, no send) ────────────────────────────────
    def _on_slider(self, idx, val):
        """Slider moved → update entry box."""
        v = int(float(val))
        self.joint_entries[idx].delete(0, "end")
        self.joint_entries[idx].insert(0, str(v))

    def _on_entry(self, idx):
        """Entry confirmed (Enter key) → clamp, update angle + slider."""
        try:
            raw = float(self.joint_entries[idx].get())
            val = self._clamp(idx, raw)
            self.angles[idx].set(val)
            self.joint_entries[idx].delete(0, "end")
            self.joint_entries[idx].insert(0, str(int(val)))
            # Slider follows automatically via shared DoubleVar
        except ValueError:
            pass
        self.root.focus_set()

    def _clamp(self, idx, val):
        lo, hi = JOINT_LIMITS[idx]
        return max(lo, min(hi, val))

    # ── Absolute positioning logic ────────────────────────────────────────────
    def _send_joint(self, idx):
        """Send single joint J command."""
        if not self.serial_port or not self.serial_port.is_open:
            self._log("Not connected.", "error")
            return
        target_steps = self.home_steps[idx] + int(round(
            self.angles[idx].get() * STEPS_PER_DEGREE))
        delta_steps = target_steps - self.motor_steps[idx]
        if delta_steps == 0:
            self._log(f"J{idx+1} already at target — no move.", "info")
            return
        speed = self.speed_vars[idx].get()
        self.motor_steps[idx] = target_steps
        ang = self.angles[idx].get()
        self._stop_flags[idx] = False  # clear any previous stop
        self._log(f"Moving J{idx+1} {JOINT_NAMES[idx]} → {ang:.0f}°  ({delta_steps:+d} steps)", "move")
        threading.Thread(target=self._write_single,
                         args=(idx, delta_steps, speed),
                         daemon=True).start()

    def _send_all(self):
        """Send M command — all joints move simultaneously on Arduino."""
        if not self.serial_port or not self.serial_port.is_open:
            self._log("Not connected — cannot send all.", "error")
            return

        self._reset_stop_flags()
        deltas = []
        any_move = False
        for i in range(6):
            target_steps = self.home_steps[i] + int(round(
                self.angles[i].get() * STEPS_PER_DEGREE))
            delta = target_steps - self.motor_steps[i]
            deltas.append(delta)
            if delta != 0:
                any_move = True
            self.motor_steps[i] = target_steps

        if not any_move:
            self._log("SEND ALL — all joints already at target, nothing to move.", "info")
            return

        speed = self.master_speed.get()
        angles_str = ", ".join(f"J{i+1}:{self.angles[i].get():.0f}°" for i in range(6))
        self._log(f"SEND ALL → simultaneous move | {angles_str} | spd={speed}µs", "move")
        threading.Thread(target=self._write_multi,
                         args=(deltas, speed),
                         daemon=True).start()

    # ── SET HOME ──────────────────────────────────────────────────────────────
    def _set_home(self):
        for i in range(6):
            self.home_steps[i] = self.motor_steps[i]
            self.angles[i].set(0)
            self.joint_entries[i].delete(0, "end")
            self.joint_entries[i].insert(0, "0")
        self._log("Home position set — all joints zeroed.", "ok")

    # ── GO HOME ───────────────────────────────────────────────────────────────
    def _go_home(self):
        if not self.serial_port or not self.serial_port.is_open:
            self._log("Not connected — cannot go home.", "error")
            return
        for i in range(6):
            self.angles[i].set(0)
            self.joint_entries[i].delete(0, "end")
            self.joint_entries[i].insert(0, "0")
        self._log("Going home — all joints → 0°", "warn")
        self._send_all()

    # ── Serial write helpers ──────────────────────────────────────────────────
    def _write_single(self, idx, delta_steps, speed):
        """Send J command and wait for OK. Aborts if stop flag is set."""
        if self._stop_flags[idx] or self._stop_all:
            self.root.after(0, lambda: self._log(f"J{idx+1} stopped before send.", "warn"))
            return
        cmd = f"J{idx},{delta_steps},{speed}\n"
        if self.serial_port and self.serial_port.is_open:
            with self._serial_lock:
                try:
                    self.serial_port.write(cmd.encode())
                    deadline = time.time() + 120
                    while time.time() < deadline:
                        if self._stop_flags[idx] or self._stop_all:
                            self.root.after(0, lambda: self._log(f"J{idx+1} stop flag — aborting wait.", "warn"))
                            break
                        line = self.serial_port.readline().decode(errors="ignore").strip()
                        if line.startswith("OK"):
                            self.root.after(0, lambda l=line: self._log(f"Arduino: {l}", "ok"))
                            break
                except Exception as e:
                    self.root.after(0, lambda err=str(e): self._log(f"Serial error: {err}", "error"))

    def _write_multi(self, deltas, speed):
        """Send M command for simultaneous multi-joint move. Aborts if stop_all is set."""
        if self._stop_all:
            self.root.after(0, lambda: self._log("STOP ALL — cancelled before send.", "warn"))
            return
        steps_str = ",".join(str(d) for d in deltas)
        cmd = f"M{steps_str},{speed}\n"
        if self.serial_port and self.serial_port.is_open:
            with self._serial_lock:
                try:
                    self.serial_port.write(cmd.encode())
                    deadline = time.time() + 120
                    while time.time() < deadline:
                        if self._stop_all:
                            self.root.after(0, lambda: self._log("STOP ALL — aborting wait.", "warn"))
                            break
                        line = self.serial_port.readline().decode(errors="ignore").strip()
                        if line.startswith("OK"):
                            self.root.after(0, lambda l=line: self._log(f"Arduino: {l}", "ok"))
                            break
                except Exception as e:
                    self.root.after(0, lambda err=str(e): self._log(f"Serial error: {err}", "error"))

    # ── Sequence helpers ──────────────────────────────────────────────────────
    def _seq_update_label(self):
        n = len(self.sequence)
        self.seq_count_lbl.config(text=f"{n} step{'s' if n != 1 else ''} recorded")

    def _seq_add_step(self):
        """Snapshot current angles + per-joint speeds into sequence."""
        step = {
            "angles": [self.angles[i].get() for i in range(6)],
            "speeds": [self.speed_vars[i].get() for i in range(6)],
        }
        self.sequence.append(step)
        n = len(self.sequence)
        angles_str = ", ".join(f"J{i+1}:{step['angles'][i]:.0f}°" for i in range(6))
        self._log(f"Step {n} added — {angles_str}", "info")
        self._seq_update_label()

    def _seq_clear(self):
        self.sequence.clear()
        self._seq_update_label()
        self._log("Sequence cleared.", "warn")

    def _seq_choose_path(self):
        """Open save-as dialog to pick where to export/import the sequence file."""
        path = filedialog.asksaveasfilename(
            title="Choose sequence file location",
            defaultextension=".json",
            filetypes=[("JSON sequence", "*.json"), ("All files", "*.*")],
            initialfile="arm_sequence.json",
        )
        if path:
            self.seq_file_path.set(path)

    def _seq_export(self):
        """Save current sequence to JSON file (prompts for path if not set)."""
        path = self.seq_file_path.get().strip()
        if not path:
            path = filedialog.asksaveasfilename(
                title="Export sequence",
                defaultextension=".json",
                filetypes=[("JSON sequence", "*.json"), ("All files", "*.*")],
                initialfile="arm_sequence.json",
            )
            if not path:
                return
            self.seq_file_path.set(path)
        if not self.sequence:
            messagebox.showwarning("Empty", "No steps to export.")
            return
        try:
            with open(path, "w") as f:
                json.dump({"sequence": self.sequence}, f, indent=2)
            self._log(f"Exported {len(self.sequence)} steps → {path}", "ok")
        except Exception as e:
            messagebox.showerror("Export error", str(e))
            self._log(f"Export error: {e}", "error")

    def _seq_import(self):
        """Load a sequence JSON file."""
        path = filedialog.askopenfilename(
            title="Import sequence",
            filetypes=[("JSON sequence", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self.sequence = data.get("sequence", [])
            self.seq_file_path.set(path)
            self._seq_update_label()
            self._log(f"Imported {len(self.sequence)} steps from {os.path.basename(path)}", "ok")
        except Exception as e:
            messagebox.showerror("Import error", str(e))
            self._log(f"Import error: {e}", "error")

    def _seq_start(self):
        """Play back the recorded sequence, step by step."""
        if not self.sequence:
            messagebox.showwarning("Empty", "No steps in sequence. Add or import steps first.")
            return
        if not self.serial_port or not self.serial_port.is_open:
            messagebox.showerror("Not connected", "Connect to Arduino first.")
            return
        if self._playback_thread and self._playback_thread.is_alive():
            messagebox.showinfo("Running", "Playback already in progress.")
            return
        self._playback_thread = threading.Thread(target=self._seq_run, daemon=True)
        self._playback_thread.start()

    def _seq_run(self):
        """Background thread: execute each sequence step."""
        self._reset_stop_flags()
        total = len(self.sequence)
        self.root.after(0, lambda: self._log(f"Sequence START — {total} steps.", "ok"))
        for step_num, step in enumerate(self.sequence, 1):
            if self._stop_all:
                self.root.after(0, lambda: self._log("Sequence aborted by STOP ALL.", "error"))
                return
            angles = step["angles"]
            speeds = step["speeds"]
            # Update UI to reflect the target angles
            def _apply_ui(a=angles, sp=speeds):
                for i in range(6):
                    self.angles[i].set(a[i])
                    self.joint_entries[i].delete(0, "end")
                    self.joint_entries[i].insert(0, str(int(a[i])))
                    self.speed_vars[i].set(sp[i])
                    self.speed_labels[i].config(text=self._speed_label(sp[i]))
            self.root.after(0, _apply_ui)

            # Compute deltas and send M command
            deltas = []
            any_move = False
            speed = min(speeds)  # use the slowest requested speed for M command
            for i in range(6):
                target = self.home_steps[i] + int(round(angles[i] * STEPS_PER_DEGREE))
                delta = target - self.motor_steps[i]
                deltas.append(delta)
                if delta != 0:
                    any_move = True
                self.motor_steps[i] = target

            sn = step_num
            self.root.after(0, lambda n=sn, t=total: self._log(f"Step {n}/{t}", "move"))
            if any_move:
                self._write_multi(deltas, speed)
            else:
                self.root.after(0, lambda: self._log("  (no movement — already at target)", "info"))

        self.root.after(0, lambda: self._log("Sequence DONE.", "ok"))

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_cb["values"] = ports
        if ports:
            self.port_var.set(ports[0])

    def _toggle_connect(self):
        if self.serial_port and self.serial_port.is_open:
            self.serial_port.close()
            self.serial_port = None
            self._set_status(False)
            self.conn_btn.config(text="CONNECT")
            self._log("Disconnected from serial port.", "warn")
        else:
            port = self.port_var.get()
            if not port:
                messagebox.showerror("Error", "No port selected.")
                self._log("Connection failed — no port selected.", "error")
                return
            try:
                self.serial_port = serial.Serial(port, 115200, timeout=120)
                self._set_status(True)
                self.conn_btn.config(text="DISCONNECT")
                self._log(f"Connected to {port} @ 115200 baud.", "ok")
            except Exception as e:
                messagebox.showerror("Connection error", str(e))
                self._log(f"Connection error: {e}", "error")

    def _set_status(self, connected):
        if connected:
            self.status_dot.config(fg=C["acc"])
            self.status_lbl.config(text="connected")
        else:
            self.status_dot.config(fg=C["off_fg"])
            self.status_lbl.config(text="disconnected")


if __name__ == "__main__":
    root = tk.Tk()
    app  = RobotArmGUI(root)
    root.mainloop()
