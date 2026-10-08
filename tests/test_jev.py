import csv
import importlib.util
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
from copilot.analysis import classify
from copilot.jev import RUBRICS


@unittest.skipUnless(importlib.util.find_spec("typesafe_sdk"), "Install requirements-jev.txt for SDK contract tests")
class JevTests(unittest.TestCase):
    def setUp(self):
        import httpx2
        from typesafe_sdk import TypeSafeClient
        self.requests = []
        self.values = {}
        self.failure = False
        self.env = patch.dict(os.environ, {
            "QA_ENABLE_JEV": "true", "QA_ENABLE_LLM": "false",
            "QA_JEV_NEGATIVE_THRESHOLD": "0.3", "QA_JEV_POSITIVE_THRESHOLD": "0.8",
            "QA_JEV_EVIDENCE_CONFIDENCE": "0.6", "QA_JEV_MODEL": "jev-latest",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

        def handler(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            if self.failure:
                return httpx2.Response(503, json={"error": "unavailable"})
            answers = {}
            for key in RUBRICS:
                p, line_id, confidence = self.values.get(key, (0.05, "NONE", 0.95))
                options = payload["questions"][key + "_evidence"]["criteria"]
                distribution = {option: float(option == line_id) for option in options}
                answers[key] = {"type": "noul", "noul": p}
                answers[key + "_evidence"] = {"type": "choice", "choice": line_id,
                                              "confidence": confidence, "probabilities": distribution}
            return httpx2.Response(200, json={"model": "jev-test", "usage": {"input_tokens": 100, "output_tokens": 10}, "answers": answers})

        def factory(**kwargs):
            return TypeSafeClient(api_key="test-key", base_url="https://api.typesafe.ai",
                                  transport=httpx2.MockTransport(handler), **kwargs)
        client_patch = patch("typesafe_sdk.TypeSafeClient", side_effect=factory)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.docs = json.loads(Path("data/knowledge.json").read_text(encoding="utf-8"))
        self.dialogue = "用户：三天没发货，请处理。\n客服：我帮您核实。"

    def analyze(self, dialogue=None):
        return classify({"dialogue": dialogue or self.dialogue}, self.docs)

    def test_sdk_request_and_verbatim_evidence(self):
        self.values["delivery"] = (0.94, "L000", 0.9)
        row = self.analyze()
        self.assertEqual(row["analysis_mode"], "jev+retrieval")
        self.assertEqual(row["labels"], ["履约发货"])
        self.assertEqual(row["priority"], "P1")
        self.assertEqual(row["suggested_owner"], "履约负责人")
        self.assertEqual(row["evidence"][0]["evidence"], self.dialogue.splitlines()[0])
        self.assertEqual(row["jev"]["model"], "jev-test")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(self.requests[0]["questions"]), 10)
        self.assertNotIn("L000", self.requests[0]["questions"]["promise_evidence"]["criteria"])
        self.assertTrue(row["sources"])

    def test_success_does_not_merge_keyword_false_positive(self):
        row = self.analyze("用户：我不投诉，只想问一下配送政策。\n客服：请参考配送说明。")
        self.assertEqual(row["analysis_mode"], "jev+retrieval")
        self.assertEqual(row["priority"], "—")
        self.assertEqual(row["evidence"], [])
        self.assertFalse(row["analysis_review_required"])

    def test_uncertain_and_conflicting_answers_need_review(self):
        for value in [(0.5, "NONE", 0.9), (0.94, "NONE", 0.9), (0.95, "L000", 0.2), (0.05, "L000", 0.9)]:
            with self.subTest(value=value):
                self.values["delivery"] = value
                row = self.analyze()
                self.assertEqual(row["analysis_mode"], "jev+retrieval")
                self.assertTrue(row["analysis_review_required"])
                self.assertEqual(row["evidence"], [])

    def test_bad_numbers_or_customer_evidence_for_service_fail_closed(self):
        for key, value in [("promise", (0.9, "L000", 0.9)), ("delivery", (1.3, "L000", 0.9)), ("delivery", (0.9, "invented", 0.9))]:
            with self.subTest(key=key, value=value):
                self.values = {key: value}
                row = self.analyze()
                self.assertEqual(row["analysis_mode"], "jev_fallback")
                self.assertTrue(row["analysis_review_required"])

    def test_failure_is_reviewable_even_without_keyword_hit(self):
        self.failure = True
        row = self.analyze("用户：请帮我看看。")
        self.assertEqual(row["analysis_mode"], "jev_fallback")
        self.assertTrue(row["analysis_review_required"])
        self.assertEqual(len(self.requests), 1)  # no hidden retry loop

    def test_oversize_turns_not_silently_truncated(self):
        row = self.analyze("\n".join("用户：你好。" for _ in range(255)))
        self.assertEqual(row["analysis_mode"], "jev_fallback")
        self.assertEqual(self.requests, [])

    def test_invalid_thresholds_do_not_call_network(self):
        with patch.dict(os.environ, {"QA_JEV_NEGATIVE_THRESHOLD": "0.9"}):
            self.assertEqual(self.analyze()["analysis_mode"], "jev_fallback")
        self.assertEqual(self.requests, [])

    def test_multiple_labels_and_boundary_thresholds(self):
        self.values.update(delivery=(0.8, "L000", 0.6), escalation=(0.95, "L000", 0.9))
        row = self.analyze("用户：三天没发货，我要投诉。\n客服：我来核实。")
        self.assertEqual(row["labels"], ["履约发货", "情绪升级"])
        self.assertEqual(row["priority"], "P0")
        self.assertEqual(row["suggested_owner"], "投诉专员")
        self.values = {"delivery": (0.3, "NONE", 0.9)}
        self.assertFalse(self.analyze()["analysis_review_required"])

    def test_normal_case_skips_generation(self):
        with patch.dict(os.environ, {"QA_ENABLE_LLM": "true"}), patch("copilot.generation.draft") as draft:
            self.assertEqual(self.analyze()["analysis_mode"], "jev+retrieval")
            draft.assert_not_called()

    def test_generation_failure_preserves_jev_labels(self):
        self.values["delivery"] = (0.94, "L000", 0.9)
        with patch.dict(os.environ, {"QA_ENABLE_LLM": "true"}), patch("copilot.generation.draft", side_effect=ValueError("bad references")):
            row = self.analyze()
        self.assertEqual(row["labels"], ["履约发货"])
        self.assertEqual(row["analysis_mode"], "jev+retrieval")
        self.assertTrue(row["analysis_review_required"])

    def test_uncertainty_persists_and_requires_human_note(self):
        self.values["delivery"] = (0.5, "NONE", 0.9)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"QA_DB_PATH": str(Path(directory) / "test.sqlite3")}):
            with TestClient(app) as client:
                self.assertEqual(self.requests, [])  # seeds never call external APIs
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow(["conversation_id", "date", "agent", "customer", "dialogue"])
                writer.writerow(["JEV-001", "2026-09-28", "客服", "用户", self.dialogue])
                response = client.post("/api/upload", files={"file": ("jev.csv", buffer.getvalue().encode(), "text/csv")})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["summary"]["pending"], 7)
                self.assertEqual(response.json()["summary"]["flagged"], 6)
                url = "/api/conversations/JEV-001/review"
                self.assertEqual(client.post(url, json={"status": "accepted"}).status_code, 422)
                self.assertEqual(client.post("/api/conversations/JEV-001/start", json={}).status_code, 409)
            with TestClient(app) as client:
                row = next(r for r in client.get("/api/conversations").json()["items"] if r["conversation_id"] == "JEV-001")
                self.assertTrue(row["analysis_review_required"])
                self.assertEqual(row["jev"]["decisions"][2]["verdict"], "uncertain")
                response = client.post(url, json={"status": "edited", "labels": ["履约发货"], "priority": "P1", "note": "核查订单后确认延迟发货", "plan": "联系仓库核实进度后回访用户。"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["item"]["ticket"]["status"], "ready")
                self.assertEqual(client.get("/api/conversations").json()["summary"]["pending"], 6)


class GenerationTests(unittest.TestCase):
    def test_draft_rejects_invented_citations_and_risk_fields(self):
        from copilot.generation import draft
        source = {"id": "KB-REAL", "content": "核实订单后由主管审核", "kind": "policy"}
        for changes in [{"source_ids": ["KB-INVENTED"]}, {"source_ids": []}, {"findings": []}]:
            payload = {"summary": "核查退款", "suggestion": "核实订单后由主管审核。", "source_ids": ["KB-REAL"], **changes}
            client = MagicMock()
            client.__enter__.return_value = client
            client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])
            with self.subTest(changes=changes), patch("openai.OpenAI", return_value=client), self.assertRaises(ValueError):
                draft("用户：退款还没到。", [], [], [source])

    def test_valid_draft_preserves_evidence_and_risk_decisions(self):
        from copilot.generation import draft
        client = MagicMock()
        client.__enter__.return_value = client
        payload = {"summary": "用户等待退款", "suggestion": "请主管先补充适用规范再确认方案。", "source_ids": []}
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])
        decisions = [{"label": "售后退款", "verdict": "uncertain", "probability": 0.5}]
        with patch("openai.OpenAI", return_value=client):
            result = draft("用户：退款还没到。", [], decisions, [])
        self.assertEqual(result.summary, payload["summary"])
        sent = json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(sent["decisions"], decisions)


if __name__ == "__main__":
    unittest.main()
