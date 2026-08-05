from __future__ import annotations

import csv
import io
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).parent
SAMPLE_PATH = ROOT / "data" / "sample_conversations.csv"
FEEDBACK_PATH = ROOT / "data" / "review_feedback.jsonl"

app = FastAPI(title="AI 客服质检 Copilot")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

RULES = [
    ("越权承诺", "P0", ["一定今天到", "保证今天到", "肯定能到", "无条件退款", "马上赔偿"]),
    ("售后退款", "P1", ["不能退款", "不支持退款", "拒绝退款", "退款不了"]),
    ("履约发货", "P1", ["没发货", "发不了", "缺货", "延迟发货", "物流停滞"]),
    ("情绪升级", "P0", ["投诉", "平台介入", "曝光", "骗子", "太差了", "气死了"]),
    ("服务规范缺失", "P2", ["自己看", "不知道", "别问了", "随便", "不归我管"]),
]


class Review(BaseModel):
    status: Literal["accepted", "rejected", "edited"]
    note: str = ""
    labels: list[str] = []


def load_rows(text: str | None = None) -> list[dict[str, str]]:
    content = text if text is not None else SAMPLE_PATH.read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(content)))


def classify(row: dict[str, str]) -> dict:
    dialogue = row.get("dialogue", "")
    hits = []
    for label, priority, keywords in RULES:
        evidence = next((line for line in dialogue.splitlines() if any(word in line for word in keywords)), None)
        if evidence:
            hits.append({"label": label, "priority": priority, "evidence": evidence.strip()})
    priority = min((item["priority"] for item in hits), default="P2")
    labels = [item["label"] for item in hits]
    confidence = min(0.96, round(0.63 + len(hits) * 0.12 + (0.08 if priority == "P0" else 0), 2)) if hits else 0.28
    action = {
        "P0": "立即转人工复核；当日回看话术与升级路径。",
        "P1": "在 24 小时内核对订单/售后政策，并向客服补充标准话术。",
        "P2": "纳入周度质检复盘，补齐服务规范与培训案例。",
    }[priority]
    return {
        **row,
        "labels": labels or ["未命中风险"],
        "priority": priority if hits else "—",
        "confidence": confidence,
        "evidence": hits,
        "suggestion": action if hits else "未发现明确风险；可按抽检策略复核。",
        "review_status": "pending",
    }


def dashboard(rows: list[dict]) -> dict:
    flagged = [row for row in rows if row["priority"] != "—"]
    labels = Counter(label for row in flagged for label in row["labels"])
    priorities = Counter(row["priority"] for row in flagged)
    return {
        "total": len(rows),
        "flagged": len(flagged),
        "p0": priorities["P0"],
        "pending": len(flagged),
        "top_labels": [{"label": key, "count": value} for key, value in labels.most_common(3)],
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/insights", response_class=HTMLResponse)
def insights_page() -> str:
    return (ROOT / "static" / "insights.html").read_text(encoding="utf-8")


@app.get("/api/conversations")
def conversations() -> dict:
    results = [classify(row) for row in load_rows()]
    return {"summary": dashboard(results), "items": results}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "请上传 CSV 文件")
    text = (await file.read()).decode("utf-8-sig")
    rows = load_rows(text)
    required = {"conversation_id", "date", "agent", "customer", "dialogue"}
    if not rows or not required.issubset(rows[0]):
        raise HTTPException(400, "CSV 缺少必填列：conversation_id、date、agent、customer、dialogue")
    results = [classify(row) for row in rows]
    return {"summary": dashboard(results), "items": results}


@app.post("/api/conversations/{conversation_id}/review")
def review(conversation_id: str, payload: Review) -> dict:
    FEEDBACK_PATH.parent.mkdir(exist_ok=True)
    record = {"conversation_id": conversation_id, **payload.model_dump(), "reviewed_at": datetime.now(timezone.utc).isoformat()}
    with FEEDBACK_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "record": record}
