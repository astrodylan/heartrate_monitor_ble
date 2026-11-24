import sys
import os
import csv
import json
import time
import asyncio
import threading
import queue
from pathlib import Path
from datetime import datetime, timezone
from collections import deque
import requests

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QStackedWidget, QLineEdit, QFormLayout,
    QColorDialog, QComboBox, QGroupBox, QCheckBox, QSpinBox, QMessageBox,
    QScrollArea
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QUrl
from PySide6.QtGui import QFont, QColor, QDesktopServices
import pyqtgraph as pg
from bleak import BleakClient, BleakScanner

HR_MEASUREMENT_CHAR_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"

APP_DIR = Path.home() / ".heart_rate_monitor"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = str(APP_DIR / "config.json")
RECORDINGS_DIR = APP_DIR / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


class TimeAxisItem(pg.AxisItem):
    """Formats Unix timestamps as relative seconds (-Xs) or HH:MM:SS (Local/UTC)."""
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
                if v < 946684800 or v > 4102444800:
                    strings.append("")
                    continue
                if mode == "relative":
                    diff = int(round(v - now))
                    strings.append(f"{diff}s" if diff != 0 else "Now")
                else:
                    dt = (
                        datetime.fromtimestamp(v, tz=timezone.utc)
                        if tz_mode == "utc"
                        else datetime.fromtimestamp(v).astimezone()
                    )
                    strings.append(dt.strftime("%H:%M:%S"))
            except (OSError, OverflowError, ValueError):
                strings.append("")
        return strings


class DataBridge(QObject):
    hr_received = Signal(int, list)
    battery_received = Signal(int)
    status_changed = Signal(str, bool)
    scan_finished = Signal(list)
class BLEWorker:
    """Owns all BLE operations on one asyncio loop/thread."""
    HEART_RATE_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"

    def __init__(self, bridge: DataBridge):
        self.bridge = bridge
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.target_address = ""
        self.discovered_devices = {}
        self.discovered_at = {}
        self.manual_disconnect = False
        self.auto_reconnect_enabled = True
        self.reconnect_task = None
        self.battery_task = None
        self.connection_state = "disconnected"
        self._scan_lock = None  # created on the BLE event loop
        self.thread = threading.Thread(target=self._run_loop, name="BLE-Event-Loop", daemon=True)
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self._scan_lock = asyncio.Lock()
        self.loop.run_forever()

    def _set_state(self, state: str, message: str, connected: bool = False):
        """Single point for connection-state transitions and UI updates."""
        self.connection_state = state
        self.bridge.status_changed.emit(message, connected)

    def scan_devices(self):
        if self.connection_state in ("scanning", "connecting", "reconnecting"):
            return
        asyncio.run_coroutine_threadsafe(self._scan(), self.loop)

    async def _scan(self):
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        if self.connection_state in ("connecting", "reconnecting"):
            return
        async with self._scan_lock:
            previous_state = self.connection_state
            self._set_state("scanning", "Scanning nearby Bluetooth devices...", bool(self.client and self.client.is_connected))
            try:
                # A longer active scan improves discovery of advertising bands.
                # One deliberate 10-second active scan gives low-duty-cycle
                # heart-rate straps time to advertise without rapid rescanning.
                devices = await BleakScanner.discover(
                    timeout=10.0, return_adv=True, scanning_mode="active"
                )
                found = []
                seen = set()
                # Keep older sightings as fallback handles, but timestamp every
                # device seen in this scan so stale entries can be rejected.

                if self.client and self.client.is_connected and self.target_address:
                    found.append(("❤️ Connected Band", self.target_address))
                    seen.add(self.target_address.lower())

                ranked = []
                for device, advertisement in devices.values():
                    address = device.address
                    normalized = address.lower()
                    self.discovered_devices[normalized] = device
                    self.discovered_at[normalized] = time.monotonic()
                    if normalized in seen:
                        continue
                    seen.add(normalized)

                    name = device.name or advertisement.local_name
                    # Some straps intentionally omit their advertised name.
                    # Keep them selectable and distinguish them by platform ID.
                    if not name:
                        suffix = str(address)[-5:]
                        name = f"Unnamed BLE device ({suffix})"
                    service_uuids = [u.lower() for u in (advertisement.service_uuids or [])]
                    has_hr_service = self.HEART_RATE_SERVICE_UUID in service_uuids
                    name_hint = any(token in name.lower() for token in ("heart", "hr", "h6", "coospo", "polar", "garmin", "wahoo"))
                    label = f"❤️ {name}" if has_hr_service or name_hint else name
                    # Heart-rate service advertisements are shown first, but other
                    # devices remain selectable for bands that omit service UUIDs.
                    ranked.append((0 if has_hr_service else 1 if name_hint else 2, label, address))

                ranked.sort(key=lambda item: (item[0], item[1].lower()))
                found.extend((label, address) for _, label, address in ranked)
                self.bridge.scan_finished.emit(found)
                connected = bool(self.client and self.client.is_connected)
                self._set_state(
                    "connected" if connected else "disconnected",
                    f"Found {len(found)} Bluetooth device(s). Select your band to connect.",
                    connected
                )
            except Exception as exc:
                connected = bool(self.client and self.client.is_connected)
                self._set_state(
                    "connected" if connected else "disconnected",
                    f"Bluetooth scan failed: {exc}",
                    connected
                )

    def connect_device(self, address: str, max_retry_sec: float = 30.0):
        address = (address or "").strip()
        if not address:
            self._set_state("disconnected", "Enter or select the band's Bluetooth address first.", False)
            return
        self.target_address = address
        self.manual_disconnect = False

        def schedule():
            if self.reconnect_task and not self.reconnect_task.done():
                self.reconnect_task.cancel()
            self.reconnect_task = asyncio.create_task(self._connect_with_retry(max_retry_sec))

        self.loop.call_soon_threadsafe(schedule)

    def disconnect_device(self):
        self.manual_disconnect = True

        def schedule():
            if self.reconnect_task and not self.reconnect_task.done():
                self.reconnect_task.cancel()
            asyncio.create_task(self._disconnect())

        self.loop.call_soon_threadsafe(schedule)

    async def _stop_battery_poll(self):
        task = self.battery_task
        self.battery_task = None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _disconnect(self):
        await self._stop_battery_poll()
        old_client = self.client
        self.client = None
        if old_client is not None:
            try:
                if old_client.is_connected:
                    await old_client.disconnect()
            except Exception as exc:
                print(f"[BLE Disconnect Error]: {exc!r}")
        self.bridge.battery_received.emit(-1)
        self._set_state("disconnected", "Disconnected", False)

    def _on_unexpected_disconnect(self, client):
        # Bleak invokes this on the BLE event loop.
        if self.client is client:
            self.client = None
        if self.battery_task and not self.battery_task.done():
            self.battery_task.cancel()
        self.bridge.battery_received.emit(-1)

        if self.manual_disconnect:
            self._set_state("disconnected", "Disconnected", False)
            return

        self._set_state("reconnecting", "Connection lost. Searching for the band...", False)
        if self.auto_reconnect_enabled and self.target_address:
            if self.reconnect_task and not self.reconnect_task.done():
                self.reconnect_task.cancel()
            self.reconnect_task = asyncio.create_task(self._connect_with_retry(30.0))

    def _notification_handler(self, sender, data: bytearray):
        # BLE Heart Rate Measurement: flags + 8/16-bit BPM + optional fields.
        if not data:
            return
        flags = data[0]
        offset = 1
        try:
            if flags & 0x01:
                if len(data) < offset + 2:
                    return
                bpm = int.from_bytes(data[offset:offset + 2], "little")
                offset += 2
            else:
                if len(data) < offset + 1:
                    return
                bpm = data[offset]
                offset += 1

            if flags & 0x08:  # Energy Expended
                if len(data) < offset + 2:
                    return
                offset += 2

            rr_intervals = []
            if flags & 0x10:  # RR-Interval present
                while offset + 1 < len(data):
                    rr_raw = int.from_bytes(data[offset:offset + 2], "little")
                    rr_intervals.append(round((rr_raw / 1024.0) * 1000.0, 1))
                    offset += 2

            if 0 < bpm <= 300:
                self.bridge.hr_received.emit(bpm, rr_intervals)
        except (IndexError, ValueError, TypeError) as exc:
            print(f"[BLE HR Packet Error]: {exc!r}")

    async def _read_battery(self, client):
        try:
            raw = await client.read_gatt_char(BATTERY_LEVEL_CHAR_UUID)
            if raw and len(raw) >= 1:
                level = int(raw[0])
                if 0 <= level <= 100:
                    self.bridge.battery_received.emit(level)
                    return
            print("[BLE Battery Error]: Empty or invalid Battery Level characteristic")
            self.bridge.battery_received.emit(-1)
        except Exception as exc:
            # Some bands do not expose the standard Battery Service.
            print(f"[BLE Battery Error]: {exc!r}")
            self.bridge.battery_received.emit(-1)

    async def _battery_poll_loop(self, client):
        try:
            while self.client is client and client.is_connected:
                await self._read_battery(client)
                # Battery capacity is expected to last months; a 10-minute
                # refresh is sufficient and keeps BLE traffic low.
                await asyncio.sleep(600)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[BLE Battery Poll Error]: {exc!r}")

    async def _find_device(self, target_norm: str, force_refresh: bool = False):
        """Return a recent platform BLE handle, optionally forcing discovery."""
        cached = self.discovered_devices.get(target_norm)
        cached_at = self.discovered_at.get(target_norm, 0.0)
        if (not force_refresh and cached is not None
                and time.monotonic() - cached_at < 15.0):
            return cached

        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        async with self._scan_lock:
            cached = self.discovered_devices.get(target_norm)
            cached_at = self.discovered_at.get(target_norm, 0.0)
            if (not force_refresh and cached is not None
                    and time.monotonic() - cached_at < 15.0):
                return cached
            # Remove the target's old handle before a refresh. If it is not
            # seen this time, do not accidentally return the stale handle.
            self.discovered_devices.pop(target_norm, None)
            self.discovered_at.pop(target_norm, None)
            devices = await BleakScanner.discover(
                timeout=12.0,
                return_adv=True,
                scanning_mode="active"
            )
            scanned_at = time.monotonic()
            for device, _advertisement in devices.values():
                normalized = str(device.address).lower()
                self.discovered_devices[normalized] = device
                self.discovered_at[normalized] = scanned_at
            return self.discovered_devices.get(target_norm)


    async def _connect_with_retry(self, max_retry_sec: float = 60.0):
        if not self.target_address:
            self._set_state(
                "disconnected",
                "Select or enter the band's Bluetooth identifier first.",
                False
            )
            return

        await self._stop_battery_poll()

        # Clean up any previous connection.
        old_client = self.client
        self.client = None

        if old_client is not None:
            try:
                await old_client.disconnect()
            except Exception as exc:
                print(f"[BLE Old Client Cleanup]: {exc!r}")

        deadline = time.monotonic() + max(60.0, max_retry_sec)
        target_norm = self.target_address.strip().lower()
        attempt = 0
        last_error = "Band was not discovered nearby."

        # Progressively longer waits between attempts.
        retry_delays = (1.0, 2.0, 4.0, 6.0, 8.0)

        while time.monotonic() < deadline and not self.manual_disconnect:
            attempt += 1
            client = None

            remaining = max(0, int(deadline - time.monotonic()))

            self._set_state(
                "connecting",
                f"Connecting to {self.target_address} "
                f"(attempt {attempt}, {remaining}s remaining)...",
                False
            )

            try:
                # Always get a fresh BLE device handle before connecting.
                device = await self._find_device(
                    target_norm,
                    force_refresh=True
                )

                if device is None:
                    raise LookupError(
                        "Band not found during fresh Bluetooth scan."
                    )

                print(
                    f"[BLE] Attempt {attempt}: "
                    f"Connecting using fresh device handle {device.address}"
                )

                client = BleakClient(
                    device,
                    disconnected_callback=self._on_unexpected_disconnect,
                    timeout=20.0
                )

                await client.connect()

                if not client.is_connected:
                    raise ConnectionError(
                        "Bluetooth connection did not complete."
                    )

                # Confirm the heart-rate service is accessible.
                await client.start_notify(
                    HR_MEASUREMENT_CHAR_UUID,
                    self._notification_handler
                )

                self.client = client

                self._set_state(
                    "connected",
                    f"Connected: {self.target_address}",
                    True
                )

                print(f"[BLE] Successfully connected on attempt {attempt}")

                # Battery remains local-only.
                self.battery_task = asyncio.create_task(
                    self._battery_poll_loop(client)
                )

                return

            except asyncio.CancelledError:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                return

            except Exception as exc:
                last_error = str(exc) or repr(exc)

                print(
                    f"[BLE Connect Attempt {attempt} Failed]: "
                    f"{type(exc).__name__}: {exc!r}"
                )

                # Invalidate the cached handle so it cannot be reused.
                self.discovered_devices.pop(target_norm, None)
                self.discovered_at.pop(target_norm, None)

                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception as cleanup_exc:
                        print(
                            f"[BLE Failed Client Cleanup]: {cleanup_exc!r}"
                        )

            if self.manual_disconnect:
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            delay = retry_delays[
                min(attempt - 1, len(retry_delays) - 1)
            ]

            await asyncio.sleep(min(delay, remaining))

        if not self.manual_disconnect:
            self._set_state(
                "disconnected",
                f"Couldn't connect to {self.target_address}. "
                f"Tried for approximately {int(max_retry_sec)} seconds. "
                f"Last error: {last_error}",
                False
            )

DEFAULT_ZONE_COLORS = {
    "zone1": "#64B5F6",
    "zone2": "#00E676",
    "zone3": "#FFD54F",
    "zone4": "#FF9100",
    "zone5": "#E11D48"
}

DEFAULT_ZONE_THRESHOLDS = {
    "z1_max": 100,
    "z2_max": 130,
    "z3_max": 155,
    "z4_max": 175
}


class HeartRateMonitorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BLE Heart Rate Monitor")
        self.resize(1040, 700)

        self.config = self.load_config()

        self.bpm_history = deque()
        self.rr_history = deque()
        self.session_min = 999
        self.session_max = 0

        self.last_packet_time = 0.0
        self.is_signal_stale = True
        self.stale_reconnect_triggered = False
        self.blink_state = False

        self.is_recording = False
        self.recorded_samples = []
        self.record_start_utc = ""
        self.last_saved_session_payload = None

        self.bridge = DataBridge()
        self.bridge.hr_received.connect(self.on_hr_data)
        self.bridge.battery_received.connect(self.on_battery_received)
        self.bridge.status_changed.connect(self.on_status_change)
        self.bridge.scan_finished.connect(self.on_scan_results)

        self.ble = BLEWorker(self.bridge)
        self.ble.auto_reconnect_enabled = self.config["auto_reconnect"]

        # One persistent sender thread serializes telemetry requests and reuses
        # a requests.Session instead of creating one thread per heart-rate packet.
        self._server_queue = queue.Queue(maxsize=500)
        self._server_worker = threading.Thread(
            target=self._server_sender_loop, name="Telemetry-Sender", daemon=True
        )
        self._server_worker.start()

        self.init_ui()
        self.refresh_saved_device_combos()
        self.apply_theme()
        self.switch_page(0)

        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.timeout.connect(self.on_watchdog_tick)
        self.watchdog_timer.start(1000)

        if self.config["auto_connect_launch"] and self.config["device_address"]:
            QTimer.singleShot(500, lambda: self.ble.connect_device(self.config["device_address"], 30.0))

    def load_config(self) -> dict:
        defaults = {
            "device_address": "",
            "saved_devices": [],
            "client_name": "",
            "auto_connect_launch": True,
            "auto_reconnect": True,
            "server_url": "http://192.168.178.46/api/telemetry",
            "server_sync": False,
            "time_display_mode": "actual",
            "timezone_mode": "local",
            "details_window_sec": 300,
            "dark_mode": True,
            "accent_color": "#E11D48",
            "bg_color": "#0B1528",
            "use_zone_colors": True,
            "zone_colors": DEFAULT_ZONE_COLORS.copy(),
            "zone_thresholds": DEFAULT_ZONE_THRESHOLDS.copy()
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    defaults.update(loaded)
                    zc = DEFAULT_ZONE_COLORS.copy()
                    zc.update(loaded.get("zone_colors", {}))
                    defaults["zone_colors"] = zc

                    zt = DEFAULT_ZONE_THRESHOLDS.copy()
                    zt.update(loaded.get("zone_thresholds", {}))
                    defaults["zone_thresholds"] = zt
            except Exception:
                pass
        return defaults

    def save_config(self):
        self.config["device_address"] = self.input_mac.text().strip()
        self.config["client_name"] = self.input_client_name.text().strip()
        self.config["auto_connect_launch"] = self.chk_auto_launch.isChecked()
        self.config["auto_reconnect"] = self.chk_auto_reconnect.isChecked()
        self.ble.auto_reconnect_enabled = self.config["auto_reconnect"]

        self.config["server_url"] = self.input_server.text().strip()
        self.config["server_sync"] = self.chk_sync.isChecked()
        self.config["time_display_mode"] = "actual" if self.chk_actual_time.isChecked() else "relative"
        self.config["timezone_mode"] = "utc" if self.chk_utc_time.isChecked() else "local"
        self.config["details_window_sec"] = self.spin_history_sec.value()
        self.config["dark_mode"] = self.chk_dark_mode.isChecked()
        self.config["use_zone_colors"] = self.chk_zone_colors.isChecked()

        self.config["zone_thresholds"] = {
            "z1_max": self.spin_z1.value(),
            "z2_max": self.spin_z2.value(),
            "z3_max": self.spin_z3.value(),
            "z4_max": self.spin_z4.value()
        }
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
        sidebar.setFixedWidth(210)
        sidebar.setObjectName("Sidebar")
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(14, 24, 14, 24)
        side_layout.setSpacing(10)

        title = QLabel("HR MONITOR")
        title.setObjectName("SidebarTitle")
        title.setAlignment(Qt.AlignCenter)
        side_layout.addWidget(title)
        side_layout.addSpacing(20)

        self.nav_buttons = []
        for idx, label in enumerate(["Overview", "Detailed Graphs", "Settings"]):
            btn = QPushButton(label)
            btn.setFixedHeight(44)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked, i=idx: self.switch_page(i))
            side_layout.addWidget(btn)
            self.nav_buttons.append(btn)

        side_layout.addStretch()

        self.lbl_conn_status = QLabel("○ Disconnected")
        self.lbl_conn_status.setObjectName("SidebarStatus")
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
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(16)

        top_bar = QHBoxLayout()
        header = QLabel("Live Overview")
        header.setObjectName("PageTitle")
        top_bar.addWidget(header)
        top_bar.addStretch()

        self.combo_quick_devices = QComboBox()
        self.combo_quick_devices.setMinimumWidth(210)
        self.btn_quick_connect = QPushButton("Quick Connect")
        self.btn_quick_connect.setObjectName("PrimaryBtn")
        self.btn_quick_connect.clicked.connect(self.quick_connect_saved)
        top_bar.addWidget(QLabel("Saved Band:"))
        top_bar.addWidget(self.combo_quick_devices)
        top_bar.addWidget(self.btn_quick_connect)
        layout.addLayout(top_bar)

        rec_card = QGroupBox()
        rec_bar = QHBoxLayout(rec_card)
        rec_bar.setContentsMargins(14, 10, 14, 10)

        self.btn_record = QPushButton("● Start Recording")
        self.btn_record.setObjectName("AccentRedBtn")
        self.btn_record.setFixedHeight(36)
        self.btn_record.clicked.connect(self.toggle_recording)

        self.lbl_rec_badge = QLabel("IDLE — Ready to record")
        self.lbl_rec_badge.setFont(QFont("Segoe UI", 10, QFont.Bold))

        self.btn_upload_rec = QPushButton("↑ Upload Last Session")
        self.btn_upload_rec.setFixedHeight(36)
        self.btn_upload_rec.setEnabled(False)
        self.btn_upload_rec.clicked.connect(self.upload_last_recording)

        rec_bar.addWidget(self.btn_record)
        rec_bar.addSpacing(10)
        rec_bar.addWidget(self.lbl_rec_badge)
        rec_bar.addStretch()
        rec_bar.addWidget(self.btn_upload_rec)
        layout.addWidget(rec_card)

        hero_card = QGroupBox()
        hero_layout = QVBoxLayout(hero_card)
        hero_layout.setContentsMargins(20, 18, 20, 18)

        self.lbl_big_bpm = QLabel("-- BPM")
        self.lbl_big_bpm.setObjectName("BigBpmLabel")
        self.lbl_big_bpm.setAlignment(Qt.AlignCenter)
        hero_layout.addWidget(self.lbl_big_bpm)

        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(12)
        self.lbl_min_bpm = QLabel("MIN\n--")
        self.lbl_avg_bpm = QLabel("AVG\n--")
        self.lbl_max_bpm = QLabel("MAX\n--")
        self.lbl_zone = QLabel("ZONE\n--")

        for lbl in [self.lbl_min_bpm, self.lbl_avg_bpm, self.lbl_max_bpm, self.lbl_zone]:
            lbl.setObjectName("StatPill")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setMinimumHeight(60)
            stats_layout.addWidget(lbl)

        hero_layout.addLayout(stats_layout)
        layout.addWidget(hero_card)

        mini_axis = TimeAxisItem(
            lambda: self.config["time_display_mode"],
            lambda: self.config["timezone_mode"],
            orientation="bottom"
        )
        pg.setConfigOptions(antialias=True)
        self.mini_plot = pg.PlotWidget(title="Live Pulse Trend (Last 60s)", axisItems={"bottom": mini_axis})
        self.mini_plot.setMouseEnabled(x=False, y=False)
        self.mini_plot.hideButtons()
        self.mini_plot.showGrid(x=False, y=True, alpha=0.18)
        self.mini_curve = self.mini_plot.plot()
        layout.addWidget(self.mini_plot)

        return page

    def create_details_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(14)

        header_row = QHBoxLayout()
        self.lbl_details_header = QLabel(f"Detailed Graphs (Last {self.config['details_window_sec']}s)")
        self.lbl_details_header.setObjectName("PageTitle")
        btn_reset = QPushButton("Reset Session")
        btn_reset.setFixedWidth(135)
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
        self.detail_hr_plot.showGrid(x=True, y=True, alpha=0.2)
        self.detail_hr_plot.setLabel("left", "BPM")
        self.detail_hr_curve = self.detail_hr_plot.plot()
        layout.addWidget(self.detail_hr_plot)

        rr_axis = TimeAxisItem(
            lambda: self.config["time_display_mode"],
            lambda: self.config["timezone_mode"],
            orientation="bottom"
        )
        self.detail_rr_plot = pg.PlotWidget(title="R-R Intervals (ms) — Beat-to-Beat Timing", axisItems={"bottom": rr_axis})
        self.detail_rr_plot.showGrid(x=True, y=True, alpha=0.2)
        self.detail_rr_plot.setLabel("left", "Milliseconds")
        self.detail_rr_curve = self.detail_rr_plot.plot(
            pen=pg.mkPen(color="#0052A5", width=2), symbol="o", symbolSize=5, symbolBrush="#0052A5"
        )
        layout.addWidget(self.detail_rr_plot)

        self.lbl_raw_rr = QLabel("Latest R-R Intervals: None")
        self.lbl_raw_rr.setObjectName("MutedText")
        layout.addWidget(self.lbl_raw_rr)

        return page
    def create_settings_page(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(18)

        header = QLabel("Settings")
        header.setObjectName("PageTitle")
        layout.addWidget(header)

        # 1. Saved & Scanned Devices + Battery Level
        ble_group = QGroupBox("Bluetooth and Saved Devices")
        ble_layout = QFormLayout(ble_group)
        ble_layout.setVerticalSpacing(12)
        ble_layout.setHorizontalSpacing(16)

        saved_row = QHBoxLayout()
        self.combo_saved_devices = QComboBox()
        self.combo_saved_devices.currentIndexChanged.connect(self.on_saved_device_chosen)
        btn_del_saved = QPushButton("Remove")
        btn_del_saved.setFixedWidth(95)
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
        self.input_device_name.setPlaceholderText("Friendly Name (e.g., My Chest Strap)")
        ble_layout.addRow("Band Label:", self.input_device_name)

        self.input_mac = QLineEdit(self.config["device_address"])
        self.input_mac.setPlaceholderText("MAC (Win/Linux) or UUID (macOS)")
        self.input_mac.editingFinished.connect(self.save_config)
        self.combo_devices.currentIndexChanged.connect(self.on_device_selected)
        ble_layout.addRow("MAC / UUID:", self.input_mac)

        auto_row = QHBoxLayout()
        self.chk_auto_launch = QCheckBox("Auto-connect on launch (max 30s)")
        self.chk_auto_launch.setChecked(self.config["auto_connect_launch"])
        self.chk_auto_launch.toggled.connect(self.save_config)

        self.chk_auto_reconnect = QCheckBox("Auto-reconnect if dropped (max 30s)")
        self.chk_auto_reconnect.setChecked(self.config["auto_reconnect"])
        self.chk_auto_reconnect.toggled.connect(self.save_config)

        auto_row.addWidget(self.chk_auto_launch)
        auto_row.addWidget(self.chk_auto_reconnect)
        ble_layout.addRow("Automation:", auto_row)

        conn_btns = QHBoxLayout()
        self.btn_save_dev = QPushButton("Save Device")
        self.btn_connect = QPushButton("Connect Band")
        self.btn_connect.setObjectName("PrimaryBtn")
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_save_dev.clicked.connect(self.save_current_device)
        self.btn_connect.clicked.connect(self.connect_band)
        self.btn_disconnect.clicked.connect(self.ble.disconnect_device)
        conn_btns.addWidget(self.btn_save_dev)
        conn_btns.addWidget(self.btn_connect)
        conn_btns.addWidget(self.btn_disconnect)
        ble_layout.addRow("Actions:", conn_btns)

        self.lbl_battery = QLabel("Battery Level: Not connected")
        self.lbl_battery.setObjectName("MutedText")
        ble_layout.addRow("Band Status:", self.lbl_battery)

        layout.addWidget(ble_group)

        # 2. Time & Graph Configuration
        time_group = QGroupBox("Time Format and Graph History")
        time_layout = QFormLayout(time_group)
        time_layout.setVerticalSpacing(12)
        time_layout.setHorizontalSpacing(16)

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

        # 3. Custom Heart Rate Zone Values & Colors
        zone_group = QGroupBox("Heart Rate Zone Thresholds and Colors")
        zone_layout = QVBoxLayout(zone_group)
        zone_layout.setSpacing(12)

        self.chk_zone_colors = QCheckBox("Colorize live BPM and Zone card dynamically by HR Zone")
        self.chk_zone_colors.setChecked(self.config["use_zone_colors"])
        self.chk_zone_colors.toggled.connect(self.save_config)
        zone_layout.addWidget(self.chk_zone_colors)

        zt = self.config["zone_thresholds"]
        self.spin_z1 = QSpinBox()
        self.spin_z2 = QSpinBox()
        self.spin_z3 = QSpinBox()
        self.spin_z4 = QSpinBox()

        for spin, val in [(self.spin_z1, zt["z1_max"]), (self.spin_z2, zt["z2_max"]),
                          (self.spin_z3, zt["z3_max"]), (self.spin_z4, zt["z4_max"])]:
            spin.setRange(50, 230)
            spin.setSuffix(" BPM")
            spin.setValue(int(val))
            spin.valueChanged.connect(self.on_zone_thresholds_changed)

        self.zone_buttons = {}
        zone_rows = [
            ("zone1", "Zone 1 (Warmup) Max:", self.spin_z1),
            ("zone2", "Zone 2 (Fat Burn) Max:", self.spin_z2),
            ("zone3", "Zone 3 (Aerobic) Max:", self.spin_z3),
            ("zone4", "Zone 4 (Anaerobic) Max:", self.spin_z4),
        ]
        for z_key, label_txt, spin_widget in zone_rows:
            row = QHBoxLayout()
            lbl = QLabel(label_txt)
            lbl.setMinimumWidth(165)
            btn = QPushButton("Pick Color")
            btn.setFixedWidth(120)
            btn.clicked.connect(lambda checked, k=z_key: self.pick_zone_color(k))
            self.zone_buttons[z_key] = btn
            row.addWidget(lbl)
            row.addWidget(spin_widget)
            row.addSpacing(12)
            row.addWidget(btn)
            row.addStretch()
            zone_layout.addLayout(row)

        z5_row = QHBoxLayout()
        lbl_z5 = QLabel("Zone 5 (Max Effort):")
        lbl_z5.setMinimumWidth(165)
        self.lbl_z5_desc = QLabel(f"Above {zt['z4_max']} BPM")
        self.lbl_z5_desc.setObjectName("MutedText")
        btn_z5 = QPushButton("Pick Color")
        btn_z5.setFixedWidth(120)
        btn_z5.clicked.connect(lambda: self.pick_zone_color("zone5"))
        self.zone_buttons["zone5"] = btn_z5
        z5_row.addWidget(lbl_z5)
        z5_row.addWidget(self.lbl_z5_desc)
        z5_row.addSpacing(12)
        z5_row.addWidget(btn_z5)
        z5_row.addStretch()
        zone_layout.addLayout(z5_row)

        layout.addWidget(zone_group)

        # 4. Server Sync, Client Identity, Recordings Folder & Theme
        srv_group = QGroupBox("Server Sync, Recordings and Theme")
        srv_layout = QFormLayout(srv_group)
        srv_layout.setVerticalSpacing(12)
        srv_layout.setHorizontalSpacing(16)

        self.input_client_name = QLineEdit(self.config["client_name"])
        self.input_client_name.setPlaceholderText("e.g., Dylan (broadcasted to server & OBS overlay)")
        self.input_client_name.editingFinished.connect(self.save_config)
        srv_layout.addRow("Your Name:", self.input_client_name)

        self.input_server = QLineEdit(self.config["server_url"])
        self.input_server.editingFinished.connect(self.save_config)
        srv_layout.addRow("Telemetry URL:", self.input_server)

        sync_row = QHBoxLayout()
        self.chk_sync = QCheckBox("Stream live telemetry to server")
        self.chk_sync.setChecked(self.config["server_sync"])
        self.chk_sync.toggled.connect(self.save_config)

        btn_open_rec = QPushButton("📂 Open Recordings Folder")
        btn_open_rec.clicked.connect(self.open_recordings_folder)

        sync_row.addWidget(self.chk_sync)
        sync_row.addStretch()
        sync_row.addWidget(btn_open_rec)
        srv_layout.addRow("Storage and Sync:", sync_row)

        self.chk_dark_mode = QCheckBox("Dark Mode (Deep Space Navy & Royal Blue)")
        self.chk_dark_mode.setChecked(self.config["dark_mode"])
        self.chk_dark_mode.toggled.connect(self.on_dark_mode_toggled)
        srv_layout.addRow("Display Mode:", self.chk_dark_mode)

        vis_btns = QHBoxLayout()
        btn_accent = QPushButton("Graph / Accent Color")
        btn_accent.clicked.connect(self.pick_accent_color)
        btn_reset_colors = QPushButton("Reset Default Colors")
        btn_reset_colors.clicked.connect(self.reset_colors)
        vis_btns.addWidget(btn_accent)
        vis_btns.addWidget(btn_reset_colors)
        srv_layout.addRow("Custom Accents:", vis_btns)
        layout.addWidget(srv_group)

        layout.addStretch()
        scroll.setWidget(page)
        return scroll
    def switch_page(self, index: int):
        self.pages.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.setProperty("active", i == index)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def open_recordings_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(RECORDINGS_DIR)))

    def on_battery_received(self, level: int):
        if level < 0:
            self.lbl_battery.setText("Battery Level: Not connected / Unavailable")
            self.lbl_battery.setStyleSheet("")
        else:
            color = "#00E676" if level > 30 else "#E11D48"
            self.lbl_battery.setText(f"Battery Level: {level}%")
            self.lbl_battery.setStyleSheet(f"color: {color}; font-weight: bold;")

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
            self.ble.connect_device(addr, 60.0)

    def connect_band(self):
        self.save_config()
        self.ble.connect_device(self.input_mac.text().strip(), 60.0)

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
        color = "#00E676" if connected else "#93C5FD"
        self.lbl_conn_status.setStyleSheet(f"color: {color}; font-weight: bold;")
        scanning = msg.startswith("Scanning nearby")
        self.btn_scan.setEnabled(not scanning)
        self.btn_scan.setText("Scanning..." if scanning else "Scan Nearby")
    def on_zone_thresholds_changed(self):
        self.lbl_z5_desc.setText(f"Above {self.spin_z4.value()} BPM")
        self.save_config()

    def get_hr_zone_info(self, bpm: int) -> tuple:
        zc = self.config["zone_colors"]
        zt = self.config["zone_thresholds"]
        if bpm <= zt["z1_max"]:
            return "Zone 1 (Warmup)", zc["zone1"]
        elif bpm <= zt["z2_max"]:
            return "Zone 2 (Fat Burn)", zc["zone2"]
        elif bpm <= zt["z3_max"]:
            return "Zone 3 (Aerobic)", zc["zone3"]
        elif bpm <= zt["z4_max"]:
            return "Zone 4 (Anaerobic)", zc["zone4"]
        return "Zone 5 (Max Effort)", zc["zone5"]

    def on_time_settings_changed(self):
        self.save_config()
        self.lbl_details_header.setText(f"Detailed Graphs (Last {self.config['details_window_sec']}s)")
        self.mini_plot.getAxis("bottom").picture = None
        self.detail_hr_plot.getAxis("bottom").picture = None
        self.detail_rr_plot.getAxis("bottom").picture = None
        self.refresh_graphs()

    def on_watchdog_tick(self):
        now = time.time()
        elapsed = (now - self.last_packet_time) if self.last_packet_time > 0.0 else 999.0

        if elapsed >= 5.0:
            self.is_signal_stale = True
            self.lbl_big_bpm.setText("-- BPM")
            self.lbl_big_bpm.setStyleSheet("color: #E11D48; background-color: transparent;")
            self.lbl_zone.setText("ZONE\nSignal Lost (5s+)")
            self.lbl_zone.setStyleSheet("color: #E11D48;")

        if (
            self.last_packet_time > 0.0
            and elapsed >= 10.0
            and not self.stale_reconnect_triggered
            and self.chk_auto_reconnect.isChecked()
            and not self.ble.manual_disconnect
            and self.input_mac.text().strip()
        ):
            self.stale_reconnect_triggered = True
            self.ble.connect_device(self.input_mac.text().strip(), 30.0)

        if self.is_recording:
            self.blink_state = not self.blink_state
            dot = "🔴" if self.blink_state else "⭕"
            self.lbl_rec_badge.setText(f"{dot} RECORDING ({len(self.recorded_samples)} samples)")
            self.lbl_rec_badge.setStyleSheet("color: #E11D48; font-weight: bold;")

    def on_hr_data(self, bpm: int, rr_intervals: list):
        now_ts = time.time()
        self.last_packet_time = now_ts
        self.is_signal_stale = False
        self.stale_reconnect_triggered = False

        zone_name, zone_color = self.get_hr_zone_info(bpm)
        active_color = zone_color if self.chk_zone_colors.isChecked() else self.config["accent_color"]

        self.lbl_big_bpm.setStyleSheet(f"color: {active_color}; background-color: transparent;")
        self.lbl_zone.setStyleSheet(f"color: {active_color};" if self.chk_zone_colors.isChecked() else "")

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
        self.lbl_min_bpm.setText(f"MIN\n{self.session_min}")
        self.lbl_avg_bpm.setText(f"AVG\n{avg_bpm}")
        self.lbl_max_bpm.setText(f"MAX\n{self.session_max}")
        self.lbl_zone.setText(f"ZONE\n{zone_name}")

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
                "device_id": self.input_mac.text().strip() or "ble-hr-sensor",
                "client_name": self.input_client_name.text().strip(),
                "bpm": bpm,
                "rr_intervals_ms": rr_intervals
            }
            url = self.input_server.text().strip()
            self._queue_server_payload(url, payload)

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
        self.lbl_min_bpm.setText("MIN\n--")
        self.lbl_avg_bpm.setText("AVG\n--")
        self.lbl_max_bpm.setText("MAX\n--")
        self.mini_curve.setData([], [])
        self.detail_hr_curve.setData([], [])
        self.detail_rr_curve.setData([], [])
    def toggle_recording(self):
        if not self.is_recording:
            self.is_recording = True
            self.recorded_samples = []
            self.record_start_utc = datetime.now(timezone.utc).isoformat()
            self.btn_record.setText("■ Stop & Export Recording")
            self.lbl_rec_badge.setText("🔴 RECORDING (0 samples)")
            self.lbl_rec_badge.setStyleSheet("color: #E11D48; font-weight: bold;")
        else:
            self.is_recording = False
            self.btn_record.setText("● Start Recording")
            self.export_recording_files()

    def export_recording_files(self):
        if not self.recorded_samples:
            self.lbl_rec_badge.setText("IDLE (No samples recorded)")
            self.lbl_rec_badge.setStyleSheet("")
            return

        ended_utc = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"hr_session_{stamp}"
        device_id = self.input_mac.text().strip() or "ble-hr-sensor"
        client_name = self.input_client_name.text().strip()

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
            "client_name": client_name,
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
            f.write(f"Heart Rate Session Report: {base_name}\n")
            f.write(f"Athlete: {client_name or device_id} ({device_id})\n")
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
        self.lbl_rec_badge.setStyleSheet("color: #00E676; font-weight: bold;")

        QMessageBox.information(
            self,
            "Recording Exported",
            f"Saved 4 formats (CSV, JSON, TXT, TCX) to:\n{RECORDINGS_DIR}"
        )

    def upload_last_recording(self):
        if not self.last_saved_session_payload:
            return
        base_url = self.input_server.text().strip()
        upload_url = (
            base_url.replace("/api/telemetry", "/api/recordings")
            if base_url.endswith("/api/telemetry")
            else base_url.rstrip("/") + "/api/recordings"
        )

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
        self.btn_upload_rec.setText("↑ Upload Last Session")
        if success:
            QMessageBox.information(self, "Upload Complete", "Recording successfully uploaded to server!")
        else:
            QMessageBox.warning(self, "Upload Failed", "Could not reach server. Check server URL in Settings.")

    def _queue_server_payload(self, url: str, payload: dict):
        if not url:
            return
        try:
            self._server_queue.put_nowait((url, payload))
        except queue.Full:
            # Keep the newest live telemetry rather than allowing an unbounded backlog.
            try:
                self._server_queue.get_nowait()
                self._server_queue.task_done()
            except queue.Empty:
                pass
            try:
                self._server_queue.put_nowait((url, payload))
            except queue.Full:
                pass

    def _server_sender_loop(self):
        session = requests.Session()
        while True:
            item = self._server_queue.get()
            try:
                if item is None:
                    return
                url, payload = item
                try:
                    response = session.post(url, json=payload, timeout=3.0)
                    if not response.ok:
                        print(f"[Server Sync] HTTP {response.status_code}: {response.text[:200]}")
                except requests.RequestException as exc:
                    print(f"[Server Sync Error]: {exc!r}")
            finally:
                self._server_queue.task_done()
    def on_dark_mode_toggled(self, enabled: bool):
        self.save_config()
        self.apply_theme()

    def pick_zone_color(self, zone_key: str):
        current = self.config["zone_colors"][zone_key]
        color = QColorDialog.getColor(QColor(current), self)
        if color.isValid():
            self.config["zone_colors"][zone_key] = color.name()
            self.save_config()
            self.apply_theme()

    def pick_accent_color(self):
        color = QColorDialog.getColor(QColor(self.config["accent_color"]), self)
        if color.isValid():
            self.config["accent_color"] = color.name()
            self.save_config()
            self.apply_theme()

    def reset_colors(self):
        self.config["accent_color"] = "#E11D48"
        self.config["dark_mode"] = True
        self.chk_dark_mode.setChecked(True)
        self.config["zone_colors"] = DEFAULT_ZONE_COLORS.copy()
        self.save_config()
        self.apply_theme()

    def apply_theme(self):
        is_dark = self.config.get("dark_mode", True)
        accent = self.config["accent_color"]

        blue_main = "#1B4F9C"
        blue_bright = "#0052A5"
        red_main = "#E11D48"
        red_deep = "#D92906"

        if is_dark:
            page_bg = "#0B1528"
            card_bg = "#131B2E"
            sub_bg = "#1A243B"
            text_main = "#F1F5F9"
            text_muted = "#94A3B8"
            title_color = "#60A5FA"
            border_col = "#26334D"
            btn_bg = "#1A243B"
            btn_text = "#93C5FD"
            btn_border = "#2B3B59"
        else:
            page_bg = "#F4F6F9"
            card_bg = "#FFFFFF"
            sub_bg = "#F4F6F9"
            text_main = "#0F172A"
            text_muted = "#526075"
            title_color = "#1B4F9C"
            border_col = "#D8E0EA"
            btn_bg = "#FFFFFF"
            btn_text = "#1B4F9C"
            btn_border = "#1B4F9C"

        for z_key, btn in self.zone_buttons.items():
            z_col = self.config["zone_colors"][z_key]
            btn.setStyleSheet(f"border: 2px solid {z_col}; color: {z_col}; font-weight: bold;")

        # Red heart rate graph line & soft red area fill
        hr_graph_red = "#E11D48"
        fill_color = QColor(hr_graph_red)
        fill_color.setAlpha(45)
        fill_brush = pg.mkBrush(fill_color)

        self.mini_plot.setBackground(card_bg)
        self.detail_hr_plot.setBackground(card_bg)
        self.detail_rr_plot.setBackground(card_bg)

        self.mini_curve.setPen(pg.mkPen(color=hr_graph_red, width=3))
        self.mini_curve.setFillLevel(0)
        self.mini_curve.setBrush(fill_brush)

        self.detail_hr_curve.setPen(pg.mkPen(color=hr_graph_red, width=2.5))
        self.detail_hr_curve.setFillLevel(0)
        self.detail_hr_curve.setBrush(fill_brush)

        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background-color: {page_bg};
                color: {text_main};
                font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Arial, sans-serif;
                font-size: 13px;
            }}
            #Sidebar {{
                background-color: #0F172A;
                border-right: 3px solid {blue_main};
            }}
            #SidebarTitle {{
                background-color: {blue_main};
                color: #FFFFFF;
                font-size: 15pt;
                font-weight: 800;
                padding: 12px 8px;
                border-radius: 8px;
                border-bottom: 3px solid {red_main};
            }}
            #Sidebar QPushButton {{
                background-color: transparent;
                border: none;
                border-radius: 8px;
                padding: 10px 14px;
                text-align: left;
                color: #CBD5E1;
                font-size: 14px;
                font-weight: 600;
            }}
            #Sidebar QPushButton:hover {{
                background-color: rgba(27, 79, 156, 0.35);
                color: #FFFFFF;
            }}
            #Sidebar QPushButton[active="true"] {{
                background-color: {blue_main};
                color: #FFFFFF;
                border-left: 4px solid {red_main};
                font-weight: bold;
            }}
            #SidebarStatus {{
                background-color: transparent;
                color: #93C5FD;
                font-size: 12px;
            }}
            #PageTitle {{
                font-size: 22pt;
                font-weight: 800;
                color: {title_color};
            }}
            #BigBpmLabel {{
                font-size: 72pt;
                font-weight: 800;
                padding: 6px 0px;
                background-color: transparent;
            }}
            #MutedText {{
                color: {text_muted};
                font-family: Consolas, monospace;
                background-color: transparent;
            }}
            #StatPill {{
                background-color: {sub_bg};
                border: 1.5px solid {border_col};
                border-radius: 10px;
                font-size: 13px;
                font-weight: bold;
                color: {text_main};
                padding: 6px;
            }}
            QPushButton {{
                background-color: {btn_bg};
                border: 1.5px solid {btn_border};
                border-radius: 7px;
                padding: 7px 14px;
                color: {btn_text};
                font-weight: 700;
            }}
            QPushButton:hover {{
                background-color: {blue_main};
                color: #FFFFFF;
                border-color: {blue_main};
            }}
            QPushButton:disabled {{
                color: {text_muted};
                border-color: {border_col};
            }}
            QPushButton#PrimaryBtn {{
                background-color: {blue_main};
                color: #FFFFFF;
                border: none;
                font-weight: bold;
            }}
            QPushButton#PrimaryBtn:hover {{
                background-color: {blue_bright};
            }}
            QPushButton#AccentRedBtn {{
                background-color: {red_main};
                color: #FFFFFF;
                border: none;
                font-weight: bold;
            }}
            QPushButton#AccentRedBtn:hover {{
                background-color: {red_deep};
            }}
            QLineEdit, QComboBox, QSpinBox {{
                background-color: {sub_bg};
                border: 1.5px solid {border_col};
                border-radius: 7px;
                padding: 7px 10px;
                color: {text_main};
            }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
                border: 1.5px solid {blue_main};
            }}
            QGroupBox {{
                background-color: {card_bg};
                border: 2px solid {blue_main};
                border-radius: 10px;
                margin-top: 18px;
                padding: 22px 16px 16px 16px;
                font-weight: bold;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 14px;
                top: 4px;
                padding: 0 8px;
                color: {title_color};
            }}
            QCheckBox {{
                background-color: transparent;
                spacing: 8px;
            }}
            QLabel {{
                background-color: transparent;
            }}
        """)
        if self.is_signal_stale:
            self.lbl_big_bpm.setStyleSheet("color: #E11D48; background-color: transparent;")
        else:
            self.lbl_big_bpm.setStyleSheet(f"color: {accent}; background-color: transparent;")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = HeartRateMonitorApp()
    window.show()
    sys.exit(app.exec())
