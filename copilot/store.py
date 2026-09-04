from __future__ import annotations

import csv
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analysis import classify

ROOT = Path(__file__).resolve().parent.parent


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    path = Path(os.getenv("QA_DB_PATH", str(ROOT / "data" / "copilot.sqlite3")))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    with database() as conn:
        for table in ("conversations", "knowledge", "notifications"):
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if conn.execute("SELECT 1 FROM settings WHERE id='initialized'").fetchone():
            return
        knowledge = json.loads((ROOT / "data" / "knowledge.json").read_text(encoding="utf-8"))
        for doc in knowledge:
            save(conn, "knowledge", doc["id"], doc)
        with (ROOT / "data" / "sample_conversations.csv").open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                item = classify(row, knowledge)
                prepare(conn, item)
                save(conn, "conversations", item["conversation_id"], item)
        conn.execute("INSERT INTO settings VALUES ('initialized','1')")


def all_records(conn, table: str) -> list[dict]:
    return [json.loads(row[0]) for row in conn.execute(f"SELECT payload FROM {table} ORDER BY rowid DESC")]


def get(conn, table: str, identifier: str) -> dict | None:
    row = conn.execute(f"SELECT payload FROM {table} WHERE id=?", (identifier,)).fetchone()
    return json.loads(row[0]) if row else None


def save(conn, table: str, identifier: str, payload: dict):
    conn.execute(f"INSERT INTO {table} (id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (identifier, json.dumps(payload, ensure_ascii=False)))


def event(item: dict, action: str, note: str):
    item.setdefault("history", []).append({"at": now(), "action": action, "note": note})


def notify(conn, item: dict, message: str):
    from uuid import uuid4
    notification = {"id": str(uuid4()), "conversation_id": item["conversation_id"], "owner": item["ticket"]["owner"], "message": message, "created_at": now(), "read": False}
    save(conn, "notifications", notification["id"], notification)


def prepare(conn, item: dict):
    item["created_at"] = now()
    item["history"] = []
    event(item, "analyzed", "已完成内容提炼、分类和风险分级")
    if item["priority"] != "—":
        hours = {"P0": 2, "P1": 24, "P2": 72}[item["priority"]]
        item["ticket"] = {
            "id": "T-" + item["conversation_id"], "owner": item["suggested_owner"],
            "status": "pending_review", "created_at": now(),
            "due_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
            "approved_plan": "", "resolution": "", "feedback": None,
        }
        event(item, "assigned", "自动分派至" + item["suggested_owner"])
        notify(conn, item, f"{item['priority']} 工单已分派，请复核处置建议")


def dashboard(rows: list[dict]) -> dict:
    from collections import Counter
    tickets = [row["ticket"] for row in rows if row.get("ticket")]
    eligible = [t for t in tickets if t["status"] != "dismissed"]
    resolved = [t for t in eligible if t["status"] == "resolved"]
    active = [t for t in eligible if t["status"] != "resolved"]
    flagged = [row for row in rows if row["priority"] != "—" and row["review_status"] != "rejected"]
    labels = Counter(label for row in flagged for label in row["labels"])
    priorities = Counter(row["priority"] for row in flagged)
    durations = [(datetime.fromisoformat(t["resolved_at"]) - datetime.fromisoformat(t["created_at"])).total_seconds() / 3600 for t in resolved]
    trend = {}
    for row in rows:
        day = row["date"]
        trend.setdefault(day, {"date": day, "total": 0, "flagged": 0})
        trend[day]["total"] += 1
        trend[day]["flagged"] += int(row["priority"] != "—" and row["review_status"] != "rejected")
    feedback_trend = {}
    for ticket in resolved:
        day = ticket.get("feedback_at", ticket["resolved_at"])[:10]
        feedback_trend.setdefault(day, {"date": day, "satisfied": 0, "neutral": 0, "dissatisfied": 0, "unknown": 0})
        feedback_trend[day][ticket["feedback"] or "unknown"] += 1
    return {
        "total": len(rows), "flagged": len(flagged), "p0": priorities["P0"],
        "pending": sum(t["status"] == "pending_review" for t in tickets),
        "active": len(active), "resolved": len(resolved), "ticket_total": len(eligible),
        "resolution_rate": round(len(resolved) / len(eligible) * 100, 1) if eligible else None,
        "avg_handling_hours": round(sum(durations) / len(durations), 2) if durations else None,
        "overdue": sum(t["due_at"] < now() for t in active),
        "top_labels": [{"label": key, "count": value} for key, value in labels.most_common()],
        "priorities": [{"label": p, "count": priorities[p]} for p in ("P0", "P1", "P2")],
        "risk_trend": [trend[day] for day in sorted(trend)],
        "feedback_trend": [feedback_trend[day] for day in sorted(feedback_trend)],
        "owners": [{"owner": owner, "active": sum(t["owner"] == owner for t in active), "resolved": sum(t["owner"] == owner for t in resolved)} for owner in sorted({t["owner"] for t in tickets})],
    }
