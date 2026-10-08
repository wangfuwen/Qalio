import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import app
from copilot.analysis import classify, retrieve
from copilot.store import ROOT


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"QA_DB_PATH": str(Path(self.temp.name) / "test.sqlite3"), "QA_ENABLE_LLM": "false", "QA_ENABLE_JEV": "false"})
        self.env.start()
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.temp.cleanup()

    def item(self, identifier="C001"):
        return next(row for row in self.client.get("/api/conversations").json()["items"] if row["conversation_id"] == identifier)

    def post(self, identifier, action, payload=None):
        return self.client.post(f"/api/conversations/{identifier}/{action}", json=payload or {})

    def approve(self, identifier="C001"):
        return self.post(identifier, "review", {"status": "accepted", "plan": "核查订单并依服务规范处理，完成后回访。"})

    def close_ticket(self, identifier="C001"):
        self.assertEqual(self.approve(identifier).status_code, 200)
        self.assertEqual(self.post(identifier, "start").status_code, 200)
        self.assertEqual(self.post(identifier, "resolve", {"resolution": "已核实订单并沟通解决，用户确认收到反馈。", "feedback": "satisfied"}).status_code, 200)

    def upload(self, identifier="NEW-001", dialogue="用户：我要投诉。\n客服：我来协助核查。"):
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["conversation_id", "date", "agent", "customer", "dialogue"])
        writer.writerow([identifier, "2026-09-04", "测试客服", "测试用户", dialogue])
        return self.client.post("/api/upload", files={"file": ("test.csv", buffer.getvalue().encode("utf-8"), "text/csv")})

    def test_seed_and_real_metrics(self):
        result = self.client.get("/api/conversations").json()
        self.assertEqual(result["summary"]["total"], 8)
        self.assertEqual(result["summary"]["flagged"], 6)
        self.assertEqual(result["summary"]["pending"], 6)
        self.assertEqual(result["summary"]["resolution_rate"], 0)
        self.assertIsNone(result["summary"]["avg_handling_hours"])
        self.assertEqual(len(result["notifications"]), 6)
        self.assertEqual(self.item()["ticket"]["owner"], "投诉专员")

    def test_human_gate_and_state_transitions(self):
        self.assertEqual(self.post("C001", "start").status_code, 409)
        self.assertEqual(self.post("C001", "resolve", {"resolution": "尚未开始就尝试关闭"}).status_code, 409)
        self.assertEqual(self.post("C001", "review", {"status": "accepted"}).status_code, 422)
        self.close_ticket()
        self.assertEqual(self.post("C001", "start").status_code, 409)
        self.assertEqual(self.approve().status_code, 409)
        summary = self.client.get("/api/conversations").json()["summary"]
        self.assertEqual(summary["pending"], 5)
        self.assertEqual(summary["resolved"], 1)
        self.assertEqual(summary["resolution_rate"], 16.7)
        self.assertEqual(summary["feedback_trend"][0]["satisfied"], 1)

    def test_reject_removes_from_risk_and_denominator(self):
        self.assertEqual(self.post("C001", "review", {"status": "rejected"}).status_code, 422)
        self.assertEqual(self.post("C001", "review", {"status": "rejected", "note": "核对原文后判定为误报"}).status_code, 200)
        summary = self.client.get("/api/conversations").json()["summary"]
        self.assertEqual(summary["flagged"], 5)
        self.assertEqual(summary["ticket_total"], 5)
        self.assertEqual(self.post("C001", "start").status_code, 409)

    def test_edit_and_manual_detection(self):
        result = self.post("C005", "review", {"status": "edited", "labels": ["售后退款"], "priority": "P1", "note": "人工补充发现的退款风险", "plan": "核实商品状态并联系售后负责人审核。"})
        self.assertEqual(result.status_code, 200)
        row = self.item("C005")
        self.assertEqual(row["labels"], ["售后退款"])
        self.assertEqual(row["ticket"]["status"], "ready")
        self.assertEqual(row["ticket"]["owner"], "售后负责人")
        self.assertEqual(self.post("C002", "review", {"status": "edited", "labels": ["不存在"], "priority": "P0", "note": "说明"}).status_code, 422)

    def test_reassign_and_notification_read(self):
        result = self.post("C001", "assign", {"owner": "售后负责人"})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.item()["ticket"]["owner"], "售后负责人")
        notification = self.client.get("/api/conversations").json()["notifications"][0]
        self.assertEqual(notification["owner"], "售后负责人")
        self.assertEqual(self.client.post("/api/notifications/" + notification["id"] + "/read").status_code, 200)
        self.assertTrue(self.client.get("/api/conversations").json()["notifications"][0]["read"])
        self.assertEqual(self.post("C001", "assign", {"owner": "不存在"}).status_code, 422)

    def test_import_persistence_and_duplicate_protection(self):
        self.assertEqual(self.upload().status_code, 200)
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.client.get("/api/conversations").json()["summary"]["total"], 9)
        self.client.__exit__(None, None, None)
        self.client = TestClient(app)
        self.client.__enter__()
        self.assertEqual(self.item("NEW-001")["ticket"]["status"], "pending_review")
        self.assertEqual(self.client.get("/api/conversations").json()["summary"]["total"], 9)

    def test_invalid_csv(self):
        for filename, body in [("test.txt", b"x"), ("test.csv", b"\xff"), ("test.csv", b"x,y\n1,2"), ("test.csv", b"conversation_id,date,agent,customer,dialogue\nX,no-date,a,b,c")]:
            self.assertEqual(self.client.post("/api/upload", files={"file": (filename, body)}).status_code, 400)
        self.assertEqual(self.client.post("/api/upload", files={"file": ("test.csv", b"x" * (2 * 1024 * 1024 + 1))}).status_code, 413)
        self.assertEqual(self.client.get("/api/conversations").json()["summary"]["total"], 8)

    def test_unknown_resources(self):
        self.assertEqual(self.post("missing", "review", {"status": "accepted"}).status_code, 404)
        self.assertEqual(self.client.post("/api/knowledge/missing/publish").status_code, 404)
        self.assertEqual(self.client.post("/api/notifications/missing/read").status_code, 404)

    def test_case_feedback_and_publication_loop(self):
        self.assertEqual(self.post("C001", "knowledge").status_code, 409)
        self.close_ticket()
        self.assertEqual(self.post("C001", "feedback", {"feedback": "dissatisfied"}).status_code, 200)
        draft = self.post("C001", "knowledge").json()["item"]
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(self.post("C001", "knowledge").status_code, 409)
        self.assertEqual(retrieve("投诉", draft["labels"], [draft]), [])
        self.assertEqual(self.client.post("/api/knowledge/" + draft["id"] + "/publish").status_code, 200)
        docs = self.client.get("/api/knowledge").json()["items"]
        published = next(doc for doc in docs if doc["id"] == draft["id"])
        self.assertEqual(len(retrieve("投诉", published["labels"], [published])), 1)
        feedback = self.client.get("/api/conversations").json()["summary"]["feedback_trend"][0]
        self.assertEqual(feedback["dissatisfied"], 1)
        self.assertEqual(feedback["satisfied"], 0)

    def test_new_knowledge_is_draft(self):
        result = self.client.post("/api/knowledge", json={"title": "新退款规范", "content": "核实订单和适用规则后再进行退款审核。", "labels": ["售后退款"]})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["item"]["status"], "draft")

    def test_knowledge_draft_edit_and_version_guard(self):
        payload = {"title": "退款规范草稿", "content": "核实订单和适用条件后联系售后进行审核。", "labels": ["售后退款"]}
        doc = self.client.post("/api/knowledge", json=payload).json()["item"]
        payload["content"] = "修订：核实订单和适用条件，并约定下一次反馈时间。"
        result = self.client.put("/api/knowledge/" + doc["id"], json=payload)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["item"]["content"], payload["content"])
        self.client.post("/api/knowledge/" + doc["id"] + "/publish")
        self.assertEqual(self.client.put("/api/knowledge/" + doc["id"], json=payload).status_code, 409)

    def test_normal_conversation_review(self):
        self.assertEqual(self.post("C005", "review", {"status": "accepted"}).status_code, 200)
        self.assertIsNone(self.item("C005")["ticket"])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"QA_ENABLE_LLM": "false", "QA_ENABLE_JEV": "false"})
        self.env.start()
        self.docs = json.loads((ROOT / "data" / "knowledge.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.env.stop()

    def test_complaint_is_not_agent_misconduct(self):
        row = classify({"dialogue": "用户：我要投诉。\\n客服：非常抱歉，我来核实。"}, self.docs)
        self.assertEqual(row["priority"], "P0")
        self.assertEqual(row["service_labels"], [])
        self.assertEqual(row["complaint_labels"], ["情绪升级"])
        self.assertNotIn("\\n", row["dialogue"])
        self.assertTrue(row["sources"])

    def test_negated_promises_and_customer_words(self):
        row = classify({"dialogue": "用户：你保证今天到吗？\n客服：不能保证今天到，我会核实物流。"}, self.docs)
        self.assertEqual(row["priority"], "—")
        row = classify({"dialogue": "用户：我不知道尺码。\n客服：请参考尺码表。"}, self.docs)
        self.assertEqual(row["priority"], "—")

    def test_valid_model_result_and_evidence(self):
        payload = {"summary": "用户有投诉意向", "findings": [{"label": "情绪升级", "evidence": "用户：我要投诉。"}], "suggestion": "先核查订单并约定反馈时间，交主管确认。", "source_ids": ["KB-001"]}
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])
        with patch.dict(os.environ, {"QA_ENABLE_LLM": "true"}), patch("openai.OpenAI", return_value=client):
            row = classify({"dialogue": "用户：我要投诉。\n客服：我来核查。"}, self.docs)
        self.assertEqual(row["analysis_mode"], "llm+rules+retrieval")
        self.assertEqual(row["summary"], "用户有投诉意向")

    def test_invalid_model_evidence_falls_back(self):
        payload = {"summary": "编造内容", "findings": [{"label": "越权承诺", "evidence": "不存在的证据"}], "suggestion": "建议", "source_ids": ["KB-001"]}
        client = MagicMock()
        client.__enter__.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])
        with patch.dict(os.environ, {"QA_ENABLE_LLM": "true"}), patch("openai.OpenAI", return_value=client):
            row = classify({"dialogue": "用户：我要投诉。\n客服：我来核查。"}, self.docs)
        self.assertEqual(row["analysis_mode"], "rules_fallback")
        self.assertNotIn("越权承诺", row["labels"])
        self.assertTrue(row["analysis_warning"])

    def test_model_failure_falls_back(self):
        with patch.dict(os.environ, {"QA_ENABLE_LLM": "true"}), patch("openai.OpenAI", side_effect=RuntimeError("unavailable")):
            row = classify({"dialogue": "用户：我要投诉。"}, self.docs)
        self.assertEqual(row["analysis_mode"], "rules_fallback")
        self.assertEqual(row["priority"], "P0")


if __name__ == "__main__":
    unittest.main()
