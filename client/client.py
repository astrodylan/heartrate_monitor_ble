import sys
import os
import csv
import json
import time
import asyncio
import threading
from pathlib import Path
from datetime import datetime, timezone
from collections import deque
import requests

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QLineEdit, QFormLayout,
    QColorDialog, QComboBox, QGroupBox, QCheckBox, QSpinBox, QMessageBox
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QFont, QColor
import pyqtgraph as pg
from bleak import BleakClient, BleakScanner

HR_MEASUREMENT_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

APP_DIR = Path.home() / ".coospo_h6_monitor"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = str(APP_DIR / "config.json")
RECORDINGS_DIR = APP_DIR / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


class TimeAxisItem(pg.AxisItem):
    """Formats Unix timestamps as either relative seconds (-Xs) or HH:MM:SS (Local/UTC)."""
    def __init__(self, get_mode_fn, get_tz_fn, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_mode = get_mode_fn
        self.get_tz = get_tz_fn

    def tickStrings(self, values, scale, spacing):
        mode = self.get_mode()
        tz_mode = self.get_tz()
        now = time.time()
        strings = []
        for v in values:
            try:
                # Ignore empty plot default ticks outside valid Unix timestamp range
                if v < 946684800 or v > 4102444800:
                    strings.append("")
                    continue

                if mode == "relative":
                    diff = int(round(v - now))
                    strings.append(f"{diff}s" if diff != 0 else "Now")
                else:
                    if tz_mode == "utc":
                        dt = datetime.fromtimestamp(v, tz=timezone.utc)
                    else:
                        dt = datetime.fromtimestamp(v).astimezone()
                    strings.append(dt.strftime("%H:%M:%S"))
            except (OSError, OverflowError, ValueError):
                strings.append("")
        return strings



class DataBridge(QObject):
    hr_received = Signal(int, list)
    status_changed = Signal(str, bool)
    scan_finished = Signal(list)
class BLEWorker:
    def __init__(self, bridge: DataBridge):
        self.bridge = bridge
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.target_address = ""
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def scan_devices(self):
        asyncio.run_coroutine_threadsafe(self._scan(), self.loop)

    async def _scan(self):
        self.bridge.status_changed.emit("Scanning for HR monitors...", False)
        try:
            devices = await BleakScanner.discover(timeout=6.0, return_adv=True, scanning_mode="active")
            found = []
            for d, adv in devices.values():
                name = d.name or adv.local_name or "Unknown Device"
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                if "0000180d-0000-1000-8000-00805f9b34fb" in uuids or "h6" in name.lower() or "coospo" in name.lower():
                    found.insert(0, (f"❤️ {name}", d.address))
                else:
                    found.append((name, d.address))

            self.bridge.scan_finished.emit(found)
            self.bridge.status_changed.emit(f"Found {len(found)} devices.", False)
        except Exception as e:
            self.bridge.status_changed.emit(f"Scan error: {e}", False)

    def connect_device(self, address: str):
        self.target_address = address
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    def disconnect_device(self):
        asyncio.run_coroutine_threadsafe(self._disconnect(), self.loop)

    async def _disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.bridge.status_changed.emit("Disconnected", False)

    def _notification_handler(self, sender, data: bytearray):
        flags = data[0]
        hr_format_uint16 = flags & 0x01
        rr_present = (flags >> 4) & 0x01

        offset = 1
        if hr_format_uint16:
            bpm = int.from_bytes(data[offset:offset + 2], byteorder="little")
            offset += 2
        else:
            bpm = data[offset]
            offset += 1

        if (flags >> 3) & 0x01:
            offset += 2

        rr_intervals = []
        if rr_present:
            while offset + 1 < len(data):
                rr_raw = int.from_bytes(data[offset:offset + 2], byteorder="little")
                rr_ms = round((rr_raw / 1024.0) * 1000.0, 1)
                rr_intervals.append(rr_ms)
                offset += 2

        self.bridge.hr_received.emit(bpm, rr_intervals)

    async def _connect(self):
        if not self.target_address:
            self.bridge.status_changed.emit("Select or enter MAC/UUID first.", False)
            return

        if self.client and self.client.is_connected:
            await self._disconnect()

        self.bridge.status_changed.emit(f"Connecting to {self.target_address}...", False)
        try:
            self.client = BleakClient(self.target_address)
            await self.client.connect()
            self.bridge.status_changed.emit(f"Connected: {self.target_address}", True)
            await self.client.start_notify(HR_MEASUREMENT_CHAR_UUID, self._notification_handler)
        except Exception as e:
            self.bridge.status_changed.emit(f"Failed: {e}", False)
class CoospoApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Coospo H6 Monitor")
        self.resize(1000, 660)

        self.config = self.load_config()

        # Telemetry Buffers storing (unix_timestamp, value)
        self.bpm_history = deque()
        self.rr_history = deque()
        self.session_min = 999
        self.session_max = 0

        # Stale Signal Watchdog State
        self.last_packet_time = 0.0
        self.is_signal_stale = True
        self.blink_state = False

        # Recording State
        self.is_recording = False
        self.recorded_samples = []
        self.record_start_utc = ""
        self.last_saved_session_payload = None

        # Bridge & BLE Worker
        self.bridge = DataBridge()
        self.bridge.hr_received.connect(self.on_hr_data)
        self.bridge.status_changed.connect(self.on_status_change)
        self.bridge.scan_finished.connect(self.on_scan_results)
        self.ble = BLEWorker(self.bridge)

        self.init_ui()
        self.refresh_saved_device_combos()
        self.apply_theme()

        # 1-second timer for stale BPM check (5s+) and recording badge blink
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.timeout.connect(self.on_watchdog_tick)
        self.watchdog_timer.start(1000)

    def load_config(self) -> dict:
        defaults = {
            "device_address": "",
            "saved_devices": [],  # List of {"name": "...", "address": "..."}
            "server_url": "http://127.0.0",
            "server_sync": False,
            "time_display_mode": "actual",  # "relative" or "actual"
            "timezone_mode": "local",       # "local" or "utc"
            "details_window_sec": 300,
            "accent_color": "#00E676",
            "bg_color": "#121212"
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    defaults.update(json.load(f))
            except Exception:
                pass
        return defaults

    def save_config(self):
        self.config["device_address"] = self.input_mac.text().strip()
        self.config["server_url"] = self.input_server.text().strip()
        self.config["server_sync"] = self.chk_sync.isChecked()
        self.config["time_display_mode"] = "actual" if self.chk_actual_time.isChecked() else "relative"
        self.config["timezone_mode"] = "utc" if self.chk_utc_time.isChecked() else "local"
        self.config["details_window_sec"] = self.spin_history_sec.value()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f"Failed to save config: {e}")

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        sidebar = QWidget()
        sidebar.setFixedWidth(195)
        sidebar.setObjectName("Sidebar")
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(12, 24, 12, 24)
        side_layout.setSpacing(10)

        title = QLabel("COOSPO H6")
        title.setFont(QFont("Arial", 16, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        side_layout.addWidget(title)
        side_layout.addSpacing(20)

        self.nav_buttons = []
        for idx, label in enumerate(["Overview", "Details & Graphs", "Settings"]):
            btn = QPushButton(label)
            btn.setFixedHeight(44)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, i=idx: self.switch_page(i))
            side_layout.addWidget(btn)
            self.nav_buttons.append(btn)

        side_layout.addStretch()

        self.lbl_conn_status = QLabel("○ Disconnected")
        self.lbl_conn_status.setWordWrap(True)
        side_layout.addWidget(self.lbl_conn_status)

        main_layout.addWidget(sidebar)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.create_overview_page())
        self.pages.addWidget(self.create_details_page())
        self.pages.addWidget(self.create_settings_page())
        main_layout.addWidget(self.pages)
    def create_overview_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        # Top Bar: Title + Quick Saved Device Connect
        top_bar = QHBoxLayout()
        header = QLabel("Live Overview")
        header.setFont(QFont("Arial", 22, QFont.Bold))
        top_bar.addWidget(header)
        top_bar.addStretch()

        self.combo_quick_devices = QComboBox()
        self.combo_quick_devices.setMinimumWidth(200)
        self.btn_quick_connect = QPushButton("Quick Connect")
        self.btn_quick_connect.clicked.connect(self.quick_connect_saved)
        top_bar.addWidget(QLabel("Saved:"))
        top_bar.addWidget(self.combo_quick_devices)
        top_bar.addWidget(self.btn_quick_connect)
        layout.addLayout(top_bar)

        # Recording Control Banner
        rec_bar = QHBoxLayout()
        self.btn_record = QPushButton("● Start Recording")
        self.btn_record.setFixedHeight(38)
        self.btn_record.clicked.connect(self.toggle_recording)

        self.lbl_rec_badge = QLabel("IDLE (Not Recording)")
        self.lbl_rec_badge.setFont(QFont("Arial", 11, QFont.Bold))

        self.btn_upload_rec = QPushButton("Upload Last Recording to Server")
        self.btn_upload_rec.setFixedHeight(38)
        self.btn_upload_rec.setEnabled(False)
        self.btn_upload_rec.clicked.connect(self.upload_last_recording)

        rec_bar.addWidget(self.btn_record)
        rec_bar.addWidget(self.lbl_rec_badge)
        rec_bar.addStretch()
        rec_bar.addWidget(self.btn_upload_rec)
        layout.addLayout(rec_bar)

        # Big BPM Readout
        self.lbl_big_bpm = QLabel("-- BPM")
        self.lbl_big_bpm.setFont(QFont("Arial", 64, QFont.Bold))
        self.lbl_big_bpm.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_big_bpm)

        # Stats Row
        stats_layout = QHBoxLayout()
        self.lbl_min_bpm = QLabel("Min: --")
        self.lbl_avg_bpm = QLabel("Avg: --")
        self.lbl_max_bpm = QLabel("Max: --")
        self.lbl_zone = QLabel("Zone: --")

        for lbl in [self.lbl_min_bpm, self.lbl_avg_bpm, self.lbl_max_bpm, self.lbl_zone]:
            lbl.setFont(QFont("Arial", 14))
            lbl.setAlignment(Qt.AlignCenter)
            stats_layout.addWidget(lbl)

        layout.addLayout(stats_layout)

        # Mini Graph with Custom Time Axis
        mini_axis = TimeAxisItem(
            lambda: self.config["time_display_mode"],
            lambda: self.config["timezone_mode"],
            orientation="bottom"
        )
        self.mini_plot = pg.PlotWidget(title="Recent Heart Rate (Last 60s)", axisItems={"bottom": mini_axis})
        self.mini_plot.setMouseEnabled(x=False, y=False)
        self.mini_plot.hideButtons()
        self.mini_curve = self.mini_plot.plot()
        layout.addWidget(self.mini_plot)

        return page

    def create_details_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)

        header_row = QHBoxLayout()
        self.lbl_details_header = QLabel(f"In-Depth Telemetry (Last {self.config['details_window_sec']}s)")
        self.lbl_details_header.setFont(QFont("Arial", 20, QFont.Bold))
        btn_reset = QPushButton("Reset Session")
        btn_reset.setFixedWidth(130)
        btn_reset.clicked.connect(self.reset_session)
        header_row.addWidget(self.lbl_details_header)
        header_row.addStretch()
        header_row.addWidget(btn_reset)
        layout.addLayout(header_row)

        hr_axis = TimeAxisItem(
            lambda: self.config["time_display_mode"],
            lambda: self.config["timezone_mode"],
            orientation="bottom"
        )
        self.detail_hr_plot = pg.PlotWidget(title="Session Heart Rate (BPM)", axisItems={"bottom": hr_axis})
        self.detail_hr_plot.showGrid(x=True, y=True, alpha=0.3)
        self.detail_hr_plot.setLabel("left", "BPM")
        self.detail_hr_curve = self.detail_hr_plot.plot()
        layout.addWidget(self.detail_hr_plot)

        rr_axis = TimeAxisItem(
            lambda: self.config["time_display_mode"],
            lambda: self.config["timezone_mode"],
            orientation="bottom"
        )
        self.detail_rr_plot = pg.PlotWidget(title="R-R Intervals (ms) - Beat-to-Beat Timing", axisItems={"bottom": rr_axis})
        self.detail_rr_plot.showGrid(x=True, y=True, alpha=0.3)
        self.detail_rr_plot.setLabel("left", "Milliseconds")
        self.detail_rr_curve = self.detail_rr_plot.plot(
            pen=pg.mkPen(color="#FF5252", width=2), symbol="o", symbolSize=5
        )
        layout.addWidget(self.detail_rr_plot)

        self.lbl_raw_rr = QLabel("Latest R-R Intervals: None")
        self.lbl_raw_rr.setFont(QFont("Consolas", 11))
        layout.addWidget(self.lbl_raw_rr)

        return page

    def create_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QLabel("Settings")
        header.setFont(QFont("Arial", 22, QFont.Bold))
        layout.addWidget(header)

        # 1. Saved & Scanned Devices
        ble_group = QGroupBox("Bluetooth & Saved Devices")
        ble_layout = QFormLayout(ble_group)

        saved_row = QHBoxLayout()
        self.combo_saved_devices = QComboBox()
        self.combo_saved_devices.currentIndexChanged.connect(self.on_saved_device_chosen)
        btn_del_saved = QPushButton("Remove")
        btn_del_saved.setFixedWidth(90)
        btn_del_saved.clicked.connect(self.remove_saved_device)
        saved_row.addWidget(self.combo_saved_devices, 1)
        saved_row.addWidget(btn_del_saved)
        ble_layout.addRow("Saved Bands:", saved_row)

        scan_row = QHBoxLayout()
        self.combo_devices = QComboBox()
        self.btn_scan = QPushButton("Scan Nearby")
        self.btn_scan.clicked.connect(self.ble.scan_devices)
        scan_row.addWidget(self.combo_devices, 1)
        scan_row.addWidget(self.btn_scan)
        ble_layout.addRow("Scan Nearby:", scan_row)

        self.input_device_name = QLineEdit()
        self.input_device_name.setPlaceholderText("Friendly Name (e.g., My Coospo H6)")
        ble_layout.addRow("Band Label:", self.input_device_name)

        self.input_mac = QLineEdit(self.config["device_address"])
        self.input_mac.setPlaceholderText("MAC (Win/Linux) or UUID (macOS)")
        self.input_mac.editingFinished.connect(self.save_config)
        self.combo_devices.currentIndexChanged.connect(self.on_device_selected)
        ble_layout.addRow("MAC / UUID:", self.input_mac)

        conn_btns = QHBoxLayout()
        self.btn_save_dev = QPushButton("Save Device")
        self.btn_connect = QPushButton("Connect Band")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_save_dev.clicked.connect(self.save_current_device)
        self.btn_connect.clicked.connect(self.connect_band)
        self.btn_disconnect.clicked.connect(self.ble.disconnect_device)
        conn_btns.addWidget(self.btn_save_dev)
        conn_btns.addWidget(self.btn_connect)
        conn_btns.addWidget(self.btn_disconnect)
        ble_layout.addRow("", conn_btns)

        layout.addWidget(ble_group)

        # 2. Time & Graph Configuration
        time_group = QGroupBox("Time Format & Graph History")
        time_layout = QFormLayout(time_group)

        time_toggles = QHBoxLayout()
        self.chk_actual_time = QCheckBox("Use Actual Clock Time (unchecked = -Xs relative)")
        self.chk_actual_time.setChecked(self.config["time_display_mode"] == "actual")
        self.chk_actual_time.toggled.connect(self.on_time_settings_changed)

        self.chk_utc_time = QCheckBox("Use UTC (unchecked = Local Time)")
        self.chk_utc_time.setChecked(self.config["timezone_mode"] == "utc")
        self.chk_utc_time.toggled.connect(self.on_time_settings_changed)

        time_toggles.addWidget(self.chk_actual_time)
        time_toggles.addWidget(self.chk_utc_time)
        time_layout.addRow("Axis Format:", time_toggles)

        self.spin_history_sec = QSpinBox()
        self.spin_history_sec.setRange(30, 86400)
        self.spin_history_sec.setSingleStep(30)
        self.spin_history_sec.setSuffix(" seconds")
        self.spin_history_sec.setValue(int(self.config["details_window_sec"]))
        self.spin_history_sec.valueChanged.connect(self.on_time_settings_changed)
        time_layout.addRow("Details Window:", self.spin_history_sec)

        layout.addWidget(time_group)

        # 3. Server & Visual Appearance
        srv_group = QGroupBox("Server Sync & Appearance")
        srv_layout = QFormLayout(srv_group)

        self.input_server = QLineEdit(self.config["server_url"])
        self.input_server.editingFinished.connect(self.save_config)
        srv_layout.addRow("Telemetry URL:", self.input_server)

        self.chk_sync = QCheckBox("Stream live telemetry to server")
        self.chk_sync.setChecked(self.config["server_sync"])
        self.chk_sync.toggled.connect(self.save_config)
        srv_layout.addRow("Live Stream:", self.chk_sync)

        vis_btns = QHBoxLayout()
        btn_accent = QPushButton("Accent Color")
        btn_accent.clicked.connect(self.pick_accent_color)
        btn_bg = QPushButton("Background Color")
        btn_bg.clicked.connect(self.pick_bg_color)
        btn_reset_colors = QPushButton("Reset Colors")
        btn_reset_colors.clicked.connect(self.reset_colors)
        vis_btns.addWidget(btn_accent)
        vis_btns.addWidget(btn_bg)
        vis_btns.addWidget(btn_reset_colors)
        srv_layout.addRow("Theme:", vis_btns)

        layout.addWidget(srv_group)
        layout.addStretch()
        return page
    def switch_page(self, index: int):
        self.pages.setCurrentIndex(index)

    def refresh_saved_device_combos(self):
        self.combo_saved_devices.blockSignals(True)
        self.combo_quick_devices.blockSignals(True)
        self.combo_saved_devices.clear()
        self.combo_quick_devices.clear()

        for dev in self.config["saved_devices"]:
            label = f"{dev['name']} [{dev['address']}]"
            self.combo_saved_devices.addItem(label, dev["address"])
            self.combo_quick_devices.addItem(label, dev["address"])

        self.combo_saved_devices.blockSignals(False)
        self.combo_quick_devices.blockSignals(False)

    def save_current_device(self):
        addr = self.input_mac.text().strip()
        if not addr:
            return
        name = self.input_device_name.text().strip() or f"Band ({addr[-5:]})"
        updated = False
        for dev in self.config["saved_devices"]:
            if dev["address"].lower() == addr.lower():
                dev["name"] = name
                updated = True
                break
        if not updated:
            self.config["saved_devices"].append({"name": name, "address": addr})

        self.save_config()
        self.refresh_saved_device_combos()

    def remove_saved_device(self):
        idx = self.combo_saved_devices.currentIndex()
        if idx >= 0:
            addr = self.combo_saved_devices.itemData(idx)
            self.config["saved_devices"] = [
                d for d in self.config["saved_devices"] if d["address"] != addr
            ]
            self.save_config()
            self.refresh_saved_device_combos()

    def on_saved_device_chosen(self, index: int):
        addr = self.combo_saved_devices.itemData(index)
        if addr:
            self.input_mac.setText(addr)
            for dev in self.config["saved_devices"]:
                if dev["address"] == addr:
                    self.input_device_name.setText(dev["name"])
            self.save_config()

    def quick_connect_saved(self):
        addr = self.combo_quick_devices.currentData()
        if addr:
            self.input_mac.setText(addr)
            self.save_config()
            self.ble.connect_device(addr)

    def connect_band(self):
        self.save_config()
        self.ble.connect_device(self.input_mac.text().strip())

    def on_scan_results(self, devices):
        self.combo_devices.clear()
        for name, addr in devices:
            self.combo_devices.addItem(f"{name} [{addr}]", (name, addr))

    def on_device_selected(self, index):
        data = self.combo_devices.itemData(index)
        if data:
            name, addr = data
            self.input_mac.setText(addr)
            clean_name = name.replace("❤️ ", "")
            if clean_name != "Unknown Device":
                self.input_device_name.setText(clean_name)
            self.save_config()

    def on_status_change(self, msg: str, connected: bool):
        prefix = "● " if connected else "○ "
        self.lbl_conn_status.setText(prefix + msg)
        color = self.config["accent_color"] if connected else "#888888"
        self.lbl_conn_status.setStyleSheet(f"color: {color}; font-weight: bold;")
    def on_time_settings_changed(self):
        self.save_config()
        self.lbl_details_header.setText(f"In-Depth Telemetry (Last {self.config['details_window_sec']}s)")
        self.mini_plot.getAxis("bottom").picture = None
        self.detail_hr_plot.getAxis("bottom").picture = None
        self.detail_rr_plot.getAxis("bottom").picture = None
        self.refresh_graphs()

    def on_watchdog_tick(self):
        now = time.time()
        if self.last_packet_time == 0.0 or (now - self.last_packet_time) >= 5.0:
            self.is_signal_stale = True
            self.lbl_big_bpm.setText("-- BPM")
            self.lbl_big_bpm.setStyleSheet("color: #FF3B30;")
            self.lbl_zone.setText("Zone: Signal Lost (5s+)")

        if self.is_recording:
            self.blink_state = not self.blink_state
            dot = "🔴" if self.blink_state else "⭕"
            self.lbl_rec_badge.setText(f"{dot} RECORDING ({len(self.recorded_samples)} samples)")
            self.lbl_rec_badge.setStyleSheet("color: #FF3B30; font-weight: bold;")

    def on_hr_data(self, bpm: int, rr_intervals: list):
        now_ts = time.time()
        self.last_packet_time = now_ts
        self.is_signal_stale = False
        self.lbl_big_bpm.setStyleSheet(f"color: {self.config['accent_color']};")

        self.bpm_history.append((now_ts, bpm))
        for rr in rr_intervals:
            self.rr_history.append((now_ts, rr))

        max_window = max(60, int(self.config["details_window_sec"]))
        cutoff = now_ts - max_window
        while self.bpm_history and self.bpm_history[0][0] < cutoff:
            self.bpm_history.popleft()
        while self.rr_history and self.rr_history[0][0] < cutoff:
            self.rr_history.popleft()

        self.session_min = min(self.session_min, bpm)
        self.session_max = max(self.session_max, bpm)
        avg_bpm = int(sum(v for _, v in self.bpm_history) / len(self.bpm_history))

        self.lbl_big_bpm.setText(f"{bpm} BPM")
        self.lbl_min_bpm.setText(f"Min: {self.session_min}")
        self.lbl_avg_bpm.setText(f"Avg: {avg_bpm}")
        self.lbl_max_bpm.setText(f"Max: {self.session_max}")
        self.lbl_zone.setText(f"Zone: {self.get_hr_zone(bpm)}")

        if rr_intervals:
            self.lbl_raw_rr.setText(f"Latest R-R Intervals (ms): {rr_intervals}")

        self.refresh_graphs()

        dt_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)
        dt_local = datetime.fromtimestamp(now_ts).astimezone()
        if self.is_recording:
            self.recorded_samples.append({
                "timestamp_utc": dt_utc.isoformat(),
                "timestamp_local": dt_local.isoformat(),
                "bpm": bpm,
                "rr_intervals_ms": rr_intervals
            })

        if self.chk_sync.isChecked():
            payload = {
                "timestamp": dt_utc.isoformat(),
                "device_id": self.input_mac.text().strip() or "coospo-h6",
                "bpm": bpm,
                "rr_intervals_ms": rr_intervals
            }
            url = self.input_server.text().strip()
            threading.Thread(target=self._send_to_server, args=(url, payload), daemon=True).start()

    def refresh_graphs(self):
        if not self.bpm_history:
            return
        now_ts = time.time()

        mini_data = [(t, v) for t, v in self.bpm_history if t >= (now_ts - 60)]
        if mini_data:
            self.mini_curve.setData([t for t, _ in mini_data], [v for _, v in mini_data])

        self.detail_hr_curve.setData(
            [t for t, _ in self.bpm_history],
            [v for _, v in self.bpm_history]
        )

        if self.rr_history:
            self.detail_rr_curve.setData(
                [t for t, _ in self.rr_history],
                [v for _, v in self.rr_history]
            )

    def reset_session(self):
        self.bpm_history.clear()
        self.rr_history.clear()
        self.session_min = 999
        self.session_max = 0
        self.lbl_min_bpm.setText("Min: --")
        self.lbl_avg_bpm.setText("Avg: --")
        self.lbl_max_bpm.setText("Max: --")
        self.mini_curve.setData([], [])
        self.detail_hr_curve.setData([], [])
        self.detail_rr_curve.setData([], [])
    def toggle_recording(self):
        if not self.is_recording:
            self.is_recording = True
            self.recorded_samples = []
            self.record_start_utc = datetime.now(timezone.utc).isoformat()
            self.btn_record.setText("■ Stop & Export Recording")
            self.btn_record.setStyleSheet("background-color: #FF3B30; color: #FFFFFF; font-weight: bold;")
            self.lbl_rec_badge.setText("🔴 RECORDING (0 samples)")
            self.lbl_rec_badge.setStyleSheet("color: #FF3B30; font-weight: bold;")
        else:
            self.is_recording = False
            self.btn_record.setText("● Start Recording")
            self.btn_record.setStyleSheet("")
            self.export_recording_files()

    def export_recording_files(self):
        if not self.recorded_samples:
            self.lbl_rec_badge.setText("IDLE (No samples recorded)")
            self.lbl_rec_badge.setStyleSheet("color: #888888;")
            return

        ended_utc = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"coospo_session_{stamp}"
        device_id = self.input_mac.text().strip() or "coospo-h6"

        csv_path = RECORDINGS_DIR / f"{base_name}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_utc", "timestamp_local", "bpm", "rr_intervals_ms"])
            for s in self.recorded_samples:
                writer.writerow([
                    s["timestamp_utc"],
                    s["timestamp_local"],
                    s["bpm"],
                    ";".join(map(str, s["rr_intervals_ms"]))
                ])

        session_payload = {
            "session_name": base_name,
            "device_id": device_id,
            "started_at_utc": self.record_start_utc,
            "ended_at_utc": ended_utc,
            "samples": self.recorded_samples
        }
        json_path = RECORDINGS_DIR / f"{base_name}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(session_payload, f, indent=2)

        txt_path = RECORDINGS_DIR / f"{base_name}.txt"
        bpms = [s["bpm"] for s in self.recorded_samples]
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"Coospo H6 Session Report: {base_name}\n")
            f.write(f"Device: {device_id}\n")
            f.write(f"Start (UTC): {self.record_start_utc}\n")
            f.write(f"End   (UTC): {ended_utc}\n")
            f.write(f"Samples: {len(bpms)} | Min: {min(bpms)} BPM | Avg: {round(sum(bpms)/len(bpms), 1)} BPM | Max: {max(bpms)} BPM\n")
            f.write("-" * 75 + "\n")
            for s in self.recorded_samples:
                f.write(f"UTC: {s['timestamp_utc']} | Local: {s['timestamp_local']} | {s['bpm']} BPM | RR: {s['rr_intervals_ms']}\n")

        tcx_path = RECORDINGS_DIR / f"{base_name}.tcx"
        with open(tcx_path, "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<TrainingCenterDatabase xmlns="http://garmin.com">\n')
            f.write('  <Activities><Activity Sport="Other">\n')
            f.write(f'    <Id>{self.record_start_utc}</Id>\n')
            f.write(f'    <Lap StartTime="{self.record_start_utc}"><Track>\n')
            for s in self.recorded_samples:
                f.write(f'      <Trackpoint><Time>{s["timestamp_utc"]}</Time>')
                f.write(f'<HeartRateBpm><Value>{s["bpm"]}</Value></HeartRateBpm></Trackpoint>\n')
            f.write('    </Track></Lap>\n  </Activity></Activities>\n</TrainingCenterDatabase>\n')

        self.last_saved_session_payload = session_payload
        self.btn_upload_rec.setEnabled(True)
        self.lbl_rec_badge.setText(f"Saved CSV/JSON/TXT/TCX ({len(bpms)} pts)")
        self.lbl_rec_badge.setStyleSheet(f"color: {self.config['accent_color']}; font-weight: bold;")

        QMessageBox.information(
            self,
            "Recording Exported",
            f"Saved 4 formats (CSV, JSON, TXT, TCX) to:\n{RECORDINGS_DIR}"
        )

    def upload_last_recording(self):
        if not self.last_saved_session_payload:
            return
        base_url = self.input_server.text().strip()
        if base_url.endswith("/api/telemetry"):
            upload_url = base_url.replace("/api/telemetry", "/api/recordings")
        else:
            upload_url = base_url.rstrip("/") + "/api/recordings"

        self.btn_upload_rec.setEnabled(False)
        self.btn_upload_rec.setText("Uploading...")

        def _worker():
            try:
                resp = requests.post(upload_url, json=self.last_saved_session_payload, timeout=10.0)
                ok = resp.status_code == 200
            except Exception:
                ok = False
            QTimer.singleShot(0, lambda: self._on_upload_done(ok))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_upload_done(self, success: bool):
        self.btn_upload_rec.setEnabled(True)
        self.btn_upload_rec.setText("Upload Last Recording to Server")
        if success:
            QMessageBox.information(self, "Upload Complete", "Recording successfully uploaded to server!")
        else:
            QMessageBox.warning(self, "Upload Failed", "Could not reach server. Check server URL in Settings.")

    def _send_to_server(self, url: str, payload: dict):
        try:
            requests.post(url, json=payload, timeout=2.0)
        except Exception:
            pass

    def get_hr_zone(self, bpm: int) -> str:
        if bpm < 100:
            return "Resting / Warmup"
        elif bpm < 130:
            return "Fat Burn (Zone 2)"
        elif bpm < 155:
            return "Aerobic (Zone 3)"
        elif bpm < 175:
            return "Anaerobic (Zone 4)"
        return "Max Effort (Zone 5)"

    def pick_accent_color(self):
        color = QColorDialog.getColor(QColor(self.config["accent_color"]), self)
        if color.isValid():
            self.config["accent_color"] = color.name()
            self.save_config()
            self.apply_theme()

    def pick_bg_color(self):
        color = QColorDialog.getColor(QColor(self.config["bg_color"]), self)
        if color.isValid():
            self.config["bg_color"] = color.name()
            self.save_config()
            self.apply_theme()

    def reset_colors(self):
        self.config["accent_color"] = "#00E676"
        self.config["bg_color"] = "#121212"
        self.save_config()
        self.apply_theme()

    def apply_theme(self):
        accent = self.config["accent_color"]
        bg = self.config["bg_color"]

        self.mini_plot.setBackground(bg)
        self.detail_hr_plot.setBackground(bg)
        self.detail_rr_plot.setBackground(bg)
        self.mini_curve.setPen(pg.mkPen(color=accent, width=3))
        self.detail_hr_curve.setPen(pg.mkPen(color=accent, width=2))

        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {bg};
                color: #FFFFFF;
                font-family: Arial, sans-serif;
            }}
            #Sidebar {{
                background-color: rgba(0, 0, 0, 0.35);
                border-right: 1px solid #333333;
            }}
            QPushButton {{
                background-color: #2A2A2A;
                border: 1px solid #444444;
                border-radius: 6px;
                padding: 7px 12px;
                color: #FFFFFF;
                font-weight: bold;
            }}
            QPushButton:hover {{
                border: 1px solid {accent};
            }}
            QLineEdit, QComboBox, QSpinBox {{
                background-color: #1E1E1E;
                border: 1px solid #444444;
                border-radius: 4px;
                padding: 6px;
                color: #FFFFFF;
            }}
            QGroupBox {{
                border: 1px solid #333333;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 15px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
                color: {accent};
            }}
        """)
        if not self.is_signal_stale:
            self.lbl_big_bpm.setStyleSheet(f"color: {accent};")
        else:
            self.lbl_big_bpm.setStyleSheet("color: #FF3B30;")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = CoospoApp()
    window.show()
    sys.exit(app.exec())
