import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

import os
import socket


DB_PATH = "telemetry.db"


def get_server_base_url(port: int = 8000) -> str:
    # 1. If deployed online with a domain/reverse proxy, set PUBLIC_URL="https://yourdomain.nl"
    env_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    if env_url:
        return env_url

    # 2. Otherwise auto-detect the machine's real LAN / external network IP
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            real_ip = s.getsockname()[0]
            return f"http://{real_ip}:{port}"
    except Exception:
        return f"http://127.0.0.1:{port}"



def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS heart_rate_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_timestamp TEXT NOT NULL,
                server_received_at TEXT NOT NULL,
                device_id TEXT NOT NULL,
                client_name TEXT DEFAULT '',
                bpm INTEGER NOT NULL,
                rr_intervals_json TEXT
            )
        """)
        # Safely migrate existing databases that don't have client_name yet
        try:
            conn.execute("ALTER TABLE heart_rate_logs ADD COLUMN client_name TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS recorded_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                device_id TEXT NOT NULL,
                client_name TEXT DEFAULT '',
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                samples_json TEXT NOT NULL,
                uploaded_at_utc TEXT NOT NULL
            )
        """)
        try:
            conn.execute("ALTER TABLE recorded_sessions ADD COLUMN client_name TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass

        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_aliases (
                device_id TEXT PRIMARY KEY,
                friendly_name TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            )
        """)
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BLE Heart Rate Collector Server", lifespan=lifespan)


class HeartRatePacket(BaseModel):
    timestamp: str
    device_id: str
    client_name: Optional[str] = ""
    bpm: int
    rr_intervals_ms: Optional[List[float]] = []


class RecordedSample(BaseModel):
    timestamp_utc: str
    timestamp_local: str
    bpm: int
    rr_intervals_ms: List[float]


class SessionUpload(BaseModel):
    session_name: str
    device_id: str
    client_name: Optional[str] = ""
    started_at_utc: str
    ended_at_utc: str
    samples: List[RecordedSample]


class AliasUpdate(BaseModel):
    device_id: str
    friendly_name: str


@app.post("/api/telemetry")
async def receive_telemetry(packet: HeartRatePacket):
    received_at = datetime.now(timezone.utc).isoformat()
    rr_json = json.dumps(packet.rr_intervals_ms or [])
    c_name = (packet.client_name or "").strip()

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO heart_rate_logs
            (client_timestamp, server_received_at, device_id, client_name, bpm, rr_intervals_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (packet.timestamp, received_at, packet.device_id, c_name, packet.bpm, rr_json)
        )
        conn.commit()
        row_id = cursor.lastrowid

    return {"status": "ok", "record_id": row_id}


@app.post("/api/recordings")
async def receive_recording(session: SessionUpload):
    uploaded_at = datetime.now(timezone.utc).isoformat()
    samples_json = json.dumps([s.model_dump() for s in session.samples])
    c_name = (session.client_name or "").strip()

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO recorded_sessions
            (session_name, device_id, client_name, started_at_utc, ended_at_utc, sample_count, samples_json, uploaded_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_name,
                session.device_id,
                c_name,
                session.started_at_utc,
                session.ended_at_utc,
                len(session.samples),
                samples_json,
                uploaded_at
            )
        )
        conn.commit()
        session_id = cursor.lastrowid

    print(f"[Session #{session_id}] Uploaded '{session.session_name}' ({len(session.samples)} samples)")
    return {"status": "ok", "session_id": session_id, "samples_saved": len(session.samples)}
@app.get("/api/devices")
async def get_devices():
    now_utc = datetime.now(timezone.utc)
    cutoff_5m = (now_utc - timedelta(minutes=5)).isoformat()
    devices = []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        alias_rows = conn.execute("SELECT device_id, friendly_name FROM device_aliases").fetchall()
        aliases = {r["device_id"]: r["friendly_name"] for r in alias_rows}

        rows = conn.execute("""
            SELECT 
                COALESCE(NULLIF(TRIM(h.client_name), ''), h.device_id) AS identity_key,
                h.device_id,
                h.client_name,
                h.bpm,
                h.rr_intervals_json,
                h.server_received_at,
                stats.total_packets,
                stats.min_bpm,
                stats.max_bpm
            FROM heart_rate_logs h
            INNER JOIN (
                SELECT 
                    COALESCE(NULLIF(TRIM(client_name), ''), device_id) AS id_key,
                    MAX(id) AS max_id,
                    COUNT(*) AS total_packets,
                    MIN(bpm) AS min_bpm,
                    MAX(bpm) AS max_bpm
                FROM heart_rate_logs
                GROUP BY id_key
            ) stats ON h.id = stats.max_id
            ORDER BY h.server_received_at DESC
        """).fetchall()

        sessions = conn.execute("""
            SELECT id, session_name, device_id, client_name, started_at_utc, ended_at_utc, sample_count, uploaded_at_utc
            FROM recorded_sessions
            ORDER BY id DESC
            LIMIT 25
        """).fetchall()

        for r in rows:
            last_dt = datetime.fromisoformat(r["server_received_at"])
            seconds_ago = max(0, int((now_utc - last_dt).total_seconds()))
            id_key = r["identity_key"]
            dev_id = r["device_id"]
            c_name = (r["client_name"] or "").strip()

            hist_rows = conn.execute(
                """
                SELECT bpm FROM heart_rate_logs
                WHERE COALESCE(NULLIF(TRIM(client_name), ''), device_id) = ?
                  AND server_received_at >= ?
                ORDER BY id ASC
                """,
                (id_key, cutoff_5m)
            ).fetchall()

            # Priority: 1. Server alias override, 2. Client-broadcasted name, 3. Empty (falls back to device_id)
            display_alias = aliases.get(id_key) or aliases.get(dev_id) or c_name

            devices.append({
                "device_id": dev_id,
                "identity_key": id_key,
                "friendly_name": display_alias,
                "bpm": r["bpm"],
                "rr_intervals": json.loads(r["rr_intervals_json"] or "[]"),
                "last_seen_utc": r["server_received_at"],
                "seconds_ago": seconds_ago,
                "is_online": seconds_ago <= 10,
                "total_packets": r["total_packets"],
                "min_bpm": r["min_bpm"],
                "max_bpm": r["max_bpm"],
                "history_5m": [hr["bpm"] for hr in hist_rows]
            })

    session_list = []
    for s in sessions:
        d = dict(s)
        c_name = (d.get("client_name") or "").strip()
        d["friendly_name"] = aliases.get(c_name) or aliases.get(d["device_id"]) or c_name
        session_list.append(d)

    return {
        "server_time_utc": now_utc.isoformat(),
        "server_url": get_server_base_url(),
        "aliases": aliases,
        "devices": devices,
        "sessions": session_list
    }


@app.delete("/api/devices/{identity_key:path}")
async def delete_device(identity_key: str):
    key = identity_key.strip()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM heart_rate_logs WHERE COALESCE(NULLIF(TRIM(client_name), ''), device_id) = ? OR device_id = ?",
            (key, key)
        )
        conn.execute("DELETE FROM device_aliases WHERE device_id = ?", (key,))
        conn.commit()
    return {"status": "ok", "deleted": key}
@app.post("/api/aliases")
async def save_alias(alias: AliasUpdate):
    dev_id = alias.device_id.strip()
    name = alias.friendly_name.strip()
    now_utc = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        if not name:
            conn.execute("DELETE FROM device_aliases WHERE device_id = ?", (dev_id,))
        else:
            conn.execute(
                "INSERT INTO device_aliases (device_id, friendly_name, updated_at_utc) VALUES (?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET friendly_name = excluded.friendly_name, updated_at_utc = excluded.updated_at_utc",
                (dev_id, name, now_utc)
            )
        conn.commit()
    return {"status": "ok", "device_id": dev_id, "friendly_name": name}


@app.get("/api/stats")
async def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        live_count = conn.execute("SELECT COUNT(*) as total FROM heart_rate_logs").fetchone()["total"]
        session_count = conn.execute("SELECT COUNT(*) as total FROM recorded_sessions").fetchone()["total"]

    return {"live_records": live_count, "recorded_sessions": session_count}

@app.get("/api/history")
async def get_all_history():
    now_utc = datetime.now(timezone.utc)
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        alias_rows = conn.execute("SELECT device_id, friendly_name FROM device_aliases").fetchall()
        aliases = {r["device_id"]: r["friendly_name"] for r in alias_rows}

        rows = conn.execute("""
            SELECT 
                COALESCE(NULLIF(TRIM(client_name), ''), device_id) AS identity_key,
                device_id,
                client_name,
                server_received_at,
                bpm
            FROM heart_rate_logs
            ORDER BY id ASC
        """).fetchall()

    series_map = {}
    for r in rows:
        id_key = r["identity_key"]
        dev_id = r["device_id"]
        c_name = (r["client_name"] or "").strip()
        label = aliases.get(id_key) or aliases.get(dev_id) or c_name or dev_id

        if id_key not in series_map:
            series_map[id_key] = {
                "identity_key": id_key,
                "label": label,
                "timestamps_utc": [],
                "bpm": []
            }
        series_map[id_key]["timestamps_utc"].append(r["server_received_at"])
        series_map[id_key]["bpm"].append(r["bpm"])

    return {
        "server_time_utc": now_utc.isoformat(),
        "series": list(series_map.values())
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BLE Heart Rate Collector</title>
    <style>
        :root {
            --blue-main: #1B4F9C;
            --blue-bright: #0052A5;
            --red-main: #E11D48;
            --red-deep: #D92906;
            --navy-dark: #0B1528;
            --navy-slate: #0F172A;

            --page-bg: #F4F6F9;
            --surface-bg: #FFFFFF;
            --sub-surface: #F4F6F9;
            --text-main: #0F172A;
            --text-muted: #526075;
            --section-color: #1B4F9C;
            --border-color: #D8E0EA;
            --card-border: #1B4F9C;
            --btn-bg: #FFFFFF;
            --btn-text: #1B4F9C;
            --btn-border: #1B4F9C;
        }
        body.dark-mode {
            --page-bg: #0B1528;
            --surface-bg: #131B2E;
            --sub-surface: #1A243B;
            --text-main: #F1F5F9;
            --text-muted: #94A3B8;
            --section-color: #60A5FA;
            --border-color: #26334D;
            --card-border: #1B4F9C;
            --btn-bg: #1A243B;
            --btn-text: #93C5FD;
            --btn-border: #2B3B59;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--page-bg);
            color: var(--text-main);
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            transition: background-color 0.2s, color 0.2s;
        }
        .header {
            background: linear-gradient(110deg, var(--blue-main) 0%, var(--blue-bright) 72%, var(--red-deep) 100%);
            color: #FFFFFF;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 36px;
            border-bottom: 4px solid var(--red-main);
            gap: 12px;
            flex-wrap: wrap;
            box-shadow: 0 4px 14px rgba(11, 21, 40, 0.25);
        }
        .header h1 { font-size: 22px; font-weight: 800; letter-spacing: 0.3px; }
        .header .sub { color: rgba(255, 255, 255, 0.88); font-size: 13px; margin-top: 3px; }
        .header-right { display: flex; align-items: center; gap: 14px; }
        .main-wrap {
            padding: 28px 36px;
            flex: 1;
            max-width: 1360px;
            width: 100%;
            margin: 0 auto;
        }
        button {
            background: var(--btn-bg);
            color: var(--btn-text);
            border: 1.5px solid var(--btn-border);
            border-radius: 7px;
            padding: 8px 15px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        button:hover {
            background: var(--blue-main);
            color: #FFFFFF;
            border-color: var(--blue-main);
        }
        .header button {
            background: var(--red-main);
            color: #FFFFFF;
            border: 1.5px solid rgba(255, 255, 255, 0.35);
        }
        .header button:hover { background: var(--red-deep); border-color: #FFFFFF; }
        button.primary { background: var(--blue-main); color: #FFFFFF; border: none; }
        button.primary:hover { background: var(--blue-bright); }
        button.danger {
            background: var(--red-main);
            color: #FFFFFF;
            border: none;
            padding: 4px 10px;
            font-size: 12px;
        }
        button.danger:hover { background: var(--red-deep); }
        .settings-panel {
            display: none;
            background: var(--surface-bg);
            border: 2px solid var(--blue-main);
            border-radius: 10px;
            padding: 20px 24px;
            margin-bottom: 28px;
            box-shadow: 0 6px 18px rgba(11, 21, 40, 0.15);
        }
        .settings-panel.open { display: block; }
        .settings-top {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 6px;
        }
        .settings-panel h2 { color: var(--section-color); font-size: 17px; }
        .settings-panel .sub { color: var(--text-muted); font-size: 13px; }
        .theme-toggle {
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--sub-surface);
            border: 1px solid var(--border-color);
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
        }
        .theme-toggle input[type="checkbox"] {
            min-width: auto;
            width: 16px;
            height: 16px;
            accent-color: var(--blue-main);
            cursor: pointer;
        }
        .settings-form { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0 16px 0; }
        input[type="text"] {
            background: var(--sub-surface);
            border: 1.5px solid var(--border-color);
            border-radius: 7px;
            padding: 8px 12px;
            color: var(--text-main);
            font-size: 13px;
            min-width: 240px;
        }
        input[type="text"]:focus { outline: none; border-color: var(--blue-main); background: var(--surface-bg); }
        .alias-list { display: flex; flex-wrap: wrap; gap: 8px; }
        .alias-chip {
            background: var(--sub-surface);
            border: 1.5px solid var(--blue-main);
            border-radius: 7px;
            padding: 6px 12px;
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 13px;
        }
        .section-title {
            font-size: 16px;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            color: var(--section-color);
            margin-bottom: 16px;
            padding: 6px 0 6px 12px;
            border-left: 5px solid var(--red-main);
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 18px;
            margin-bottom: 36px;
        }
        .card {
            background: var(--surface-bg);
            border: 1.5px solid var(--border-color);
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 4px 12px rgba(11, 21, 40, 0.12);
        }
        .card.online { border: 2px solid var(--card-border); }
        .card-top {
            background: var(--navy-slate);
            color: #FFFFFF;
            padding: 14px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
        }
        .card.online .card-top { background: linear-gradient(90deg, var(--blue-main) 0%, var(--blue-bright) 100%); }
        .dev-title { font-weight: 800; font-size: 16px; color: #FFFFFF; }
        .dev-sub { font-size: 11px; color: rgba(255, 255, 255, 0.8); font-family: Consolas, monospace; margin-top: 2px; }
        .badge {
            font-size: 11px;
            font-weight: 800;
            padding: 4px 10px;
            border-radius: 999px;
            text-transform: uppercase;
            letter-spacing: 0.4px;
            white-space: nowrap;
        }
        .badge.live { background: var(--red-main); color: #FFFFFF; box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.25); }
        .badge.offline { background: rgba(255, 255, 255, 0.15); color: #FFFFFF; }
        .bpm { font-size: 50px; font-weight: 800; color: var(--red-main); line-height: 1.1; padding: 18px 20px 8px 20px; }
        .card:not(.online) .bpm { color: var(--text-muted); }
        .meta {
            color: var(--text-muted);
            font-size: 13px;
            line-height: 1.7;
            padding: 12px 20px;
            background: var(--sub-surface);
            border-top: 1px solid var(--border-color);
        }
        .card > div:last-child { padding: 12px 20px 16px 20px; }
        table {
            width: 100%;
            border-collapse: collapse;
            background: var(--surface-bg);
            border: 2px solid var(--card-border);
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 4px 12px rgba(11, 21, 40, 0.12);
        }
        th, td { padding: 13px 16px; text-align: left; border-bottom: 1px solid var(--border-color); font-size: 13px; }
        th {
            background: var(--blue-main);
            color: #FFFFFF;
            font-weight: 700;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 3px solid var(--red-main);
        }
        tr:nth-child(even) td { background: var(--sub-surface); }
        .empty {
            color: var(--text-muted);
            padding: 24px;
            text-align: center;
            background: var(--surface-bg);
            border: 1.5px solid var(--border-color);
            border-radius: 8px;
        }
        .footer {
            background: var(--navy-dark);
            color: rgba(255, 255, 255, 0.8);
            text-align: center;
            padding: 14px;
            font-size: 12px;
            border-top: 3px solid var(--red-main);
            margin-top: auto;
        }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>BLE Heart Rate Collector</h1>
            <div class="sub" id="status-line">Connecting to server...</div>
        </div>
        <div class="header-right">
            <span class="sub">Auto-refreshes every 1s</span>
            <button onclick="toggleSettings()">⚙️ Settings & Device Names</button>
            <button onclick="window.location.href='/graphs'">📈 Live Graphs</button>
        </div>
    </div>


    <div class="main-wrap">
        <div class="settings-panel" id="settings-panel">
            <div class="settings-top">
                <div>
                    <h2>Dashboard Settings & Device Names</h2>
                    <div class="sub">Assign friendly names to sensor IDs or switch between Light and Dark theme.</div>
                </div>
                <label class="theme-toggle">
                    <input type="checkbox" id="dark-mode-toggle" onchange="toggleDarkMode(this.checked)">
                    <span>🌙 Dark Mode</span>
                </label>
            </div>
            <div class="settings-form">
                <input type="text" id="alias-id" placeholder="Device ID / MAC (e.g. DA:F6:45:C6:6C:11)">
                <input type="text" id="alias-name" placeholder="Friendly Name (e.g. John - Chest Strap)">
                <button class="primary" onclick="saveAlias()">Save Name</button>
            </div>
            <div class="alias-list" id="alias-list"></div>
        </div>

        <h2 class="section-title">Connected & Known Devices</h2>
        <div class="grid" id="device-grid">
            <div class="empty">Waiting for devices to broadcast...</div>
        </div>

        <h2 class="section-title">Uploaded Session Recordings</h2>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Session Name</th>
                    <th>Sensor / Person</th>
                    <th>Samples</th>
                    <th>Started (UTC)</th>
                    <th>Uploaded (UTC)</th>
                </tr>
            </thead>
            <tbody id="sessions-body">
                <tr><td colspan="6" class="empty">No recorded sessions uploaded yet.</td></tr>
            </tbody>
        </table>
    </div>

    <div class="footer" style="display: flex; justify-content: center; align-items: center; gap: 18px; flex-wrap: wrap;">
        <span>BLE Heart Rate Collector Server</span>
        <span>•</span>
        <span style="display: inline-flex; align-items: center; gap: 6px;">
            Client Telemetry URL:
            <code id="client-url-text" style="background: rgba(255,255,255,0.08); color: #F1F5F9; padding: 3px 8px; border-radius: 5px; font-family: Consolas, monospace;"></code>
            <button onclick="copyUrl('client-url-text', this)" style="padding: 3px 9px; font-size: 11px;">📋 Copy</button>
        </span>
        <span style="display: inline-flex; align-items: center; gap: 6px;">
            OBS Overlay:
            <code id="obs-url-text" style="background: rgba(255,255,255,0.08); color: #F1F5F9; padding: 3px 8px; border-radius: 5px; font-family: Consolas, monospace;"></code>
            <button onclick="copyUrl('obs-url-text', this)" style="padding: 3px 9px; font-size: 11px;">📋 Copy</button>
        </span>
    </div>
    <script>

        function copyUrl(elementId, btn) {
            const text = document.getElementById(elementId).textContent;
            navigator.clipboard.writeText(text);
            const orig = btn.textContent;
            btn.textContent = '✅ Copied!';
            setTimeout(() => { btn.textContent = orig; }, 1500);
        }

        function applyStoredTheme() {
            const isDark = localStorage.getItem('ble_dashboard_dark') === 'true';
            document.body.classList.toggle('dark-mode', isDark);
            const chk = document.getElementById('dark-mode-toggle');
            if (chk) chk.checked = isDark;
        }

        function toggleDarkMode(enabled) {
            document.body.classList.toggle('dark-mode', enabled);
            localStorage.setItem('ble_dashboard_dark', enabled ? 'true' : 'false');
        }

        applyStoredTheme();

        function toggleSettings() {
            document.getElementById('settings-panel').classList.toggle('open');
        }

        function prefillAlias(devId, currentName = '') {
            const panel = document.getElementById('settings-panel');
            panel.classList.add('open');
            document.getElementById('alias-id').value = devId;
            document.getElementById('alias-name').value = currentName;
            document.getElementById('alias-name').focus();
        }

        async function saveAlias(devIdOverride = null, nameOverride = null) {
            const device_id = (devIdOverride !== null ? devIdOverride : document.getElementById('alias-id').value).trim();
            const friendly_name = (nameOverride !== null ? nameOverride : document.getElementById('alias-name').value).trim();
            if (!device_id) return;

            await fetch('/api/aliases', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ device_id, friendly_name })
            });

            if (devIdOverride === null) {
                document.getElementById('alias-id').value = '';
                document.getElementById('alias-name').value = '';
            }
            refreshDashboard();
        }
        async function deleteDevice(identityKey, displayLabel) {
            if (!confirm(`Remove "${displayLabel}" and all its live telemetry logs from the server?`)) {
                return;
            }
            await fetch(`/api/devices/${encodeURIComponent(identityKey)}`, {
                method: 'DELETE'
            });
            refreshDashboard();
        }


        async function refreshDashboard() {
            try {
                const res = await fetch('/api/devices');
                const data = await res.json();
                
                // Automatically detect the server's current URL (localhost, LAN IP, or public domain)
                const baseUrl = data.server_url || window.location.origin;
                document.getElementById('client-url-text').textContent = `${baseUrl}/api/telemetry`;
                document.getElementById('obs-url-text').textContent = `${baseUrl}/overlay`;


                const onlineCount = data.devices.filter(d => d.is_online).length;
                document.getElementById('status-line').textContent =
                    `${onlineCount} online / ${data.devices.length} total devices • Server UTC: ${data.server_time_utc.replace('T', ' ').slice(0, 19)}`;

                const aliasList = document.getElementById('alias-list');
                const entries = Object.entries(data.aliases || {});
                if (entries.length === 0) {
                    aliasList.innerHTML = '<span class="sub">No custom names saved yet. Click "Rename" on any device card below to assign one.</span>';
                } else {
                    aliasList.innerHTML = entries.map(([id, name]) => `
                        <div class="alias-chip">
                            <span><strong>${name}</strong> <span style="opacity:0.7;font-family:Consolas,monospace;font-size:12px;">(${id})</span></span>
                            <button class="danger" onclick="saveAlias('${id}', '')">Remove</button>
                        </div>
                    `).join('');
                }

                const grid = document.getElementById('device-grid');
                if (data.devices.length === 0) {
                    grid.innerHTML = '<div class="empty">No telemetry received yet. Enable "Stream live telemetry to server" in the client Settings.</div>';
                } else {
                    grid.innerHTML = data.devices.map(d => {
                        const title = d.friendly_name ? d.friendly_name : d.device_id;
                        const sub = d.friendly_name ? `<div class="dev-sub">${d.device_id}</div>` : '';
                        const safeKey = (d.identity_key || d.device_id).replace(/'/g, "\\'");
                        const safeName = (d.friendly_name || '').replace(/'/g, "\\'");
                        const safeTitle = title.replace(/'/g, "\\'");
                        return `

                        <div class="card ${d.is_online ? 'online' : ''}">
                            <div class="card-top">
                                <div>
                                    <div class="dev-title">${title}</div>
                                    ${sub}
                                </div>
                                <span class="badge ${d.is_online ? 'live' : 'offline'}">
                                    ${d.is_online ? '● LIVE' : `STALE (${d.seconds_ago}s ago)`}
                                </span>
                            </div>
                            <div class="bpm">${d.is_online ? d.bpm : '--'} <span style="font-size:18px;font-weight:600;">BPM</span></div>
                            <div class="meta">
                                Session Range: ${d.min_bpm} – ${d.max_bpm} BPM<br>
                                Latest R-R: ${d.rr_intervals.length ? d.rr_intervals.join(', ') + ' ms' : 'None'}<br>
                                Total Packets: ${d.total_packets}
                            </div>
                            <div style="margin-top:8px; display:flex; gap:8px; align-items:center;">
                                <button onclick="prefillAlias('${safeKey}', '${safeName}')">✏️ Rename</button>
                                <button class="danger" style="padding:8px 12px; font-size:13px;" onclick="deleteDevice('${safeKey}', '${safeTitle}')">🗑️ Delete</button>
                            </div>

                        </div>
                    `}).join('');
                }

                const tbody = document.getElementById('sessions-body');
                if (data.sessions.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="6" class="empty">No recorded sessions uploaded yet.</td></tr>';
                } else {
                    tbody.innerHTML = data.sessions.map(s => {
                        const who = s.friendly_name
                            ? `<strong>${s.friendly_name}</strong><br><span style="opacity:0.7;font-family:Consolas,monospace;font-size:12px;">${s.device_id}</span>`
                            : s.device_id;
                        return `
                        <tr>
                            <td>#${s.id}</td>
                            <td><strong>${s.session_name}</strong></td>
                            <td>${who}</td>
                            <td>${s.sample_count}</td>
                            <td>${s.started_at_utc.replace('T', ' ').slice(0, 19)}</td>
                            <td>${s.uploaded_at_utc.replace('T', ' ').slice(0, 19)}</td>
                        </tr>
                    `}).join('');
                }
            } catch (err) {
                document.getElementById('status-line').textContent = 'Error polling server API.';
            }
        }

        refreshDashboard();
        setInterval(refreshDashboard, 1000);
    </script>
</body>
</html>
"""
@app.get("/overlay", response_class=HTMLResponse)
async def obs_overlay():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>OBS Heart Rate Overlay</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        html, body {
            background: transparent !important;
            overflow: hidden;
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
            padding: 16px;
        }
        #overlay-container {
            display: flex;
            flex-direction: column;
            gap: 12px;
            width: 310px;
        }
        body.row-layout #overlay-container {
            flex-direction: row;
            flex-wrap: wrap;
            width: auto;
        }
        .hr-pill {
            display: flex;
            flex-direction: column;
            gap: 8px;
            background: linear-gradient(100deg, rgba(11, 21, 40, 0.92) 0%, rgba(27, 79, 156, 0.92) 100%);
            border-left: 5px solid #E11D48;
            border-radius: 10px;
            padding: 12px 16px 10px 16px;
            color: #FFFFFF;
            box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
            min-width: 270px;
        }
        .top-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
        }
        .athlete-name {
            font-size: 17px;
            font-weight: 800;
            letter-spacing: 0.3px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 155px;
        }
        .right-bpm {
            display: flex;
            align-items: baseline;
            gap: 6px;
        }
        .heart-icon {
            color: #E11D48;
            font-size: 20px;
            display: inline-block;
            animation: pulse var(--beat-duration, 0.8s) infinite ease-in-out;
        }
        .bpm-num {
            font-size: 34px;
            font-weight: 900;
            line-height: 1;
            color: #FFFFFF;
            font-variant-numeric: tabular-nums;
        }
        .bpm-unit {
            font-size: 12px;
            font-weight: 800;
            color: #FDA4AF;
        }
        .sparkline-box {
            width: 100%;
            height: 36px;
            display: block;
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            25% { transform: scale(1.28); }
            50% { transform: scale(1.05); }
        }
    </style>
</head>
<body>
    <div id="overlay-container"></div>
    <script>
        const params = new URLSearchParams(window.location.search);
        if (params.get('layout') === 'row') {
            document.body.classList.add('row-layout');
        }

        function buildSparklineSvg(points) {
            const w = 240;
            const h = 36;
            const pad = 3;

            if (!points || points.length === 0) {
                return `<svg class="sparkline-box" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"></svg>`;
            }
            if (points.length === 1) {
                const y = (h / 2).toFixed(1);
                return `<svg class="sparkline-box" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
                    <line x1="0" y1="${y}" x2="${w}" y2="${y}" stroke="#E11D48" stroke-width="2.5" stroke-linecap="round" />
                </svg>`;
            }

            const minVal = Math.min(...points);
            const maxVal = Math.max(...points);
            const span = Math.max(1, maxVal - minVal);

            const coords = points.map((val, idx) => {
                const x = ((idx / (points.length - 1)) * w).toFixed(1);
                const norm = (val - minVal) / span;
                const y = (h - pad - norm * (h - pad * 2)).toFixed(1);
                return `${x},${y}`;
            }).join(' ');

            return `<svg class="sparkline-box" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
                <polyline fill="none" stroke="#E11D48" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" points="${coords}" />
            </svg>`;
        }

        async function updateOverlay() {
            try {
                const res = await fetch('/api/devices');
                const data = await res.json();

                const active = (data.devices || []).filter(d => d.is_online);
                const container = document.getElementById('overlay-container');

                if (active.length === 0) {
                    container.innerHTML = '';
                    return;
                }

                container.innerHTML = active.map(d => {
                    const label = d.friendly_name ? d.friendly_name : d.device_id;
                    const beatSec = d.bpm > 0 ? (60 / d.bpm).toFixed(2) : '0.80';
                    const graphSvg = buildSparklineSvg(d.history_5m);
                    return `
                        <div class="hr-pill">
                            <div class="top-row">
                                <div class="athlete-name">${label}</div>
                                <div class="right-bpm">
                                    <span class="heart-icon" style="--beat-duration: ${beatSec}s;">♥</span>
                                    <span class="bpm-num">${d.bpm}</span>
                                    <span class="bpm-unit">BPM</span>
                                </div>
                            </div>
                            ${graphSvg}
                        </div>
                    `;
                }).join('');
            } catch (e) {
                // Keep last state silently on brief network hiccup
            }
        }

        updateOverlay();
        setInterval(updateOverlay, 1000);
    </script>
</body>
</html>
"""

@app.get("/graphs", response_class=HTMLResponse)
async def graphs_page():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Live Multi-Device Heart Rate Graphs</title>
    <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
    <style>
        :root {
            --blue-main: #1B4F9C;
            --blue-bright: #0052A5;
            --red-main: #E11D48;
            --red-deep: #D92906;
            --navy-dark: #0B1528;
            --page-bg: #F4F6F9;
            --surface-bg: #FFFFFF;
            --text-main: #0F172A;
            --text-muted: #526075;
            --border-color: #D8E0EA;
        }
        body.dark-mode {
            --page-bg: #0B1528;
            --surface-bg: #131B2E;
            --text-main: #F1F5F9;
            --text-muted: #94A3B8;
            --border-color: #26334D;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: var(--page-bg);
            color: var(--text-main);
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: linear-gradient(110deg, var(--blue-main) 0%, var(--blue-bright) 72%, var(--red-deep) 100%);
            color: #FFFFFF;
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 18px 36px;
            border-bottom: 4px solid var(--red-main);
            flex-wrap: wrap;
            gap: 12px;
        }
        .header h1 { font-size: 22px; font-weight: 800; }
        .header .sub { color: rgba(255,255,255,0.88); font-size: 13px; margin-top: 2px; }
        .controls { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
        button {
            background: #FFFFFF;
            color: var(--blue-main);
            border: none;
            border-radius: 7px;
            padding: 8px 14px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
        }
        button.red { background: var(--red-main); color: #FFFFFF; }
        button.red:hover { background: var(--red-deep); }
        .wrap {
            padding: 24px 36px;
            flex: 1;
            max-width: 1500px;
            width: 100%;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
        }
        .chart-card {
            background: var(--surface-bg);
            border: 2px solid var(--blue-main);
            border-radius: 12px;
            padding: 16px;
            flex: 1;
            min-height: 540px;
            box-shadow: 0 6px 18px rgba(11, 21, 40, 0.15);
        }
        #hr-chart { width: 100%; height: 540px; }
        .footer {
            background: var(--navy-dark);
            color: rgba(255,255,255,0.8);
            text-align: center;
            padding: 12px;
            font-size: 12px;
            border-top: 3px solid var(--red-main);
        }
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>Multi-Device Live Heart Rate Graphs</h1>
            <div class="sub" id="graph-status">Loading UTC telemetry stream...</div>
        </div>
        <div class="controls">
            <button onclick="resetToLast5Min()">⏱️ Zoom Last 5 Min (300s)</button>
            <button onclick="toggleTheme()">🌙 Toggle Dark/Light</button>
            <button class="red" onclick="window.location.href='/'">← Back to Dashboard</button>
        </div>
    </div>

    <div class="wrap">
        <div class="chart-card">
            <div id="hr-chart"></div>
        </div>
    </div>

    <div class="footer">UTC Time Axis • Drag to zoom any time range • Double-click chart or click "Zoom Last 5 Min" to follow live</div>
    <script>
        const PALETTE = [
            '#E11D48', '#2563EB', '#10B981', '#F59E0B', '#8B5CF6',
            '#06B6D4', '#F97316', '#EC4899', '#84CC16', '#6366F1'
        ];

        let followLast5Min = true;
        let userCustomRange = null;
        let chartInitialized = false;

        if (localStorage.getItem('ble_dashboard_dark') !== 'false') {
            document.body.classList.add('dark-mode');
        }

        function toggleTheme() {
            const isDark = document.body.classList.toggle('dark-mode');
            localStorage.setItem('ble_dashboard_dark', isDark ? 'true' : 'false');
            chartInitialized = false; // Force full style redraw on theme switch
            updateChart();
        }

        function resetToLast5Min() {
            followLast5Min = true;
            userCustomRange = null;
            updateChart();
        }

        function zoomAllHistory() {
            followLast5Min = false;
            userCustomRange = 'all';
            updateChart();
        }

        async function updateChart() {
            try {
                const res = await fetch('/api/history');
                const data = await res.json();
                const isDark = document.body.classList.contains('dark-mode');

                const serverNowMs = new Date(data.server_time_utc).getTime();
                const fiveMinAgoIso = new Date(serverNowMs - 300 * 1000).toISOString();
                const nowIso = new Date(serverNowMs + 2000).toISOString();

                const traces = (data.series || []).map((s, idx) => ({
                    x: s.timestamps_utc,
                    y: s.bpm,
                    name: s.label,
                    type: 'scatter',
                    mode: 'lines',
                    line: {
                        color: PALETTE[idx % PALETTE.length],
                        width: 2.5,
                        shape: 'spline',
                        smoothing: 0.4
                    }
                }));

                const chartDiv = document.getElementById('hr-chart');

                if (!chartInitialized) {
                    const layout = {
                        paper_bgcolor: 'rgba(0,0,0,0)',
                        plot_bgcolor: 'rgba(0,0,0,0)',
                        font: {
                            family: 'Segoe UI, sans-serif',
                            color: isDark ? '#F1F5F9' : '#0F172A'
                        },
                        margin: { l: 55, r: 25, t: 25, b: 95 },
                        hovermode: 'x unified',
                        dragmode: 'pan',
                        showlegend: true,
                        legend: {
                            orientation: 'h',
                            yanchor: 'top',
                            y: -0.22,
                            xanchor: 'center',
                            x: 0.5,
                            bgcolor: isDark ? 'rgba(19, 27, 46, 0.85)' : 'rgba(255, 255, 255, 0.9)',
                            bordercolor: isDark ? '#26334D' : '#D8E0EA',
                            borderwidth: 1,
                            font: { size: 13 }
                        },

                        xaxis: {
                            title: 'Time (UTC)',
                            type: 'date',
                            tickformat: '%H:%M:%S',
                            gridcolor: isDark ? '#26334D' : '#E2E8F0',
                            zeroline: false,
                            range: [fiveMinAgoIso, nowIso]
                        },
                        yaxis: {
                            title: 'Heart Rate (BPM)',
                            gridcolor: isDark ? '#26334D' : '#E2E8F0',
                            zeroline: false,
                            fixedrange: true
                        }
                    };

                    await Plotly.newPlot(chartDiv, traces, layout, {
                        responsive: true,
                        displaylogo: false,
                        scrollZoom: true
                    });

                    chartDiv.on('plotly_relayout', (ev) => {
                        if (ev['xaxis.range[0]'] !== undefined && ev['xaxis.range[1]'] !== undefined) {
                            followLast5Min = false;
                            userCustomRange = [ev['xaxis.range[0]'], ev['xaxis.range[1]']];
                        } else if (ev['xaxis.autorange']) {
                            resetToLast5Min();
                        }
                    });

                    chartInitialized = true;
                } else {
                    // Update traces without resetting user pan/zoom state
                    const rangeUpdate = {};
                    if (followLast5Min) {
                        rangeUpdate['xaxis.range'] = [fiveMinAgoIso, nowIso];
                    } else if (userCustomRange === 'all') {
                        rangeUpdate['xaxis.autorange'] = true;
                    }

                    await Plotly.react(chartDiv, traces, chartDiv.layout, {
                        responsive: true,
                        displaylogo: false,
                        scrollZoom: true
                    });

                    if (Object.keys(rangeUpdate).length > 0) {
                        await Plotly.relayout(chartDiv, rangeUpdate);
                    }
                }

                const totalPts = traces.reduce((acc, t) => acc + t.y.length, 0);
                document.getElementById('graph-status').textContent =
                    `${traces.length} device(s) • ${totalPts} total datapoints • Mode: ${followLast5Min ? 'Rolling Last 5 Min (300s)' : 'Manual Pan / Zoom'}`;
            } catch (err) {
                document.getElementById('graph-status').textContent = 'Error loading graph telemetry.';
            }
        }

        updateChart();
        setInterval(updateChart, 1000);
    </script>

</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
