import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI
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
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Coospo H6 Collector Server", lifespan=lifespan)


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


@app.get("/api/stats")
async def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        live_count = conn.execute("SELECT COUNT(*) as total FROM heart_rate_logs").fetchone()["total"]
        session_count = conn.execute("SELECT COUNT(*) as total FROM recorded_sessions").fetchone()["total"]

    return {"live_records": live_count, "recorded_sessions": session_count}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
