import sqlite3
from datetime import datetime, timezone
from config import DB_PATH

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                content_id TEXT PRIMARY KEY,
                creator_id TEXT,
                timestamp TEXT,
                attribution TEXT,
                confidence REAL,
                llm_score REAL,
                stylometry_score REAL,
                status TEXT,
                appeal_reasoning TEXT,
                appeal_timestamp TEXT
            )
        """)

def log_event(entry):
    """Log a classification event."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO audit_log 
               (content_id, creator_id, timestamp, attribution, confidence, llm_score, 
                stylometry_score, status, appeal_reasoning, appeal_timestamp)
               VALUES (:content_id, :creator_id, :timestamp, :attribution, :confidence, 
                       :llm_score, :stylometry_score, :status, :appeal_reasoning, :appeal_timestamp)""",
            {
                **entry,
                "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
                "llm_score": entry.get("llm_score"),
                "stylometry_score": entry.get("stylometry_score"),
                "appeal_reasoning": None,
                "appeal_timestamp": None
            },
        )

def update_appeal(content_id, appeal_reasoning):
    """Update a record with an appeal."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """UPDATE audit_log 
               SET status = 'under_review', appeal_reasoning = ?, appeal_timestamp = ?
               WHERE content_id = ?""",
            (appeal_reasoning, datetime.now(timezone.utc).isoformat() + "Z", content_id)
        )

def read_log(limit=20):
    """Read the most recent audit log entries."""
    if limit is None:
        limit = -1  # SQLite: -1 means no limit
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]

def get_entry(content_id):
    """Get a specific audit log entry by content_id."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM audit_log WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None