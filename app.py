from __future__ import annotations

import csv
import io
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from copilot.analysis import OWNERS, PRIORITIES, SERVICE_LABELS, classify
from copilot.jev import enabled as jev_enabled
from copilot.access import AccessGate, validate_access_config
from copilot.store import ROOT, all_records, dashboard, database, event, get, initialize, notify, now, prepare, save


@asynccontextmanager
async def lifespan(app):
    validate_access_config()
    await run_in_threadpool(initialize)
    yield


app = FastAPI(title="Qalio · 客诉管理 Copilot", lifespan=lifespan)
app.add_middleware(AccessGate)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/healthz", include_in_schema=False)
def health():
    return {"status": "ok"}


class Input(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


class Review(Input):
    status: Literal["accepted", "rejected", "edited"]
    note: str = Field(default="", max_length=2000)
    labels: list[str] = Field(default_factory=list)
    priority: Literal["P0", "P1", "P2"] | None = None
    plan: str = Field(default="", max_length=6000)

    @field_validator("labels")
    @classmethod
    def valid_labels(cls, labels):
        if any(label not in PRIORITIES for label in labels):
            raise ValueError("存在未知风险标签")
        return list(dict.fromkeys(labels))


class Assignment(Input):
    owner: str


class Resolution(Input):
    resolution: str = Field(min_length=5, max_length=4000)
    feedback: Literal["satisfied", "neutral", "dissatisfied", "unknown"] = "unknown"


class Feedback(Input):
    feedback: Literal["satisfied", "neutral", "dissatisfied", "unknown"]


class Knowledge(Input):
    title: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=10, max_length=6000)
    labels: list[str] = Field(min_length=1, max_length=5)
    kind: Literal["policy", "case"] = "policy"
    version: str = Field(default="v1", min_length=1, max_length=80)

    @field_validator("labels")
    @classmethod
    def valid_labels(cls, labels):
        return Review.valid_labels(labels)


def require_item(conn, identifier):
    item = get(conn, "conversations", identifier)
    if item is None:
        raise HTTPException(404, "会话不存在")
    return item


def require_ticket(item, states):
    if not item.get("ticket"):
        raise HTTPException(409, "该会话尚未生成工单")
    if item["ticket"]["status"] not in states:
        raise HTTPException(409, "当前工单状态不允许此操作，请刷新后重试")
    return item["ticket"]


@app.get("/", response_class=HTMLResponse)
@app.get("/insights", response_class=HTMLResponse)
def index():
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/conversations")
def conversations():
    with database() as conn:
        rows = all_records(conn, "conversations")
        notifications = all_records(conn, "notifications")
    rows.sort(key=lambda row: (row["priority"] == "—", row["priority"], row["conversation_id"]))
    return {"summary": dashboard(rows), "items": rows, "owners": OWNERS, "labels": list(PRIORITIES), "notifications": notifications, "jev_enabled": jev_enabled(), "llm_enabled": os.getenv("QA_ENABLE_LLM", "").lower() in {"true", "1"}}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "请上传 UTF-8 编码的 CSV 文件")
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "文件不能超过 2 MB")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), strict=True)
        required = {"conversation_id", "date", "agent", "customer", "dialogue"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV 缺少必填列：conversation_id、date、agent、customer、dialogue")
        rows = list(reader)
        if not 1 <= len(rows) <= 100:
            raise ValueError("单次请导入 1–100 条会话")
        identifiers = set()
        for row in rows:
            if None in row or any(not row.get(key) or not row[key].strip() for key in required):
                raise ValueError("会话必填字段不能为空，且每行列数须一致")
            for key in required:
                row[key] = row[key].strip()
            identifier = row["conversation_id"]
            if len(identifier) > 80 or any(c in identifier for c in "/\\?#%"):
                raise ValueError("会话 ID 不能超过 80 字或包含 /、\\、?、#、%")
            if identifier in identifiers:
                raise ValueError("文件内会话 ID 重复：" + identifier)
            if len(row["dialogue"]) > 20000:
                raise ValueError("单条会话不能超过 20000 字")
            row["date"] = date.fromisoformat(row["date"]).isoformat()
            identifiers.add(identifier)
        with database() as conn:
            if any(get(conn, "conversations", identifier) for identifier in identifiers):
                raise HTTPException(409, "会话 ID 已存在，请使用新 ID；已有工单不会被覆盖")
            knowledge = all_records(conn, "knowledge")
        results = await run_in_threadpool(lambda: [classify({key: row[key] for key in required}, knowledge) for row in rows])
        with database() as conn:
            if any(get(conn, "conversations", item["conversation_id"]) for item in results):
                raise HTTPException(409, "会话 ID 已被其他导入占用，请刷新后重试")
            for item in results:
                prepare(conn, item)
                save(conn, "conversations", item["conversation_id"], item)
    except (UnicodeDecodeError, csv.Error, ValueError) as error:
        raise HTTPException(400, "CSV 校验失败：" + str(error)) from error
    return await run_in_threadpool(conversations)


@app.post("/api/conversations/{conversation_id}/review")
def review(conversation_id: str, payload: Review):
    with database() as conn:
        item = require_item(conn, conversation_id)
        if item["review_status"] != "pending":
            raise HTTPException(409, "此会话已完成复核")
        if payload.status in {"rejected", "edited"} and not payload.note:
            raise HTTPException(422, "修改或驳回时请填写原因")
        if item.get("analysis_review_required") and not payload.note:
            raise HTTPException(422, "分析不确定或已降级，请填写人工核查依据后提交")
        if payload.status == "edited":
            if not payload.labels or not payload.priority:
                raise HTTPException(422, "修改结论时必须提供风险标签和优先级")
            item.update(labels=payload.labels, priority=payload.priority)
            item["complaint_labels"] = [label for label in payload.labels if label not in SERVICE_LABELS]
            item["service_labels"] = [label for label in payload.labels if label in SERVICE_LABELS]
            if not item["ticket"]:
                item["suggested_owner"] = "投诉专员" if payload.priority == "P0" else "售后负责人" if "售后退款" in payload.labels else "履约负责人" if "履约发货" in payload.labels else "质检主管"
                history, created_at = item["history"], item["created_at"]
                prepare(conn, item)
                item["history"] = history + item["history"][1:]
                item["created_at"] = created_at
            item["ticket"]["due_at"] = (datetime.fromisoformat(item["ticket"]["created_at"]) + timedelta(hours={"P0": 2, "P1": 24, "P2": 72}[payload.priority])).isoformat()
        if item["ticket"]:
            ticket = require_ticket(item, {"pending_review"})
            if payload.status == "rejected":
                ticket["status"] = "dismissed"
            else:
                if not payload.plan:
                    raise HTTPException(422, "请确认或编辑处置方案后提交")
                ticket.update(status="ready", approved_plan=payload.plan, approved_at=now())
            notify(conn, item, "主管已驳回风险结论" if payload.status == "rejected" else "处置方案已确认，请负责人登记执行")
        item["review_status"] = payload.status
        item["review_note"] = payload.note
        event(item, "reviewed", payload.status + "：" + payload.note)
        save(conn, "conversations", conversation_id, item)
    return {"ok": True, "item": item}


@app.post("/api/conversations/{conversation_id}/assign")
def assign(conversation_id: str, payload: Assignment):
    if payload.owner not in OWNERS:
        raise HTTPException(422, "请选择已配置的负责人")
    with database() as conn:
        item = require_item(conn, conversation_id)
        ticket = require_ticket(item, {"pending_review", "ready", "in_progress"})
        previous = ticket["owner"]
        ticket["owner"] = payload.owner
        event(item, "assigned", previous + " → " + payload.owner)
        notify(conn, item, "工单已转派，请跟进")
        save(conn, "conversations", conversation_id, item)
    return {"ok": True, "item": item}


@app.post("/api/conversations/{conversation_id}/start")
def start(conversation_id: str):
    with database() as conn:
        item = require_item(conn, conversation_id)
        ticket = require_ticket(item, {"ready"})
        ticket.update(status="in_progress", started_at=now())
        event(item, "started", "负责人登记开始执行已确认方案")
        save(conn, "conversations", conversation_id, item)
    return {"ok": True, "item": item}


@app.post("/api/conversations/{conversation_id}/resolve")
def resolve(conversation_id: str, payload: Resolution):
    with database() as conn:
        item = require_item(conn, conversation_id)
        ticket = require_ticket(item, {"in_progress"})
        ticket.update(status="resolved", resolution=payload.resolution, feedback=payload.feedback, resolved_at=now(), feedback_at=now())
        event(item, "resolved", payload.resolution)
        save(conn, "conversations", conversation_id, item)
    return {"ok": True, "item": item}


@app.post("/api/conversations/{conversation_id}/feedback")
def feedback(conversation_id: str, payload: Feedback):
    with database() as conn:
        item = require_item(conn, conversation_id)
        ticket = require_ticket(item, {"resolved"})
        ticket.update(feedback=payload.feedback, feedback_at=now())
        event(item, "feedback", "更新回访反馈：" + payload.feedback)
        save(conn, "conversations", conversation_id, item)
    return {"ok": True, "item": item}


@app.get("/api/knowledge")
def knowledge():
    with database() as conn:
        return {"items": all_records(conn, "knowledge")}


@app.post("/api/knowledge")
def add_knowledge(payload: Knowledge):
    doc = {**payload.model_dump(), "id": "KB-" + uuid4().hex[:12], "status": "draft", "created_at": now()}
    with database() as conn:
        save(conn, "knowledge", doc["id"], doc)
    return {"item": doc}


@app.put("/api/knowledge/{identifier}")
def edit_knowledge(identifier: str, payload: Knowledge):
    with database() as conn:
        doc = get(conn, "knowledge", identifier)
        if not doc:
            raise HTTPException(404, "知识条目不存在")
        if doc["status"] != "draft":
            raise HTTPException(409, "已发布知识保留原始版本，请新建修订条目")
        doc.update(**payload.model_dump(), updated_at=now())
        save(conn, "knowledge", identifier, doc)
    return {"item": doc}


@app.post("/api/knowledge/{identifier}/publish")
def publish_knowledge(identifier: str):
    with database() as conn:
        doc = get(conn, "knowledge", identifier)
        if not doc:
            raise HTTPException(404, "知识条目不存在")
        if doc["status"] != "draft":
            raise HTTPException(409, "该条目已发布")
        doc.update(status="published", published_at=now())
        save(conn, "knowledge", identifier, doc)
    return {"item": doc}


@app.post("/api/conversations/{conversation_id}/knowledge")
def promote_case(conversation_id: str):
    with database() as conn:
        item = require_item(conn, conversation_id)
        ticket = require_ticket(item, {"resolved"})
        identifier = "CASE-" + conversation_id
        if get(conn, "knowledge", identifier):
            raise HTTPException(409, "该工单已沉淀为案例草稿")
        doc = {"id": identifier, "title": "处理案例 · " + conversation_id, "kind": "case", "labels": item["labels"], "content": "问题：" + item["summary"] + "\n已确认方案：" + ticket["approved_plan"] + "\n处理结果：" + ticket["resolution"], "version": "v1", "status": "draft", "created_at": now(), "conversation_id": conversation_id}
        save(conn, "knowledge", identifier, doc)
        event(item, "knowledge_draft", "已生成知识案例草稿，发布后参与检索")
        save(conn, "conversations", conversation_id, item)
    return {"item": doc}


@app.post("/api/notifications/{identifier}/read")
def read_notification(identifier: str):
    with database() as conn:
        notification = get(conn, "notifications", identifier)
        if not notification:
            raise HTTPException(404, "提醒不存在")
        notification["read"] = True
        save(conn, "notifications", identifier, notification)
    return {"ok": True}
