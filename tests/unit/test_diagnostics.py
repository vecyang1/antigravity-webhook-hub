"""
Unit Tests for DiagnosticInspector & Formatters (对账器优先)
Testing URL explaining, Spark email resolution, domain routing, and /api/diagnose handler.
"""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from hub.antigravity.diagnostics import DiagnosticInspector, format_diagnostic_report
from hub.db import DatabaseManager
from hub.models import HTTPRequest


class TestDiagnosticInspector(unittest.TestCase):
    """Test suite for DiagnosticInspector URL analysis, email routing, and formatting."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_hub.db")
        self.db = DatabaseManager(db_path=self.db_path)
        self.inspector = DiagnosticInspector(db=self.db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_explain_empty_target(self):
        """Empty or whitespace target returns INVALID_INPUT."""
        res = self.inspector.explain("")
        self.assertEqual(res["verdict"], "INVALID_INPUT")
        self.assertIn("error", res)

    def test_explain_n8n_cloud_url(self):
        """Verify n8n cloud webhook URL routing and ownership resolution."""
        url = "https://n.worldinspirelab.com/webhook/carradiocodes-notifications"
        res = self.inspector.explain(url)

        self.assertEqual(res["target_type"], "url")
        self.assertEqual(res["verdict"], "ROUTE_RECOGNIZED")
        self.assertEqual(res["host"], "n.worldinspirelab.com")
        self.assertEqual(res["system_owner"], "n8n Automation Engine (Cloud VPS @ 62.171.132.182)")
        self.assertEqual(res["owning_skill"], "n8n-automation")
        self.assertIn("n8n_workflow_ops.py", res["operator_cli"])
        self.assertTrue(res["is_known_route"])
        self.assertIn("Radio Code", res["target_flow"])

        # Test report formatting
        report = format_diagnostic_report(res)
        self.assertIn("n.worldinspirelab.com", report)
        self.assertIn("ROUTE_RECOGNIZED", report)
        self.assertIn("Radio Code", report)

    def test_explain_webhook_tunnel_url(self):
        """Verify Cloudflare tunnel ingress routing to local webhook hub."""
        url = "https://webhook.worldinspirelab.com/webhook/contact-review"
        res = self.inspector.explain(url)

        self.assertEqual(res["target_type"], "url")
        self.assertEqual(res["verdict"], "ROUTE_RECOGNIZED")
        self.assertEqual(res["host"], "webhook.worldinspirelab.com")
        self.assertEqual(res["system_owner"], "Antigravity Webhook Hub (Local Gateway via Cloudflare Tunnel)")
        self.assertIn("Cloudflare Tunnel", res["boundary"])
        self.assertIn("9423", res["boundary"])
        self.assertTrue(res["is_known_route"])
        self.assertIn("Notion CRM", res["target_flow"])

        report = format_diagnostic_report(res)
        self.assertIn("webhook.worldinspirelab.com", report)
        self.assertIn("127.0.0.1:9423", report)

    def test_explain_local_path(self):
        """Verify local path URL explaining."""
        res = self.inspector.explain("/webhook/antigravity")

        self.assertEqual(res["target_type"], "url")
        self.assertEqual(res["verdict"], "ROUTE_RECOGNIZED")
        self.assertEqual(res["path"], "/webhook/antigravity")
        self.assertEqual(res["auth_type"], "bearer_token")
        self.assertTrue(res["is_known_route"])

    def test_explain_unknown_url(self):
        """Verify unknown third-party URL handling."""
        res = self.inspector.explain("https://api.unknownservice.io/v1/event")

        self.assertEqual(res["target_type"], "url")
        self.assertEqual(res["verdict"], "UNKNOWN_ROUTE")
        self.assertFalse(res["is_known_route"])

    def test_explain_spark_email_target(self):
        """Verify Spark email reference recognition."""
        res = self.inspector.explain("spark:724913")

        self.assertEqual(res["target_type"], "email_spark")
        self.assertEqual(res["spark_pk"], 724913)
        self.assertIn("Spark Desktop & Apple Mail", res["system_owner"])
        self.assertEqual(res["owning_skill"], "use-spark & spark-otp")
        self.assertTrue(res["is_anker_warranty_inquiry"])

        report = format_diagnostic_report(res)
        self.assertIn("SPARK EMAIL INQUIRY ANALYSIS", report)
        self.assertIn("724913", report)

    def test_explain_email_domain_routing(self):
        """Verify brand and domain email routing."""
        res = self.inspector.explain("email:orders@carradiocodes.co.uk")

        self.assertEqual(res["target_type"], "email_routing")
        self.assertEqual(res["domain"], "carradiocodes.co.uk")
        self.assertIn("Car Radio Codes", res["brand"])
        self.assertIn("#notification_radiocode", res["target_slack_channel"])
        self.assertEqual(res["verdict"], "DOMAIN_ROUTED")

        report = format_diagnostic_report(res)
        self.assertIn("DOMAIN & EMAIL ROUTING ANALYSIS", report)
        self.assertIn("Car Radio Codes", report)

    def test_api_diagnose_handler(self):
        """Verify /api/diagnose route handler returns structured JSON and report_text."""
        from hub.config import AppConfig
        from hub.routes.observability import register_observability_routes
        from hub.server import AsyncHTTPServer

        server = AsyncHTTPServer()
        register_observability_routes(server, config=AppConfig(), db=self.db)

        # Find handler for GET /api/diagnose
        handler = server._exact_routes.get(("GET", "/api/diagnose"))
        self.assertIsNotNone(handler, "GET /api/diagnose route must be registered in _exact_routes")

        import asyncio
        req = HTTPRequest(
            method="GET",
            path="/api/diagnose",
            headers={},
            query_params={"target": "https://n.worldinspirelab.com/webhook/heartbeat"},
        )
        resp = asyncio.run(handler(req))
        self.assertEqual(resp.status_code, 200)

        import json
        body = json.loads(resp.body)
        self.assertEqual(body.get("verdict"), "ROUTE_RECOGNIZED")
        self.assertEqual(body.get("system_owner"), "n8n Automation Engine (Cloud VPS @ 62.171.132.182)")
        self.assertIn("report_text", body)
        self.assertIn("ANTIGRAVITY WEBHOOK HUB", body["report_text"])


if __name__ == "__main__":
    unittest.main()
