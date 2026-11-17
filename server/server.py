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

    print(f"[#{row_id}] {packet.device_id} | BPM: {packet.bpm} | RR: {packet.rr_intervals_ms}")
    return {"status": "ok", "record_id": row_id}


@app.get("/api/stats")
async def get_stats():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        count = conn.execute("SELECT COUNT(*) as total FROM heart_rate_logs").fetchone()["total"]
        latest = conn.execute("SELECT * FROM heart_rate_logs ORDER BY id DESC LIMIT 1").fetchone()

    return {
        "total_records": count,
        "latest": dict(latest) if latest else None
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
