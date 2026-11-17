import sys
import os
import json
import asyncio
import threading
from pathlib import Path
from datetime import datetime, timezone
from collections import deque
import requests

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QLineEdit, QFormLayout,
    QColorDialog, QComboBox, QGroupBox, QCheckBox
)
from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtGui import QFont, QColor
import pyqtgraph as pg
from bleak import BleakClient, BleakScanner

HR_MEASUREMENT_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

APP_DIR = Path.home() / ".coospo_h6_monitor"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = str(APP_DIR / "config.json")


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
        self.bridge.status_changed.emit("Scanning for BLE devices...", False)
        try:
            devices = await BleakScanner.discover(timeout=4.0)
            found = [(d.name or "Unknown Device", d.address) for d in devices]
            self.bridge.scan_finished.emit(found)
            self.bridge.status_changed.emit("Scan complete.", False)
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
            self.bridge.status_changed.emit("Enter MAC/UUID first.", False)
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
        self.resize(950, 620)

        self.config = self.load_config()

        self.bpm_history = deque(maxlen=300)
        self.rr_history = deque(maxlen=100)
        self.session_min = 999
        self.session_max = 0

        self.bridge = DataBridge()
        self.bridge.hr_received.connect(self.on_hr_data)
        self.bridge.status_changed.connect(self.on_status_change)
        self.bridge.scan_finished.connect(self.on_scan_results)
        self.ble = BLEWorker(self.bridge)

        self.init_ui()
        self.apply_theme()

    def load_config(self) -> dict:
        defaults = {
            "device_address": "",
            "server_url": "http://127.0.0",
            "server_sync": False,
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
        sidebar.setFixedWidth(190)
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
        layout.setContentsMargins(30, 30, 30, 30)

        header = QLabel("Live Overview")
        header.setFont(QFont("Arial", 22, QFont.Bold))
        layout.addWidget(header)

        self.lbl_big_bpm = QLabel("-- BPM")
        self.lbl_big_bpm.setFont(QFont("Arial", 64, QFont.Bold))
        self.lbl_big_bpm.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_big_bpm)

        stats_layout = QHBoxLayout()
        self.lbl_min_bpm = QLabel("Min: --")
        self.lbl_avg_bpm = QLabel("Avg: --")
        self.lbl_max_bpm = QLabel("Max: --")
        self.lbl_zone = QLabel("Zone: Resting")

        for lbl in [self.lbl_min_bpm, self.lbl_avg_bpm, self.lbl_max_bpm, self.lbl_zone]:
            lbl.setFont(QFont("Arial", 14))
            lbl.setAlignment(Qt.AlignCenter)
            stats_layout.addWidget(lbl)

        layout.addLayout(stats_layout)

        self.mini_plot = pg.PlotWidget(title="Recent Heart Rate (Last 60s)")
        self.mini_plot.setMouseEnabled(x=False, y=False)
        self.mini_plot.hideButtons()
        self.mini_curve = self.mini_plot.plot()
        layout.addWidget(self.mini_plot)

        return page

    def create_details_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 30, 30, 30)

        header_row = QHBoxLayout()
        header = QLabel("In-Depth Telemetry & HRV")
        header.setFont(QFont("Arial", 22, QFont.Bold))
        btn_reset = QPushButton("Reset Session")
        btn_reset.setFixedWidth(130)
        btn_reset.clicked.connect(self.reset_session)
        header_row.addWidget(header)
        header_row.addStretch()
        header_row.addWidget(btn_reset)
        layout.addLayout(header_row)

        self.detail_hr_plot = pg.PlotWidget(title="Session Heart Rate (BPM)")
        self.detail_hr_plot.showGrid(x=True, y=True, alpha=0.3)
        self.detail_hr_plot.setLabel("left", "BPM")
        self.detail_hr_curve = self.detail_hr_plot.plot()
        layout.addWidget(self.detail_hr_plot)

        self.detail_rr_plot = pg.PlotWidget(title="R-R Intervals (ms) - Beat-to-Beat Timing")
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
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(18)

        header = QLabel("Settings")
        header.setFont(QFont("Arial", 22, QFont.Bold))
        layout.addWidget(header)

        ble_group = QGroupBox("Coospo H6 Bluetooth Connection")
        ble_layout = QFormLayout(ble_group)

        scan_row = QHBoxLayout()
        self.combo_devices = QComboBox()
        self.btn_scan = QPushButton("Scan Nearby")
        self.btn_scan.clicked.connect(self.ble.scan_devices)
        scan_row.addWidget(self.combo_devices, 1)
        scan_row.addWidget(self.btn_scan)
        ble_layout.addRow("Discovered:", scan_row)

        self.input_mac = QLineEdit(self.config["device_address"])
        self.input_mac.setPlaceholderText("e.g., C4:21:08:XX:XX:XX (Win/Linux) or UUID (macOS)")
        self.input_mac.editingFinished.connect(self.save_config)
        self.combo_devices.currentIndexChanged.connect(self.on_device_selected)
        ble_layout.addRow("MAC / UUID:", self.input_mac)

        conn_btns = QHBoxLayout()
        self.btn_connect = QPushButton("Connect Band")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_connect.clicked.connect(self.connect_band)
        self.btn_disconnect.clicked.connect(self.ble.disconnect_device)
        conn_btns.addWidget(self.btn_connect)
        conn_btns.addWidget(self.btn_disconnect)
        ble_layout.addRow("", conn_btns)

        layout.addWidget(ble_group)

        srv_group = QGroupBox("Data Collection Server")
        srv_layout = QFormLayout(srv_group)

        self.input_server = QLineEdit(self.config["server_url"])
        self.input_server.editingFinished.connect(self.save_config)
        srv_layout.addRow("Endpoint URL:", self.input_server)

        self.chk_sync = QCheckBox("Stream live telemetry to server")
        self.chk_sync.setChecked(self.config["server_sync"])
        self.chk_sync.toggled.connect(self.save_config)
        srv_layout.addRow("Sync Status:", self.chk_sync)

        layout.addWidget(srv_group)

        vis_group = QGroupBox("Visual Appearance")
        vis_layout = QHBoxLayout(vis_group)

        btn_accent = QPushButton("Accent / Graph Color")
        btn_accent.clicked.connect(self.pick_accent_color)

        btn_bg = QPushButton("Background Color")
        btn_bg.clicked.connect(self.pick_bg_color)

        btn_reset_colors = QPushButton("Reset Defaults")
        btn_reset_colors.clicked.connect(self.reset_colors)

        vis_layout.addWidget(btn_accent)
        vis_layout.addWidget(btn_bg)
        vis_layout.addWidget(btn_reset_colors)
        layout.addWidget(vis_group)

        layout.addStretch()
        return page
    def switch_page(self, index: int):
        self.pages.setCurrentIndex(index)

    def connect_band(self):
        self.save_config()
        self.ble.connect_device(self.input_mac.text().strip())

    def reset_session(self):
        self.bpm_history.clear()
        self.rr_history.clear()
        self.session_min = 999
        self.session_max = 0
        self.lbl_big_bpm.setText("-- BPM")
        self.lbl_min_bpm.setText("Min: --")
        self.lbl_avg_bpm.setText("Avg: --")
        self.lbl_max_bpm.setText("Max: --")
        self.mini_curve.setData([])
        self.detail_hr_curve.setData([])
        self.detail_rr_curve.setData([])

    def on_scan_results(self, devices):
        self.combo_devices.clear()
        for name, addr in devices:
            self.combo_devices.addItem(f"{name} [{addr}]", addr)

    def on_device_selected(self, index):
        addr = self.combo_devices.itemData(index)
        if addr:
            self.input_mac.setText(addr)
            self.save_config()

    def on_status_change(self, msg: str, connected: bool):
        prefix = "● " if connected else "○ "
        self.lbl_conn_status.setText(prefix + msg)
        color = self.config["accent_color"] if connected else "#888888"
        self.lbl_conn_status.setStyleSheet(f"color: {color}; font-weight: bold;")

    def on_hr_data(self, bpm: int, rr_intervals: list):
        self.bpm_history.append(bpm)
        for rr in rr_intervals:
            self.rr_history.append(rr)

        self.session_min = min(self.session_min, bpm)
        self.session_max = max(self.session_max, bpm)
        avg_bpm = int(sum(self.bpm_history) / len(self.bpm_history))

        self.lbl_big_bpm.setText(f"{bpm} BPM")
        self.lbl_min_bpm.setText(f"Min: {self.session_min}")
        self.lbl_avg_bpm.setText(f"Avg: {avg_bpm}")
        self.lbl_max_bpm.setText(f"Max: {self.session_max}")
        self.lbl_zone.setText(f"Zone: {self.get_hr_zone(bpm)}")

        bpm_list = list(self.bpm_history)
        self.mini_curve.setData(bpm_list[-60:])
        self.detail_hr_curve.setData(bpm_list)
        self.detail_rr_curve.setData(list(self.rr_history))

        if rr_intervals:
            self.lbl_raw_rr.setText(f"Latest R-R Intervals (ms): {rr_intervals}")

        if self.chk_sync.isChecked():
            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "device_id": self.input_mac.text().strip() or "coospo-h6",
                "bpm": bpm,
                "rr_intervals_ms": rr_intervals
            }
            url = self.input_server.text().strip()
            threading.Thread(target=self._send_to_server, args=(url, payload), daemon=True).start()

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
                padding: 8px;
                color: #FFFFFF;
                font-weight: bold;
            }}
            QPushButton:hover {{
                border: 1px solid {accent};
            }}
            QLineEdit, QComboBox {{
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
        self.lbl_big_bpm.setStyleSheet(f"color: {accent};")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = CoospoApp()
    window.show()
    sys.exit(app.exec())
