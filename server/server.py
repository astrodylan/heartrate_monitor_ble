import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

DB_PATH = "telemetry.db"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS heart_rate_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_timestamp TEXT NOT NULL,
                server_received_at TEXT NOT NULL,
                device_id TEXT NOT NULL,
                bpm INTEGER NOT NULL,
                rr_intervals_json TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS recorded_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_name TEXT NOT NULL,
                device_id TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT NOT NULL,
                sample_count INTEGER NOT NULL,
                samples_json TEXT NOT NULL,
                uploaded_at_utc TEXT NOT NULL
            )
        """)
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

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO heart_rate_logs
            (client_timestamp, server_received_at, device_id, bpm, rr_intervals_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (packet.timestamp, received_at, packet.device_id, packet.bpm, rr_json)
        )
        conn.commit()
        row_id = cursor.lastrowid

    return {"status": "ok", "record_id": row_id}


@app.post("/api/recordings")
async def receive_recording(session: SessionUpload):
    uploaded_at = datetime.now(timezone.utc).isoformat()
    samples_json = json.dumps([s.model_dump() for s in session.samples])

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO recorded_sessions
            (session_name, device_id, started_at_utc, ended_at_utc, sample_count, samples_json, uploaded_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.session_name,
                session.device_id,
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
                """
                INSERT INTO device_aliases (device_id, friendly_name, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    friendly_name = excluded.friendly_name,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (dev_id, name, now_utc)
            )
        conn.commit()

    return {"status": "ok", "device_id": dev_id, "friendly_name": name}


@app.get("/api/devices")
async def get_devices():
    now_utc = datetime.now(timezone.utc)
    devices = []

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        alias_rows = conn.execute("SELECT device_id, friendly_name FROM device_aliases").fetchall()
        aliases = {r["device_id"]: r["friendly_name"] for r in alias_rows}

        rows = conn.execute("""
            SELECT 
                h.device_id,
                h.bpm,
                h.rr_intervals_json,
                h.server_received_at,
                stats.total_packets,
                stats.min_bpm,
                stats.max_bpm
            FROM heart_rate_logs h
            INNER JOIN (
                SELECT 
                    device_id,
                    MAX(id) AS max_id,
                    COUNT(*) AS total_packets,
                    MIN(bpm) AS min_bpm,
                    MAX(bpm) AS max_bpm
                FROM heart_rate_logs
                GROUP BY device_id
            ) stats ON h.id = stats.max_id
            ORDER BY h.server_received_at DESC
        """).fetchall()

        sessions = conn.execute("""
            SELECT id, session_name, device_id, started_at_utc, ended_at_utc, sample_count, uploaded_at_utc
            FROM recorded_sessions
            ORDER BY id DESC
            LIMIT 25
        """).fetchall()

    for r in rows:
        last_dt = datetime.fromisoformat(r["server_received_at"])
        seconds_ago = max(0, int((now_utc - last_dt).total_seconds()))
        dev_id = r["device_id"]
        devices.append({
            "device_id": dev_id,
            "friendly_name": aliases.get(dev_id, ""),
            "bpm": r["bpm"],
            "rr_intervals": json.loads(r["rr_intervals_json"] or "[]"),
            "last_seen_utc": r["server_received_at"],
            "seconds_ago": seconds_ago,
            "is_online": seconds_ago <= 10,
            "total_packets": r["total_packets"],
            "min_bpm": r["min_bpm"],
            "max_bpm": r["max_bpm"]
        })

    session_list = []
    for s in sessions:
        d = dict(s)
        d["friendly_name"] = aliases.get(d["device_id"], "")
        session_list.append(d)

    return {
        "server_time_utc": now_utc.isoformat(),
        "aliases": aliases,
        "devices": devices,
        "sessions": session_list
    }


@app.get("/api/stats")
async def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        live_count = conn.execute("SELECT COUNT(*) as total FROM heart_rate_logs").fetchone()["total"]
        session_count = conn.execute("SELECT COUNT(*) as total FROM recorded_sessions").fetchone()["total"]

    return {"live_records": live_count, "recorded_sessions": session_count}
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
            --white: #FFFFFF;
            --light-gray: #F4F6F9;
            --border: #D8E0EA;
            --muted: #526075;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--light-gray);
            color: var(--navy-slate);
            font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }
        .header {
            background: linear-gradient(110deg, var(--blue-main) 0%, var(--blue-bright) 72%, var(--red-deep) 100%);
            color: var(--white);
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 36px;
            border-bottom: 4px solid var(--red-main);
            gap: 12px;
            flex-wrap: wrap;
            box-shadow: 0 4px 14px rgba(11, 21, 40, 0.2);
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
            background: var(--white);
            color: var(--blue-main);
            border: 1.5px solid var(--blue-main);
            border-radius: 7px;
            padding: 8px 15px;
            font-size: 13px;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.15s ease;
        }
        button:hover {
            background: var(--blue-main);
            color: var(--white);
        }
        .header button {
            background: var(--red-main);
            color: var(--white);
            border: 1.5px solid rgba(255, 255, 255, 0.35);
        }
        .header button:hover {
            background: var(--red-deep);
            border-color: var(--white);
        }
        button.primary {
            background: var(--blue-main);
            color: var(--white);
            border: none;
        }
        button.primary:hover {
            background: var(--blue-bright);
        }
        button.danger {
            background: var(--red-main);
            color: var(--white);
            border: none;
            padding: 4px 10px;
            font-size: 12px;
        }
        button.danger:hover {
            background: var(--red-deep);
        }
        .settings-panel {
            display: none;
            background: var(--white);
            border: 2px solid var(--blue-main);
            border-radius: 10px;
            padding: 20px 24px;
            margin-bottom: 28px;
            box-shadow: 0 6px 18px rgba(11, 21, 40, 0.08);
        }
        .settings-panel.open { display: block; }
        .settings-panel h2 { color: var(--blue-main); font-size: 17px; margin-bottom: 4px; }
        .settings-panel .sub { color: var(--muted); font-size: 13px; }
        .settings-form {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin: 14px 0 16px 0;
        }
        input {
            background: var(--light-gray);
            border: 1.5px solid var(--border);
            border-radius: 7px;
            padding: 8px 12px;
            color: var(--navy-slate);
            font-size: 13px;
            min-width: 240px;
        }
        input:focus {
            outline: none;
            border-color: var(--blue-main);
            background: var(--white);
        }
        .alias-list { display: flex; flex-wrap: wrap; gap: 8px; }
        .alias-chip {
            background: var(--light-gray);
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
            color: var(--blue-main);
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
            background: var(--white);
            border: 1.5px solid var(--border);
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 4px 12px rgba(11, 21, 40, 0.06);
        }
        .card.online {
            border: 2px solid var(--blue-main);
        }
        .card-top {
            background: var(--navy-slate);
            color: var(--white);
            padding: 14px 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 10px;
        }
        .card.online .card-top {
            background: linear-gradient(90deg, var(--blue-main) 0%, var(--blue-bright) 100%);
        }
        .dev-title { font-weight: 800; font-size: 16px; color: var(--white); }
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
        .badge.live {
            background: var(--red-main);
            color: var(--white);
            box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.25);
        }
        .badge.offline {
            background: rgba(255, 255, 255, 0.15);
            color: var(--white);
        }
        .bpm {
            font-size: 50px;
            font-weight: 800;
            color: var(--red-main);
            line-height: 1.1;
            padding: 18px 20px 8px 20px;
        }
        .card:not(.online) .bpm { color: var(--muted); }
        .meta {
            color: var(--navy-slate);
            font-size: 13px;
            line-height: 1.7;
            padding: 12px 20px;
            background: var(--light-gray);
            border-top: 1px solid var(--border);
        }
        .card > div:last-child {
            padding: 12px 20px 16px 20px;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            background: var(--white);
            border: 2px solid var(--blue-main);
            border-radius: 10px;
            overflow: hidden;
            box-shadow: 0 4px 12px rgba(11, 21, 40, 0.06);
        }
        th, td {
            padding: 13px 16px;
            text-align: left;
            border-bottom: 1px solid var(--border);
            font-size: 13px;
        }
        th {
            background: var(--blue-main);
            color: var(--white);
            font-weight: 700;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 3px solid var(--red-main);
        }
        tr:nth-child(even) td { background: var(--light-gray); }
        .empty {
            color: var(--muted);
            padding: 24px;
            text-align: center;
            background: var(--white);
            border: 1.5px solid var(--border);
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
            <span class="sub">Auto-refreshes every 2s</span>
            <button onclick="toggleSettings()">⚙️ Settings & Device Names</button>
        </div>
    </div>

    <div class="main-wrap">
        <div class="settings-panel" id="settings-panel">
            <h2>Map Device ID / MAC to Friendly Name</h2>
            <div class="sub">Assign a person's name or label to any sensor ID so it appears across live cards and recorded sessions.</div>
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

    <div class="footer">BLE Heart Rate Collector Server</div>
    <script>
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

        async function refreshDashboard() {
            try {
                const res = await fetch('/api/devices');
                const data = await res.json();

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
                            <span><strong>${name}</strong> <span class="dev-sub">(${id})</span></span>
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
                            <div style="margin-top:12px;">
                                <button onclick="prefillAlias('${d.device_id}', '${d.friendly_name.replace(/'/g, "\\'")}')">✏️ Rename</button>
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
                            ? `<strong>${s.friendly_name}</strong><br><span class="dev-sub">${s.device_id}</span>`
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
