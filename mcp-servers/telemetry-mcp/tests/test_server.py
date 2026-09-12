import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

from server import MCP_PROTOCOL_VERSION, TelemetryHandler, dispatch_mcp


class TelemetryServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_secret = os.environ.get("MCP_SHARED_SECRET")
        os.environ["MCP_SHARED_SECRET"] = "telemetry-test-secret"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TelemetryHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        if cls.previous_secret is None:
            os.environ.pop("MCP_SHARED_SECRET", None)
        else:
            os.environ["MCP_SHARED_SECRET"] = cls.previous_secret

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        secret: str | None = None,
        protocol_version: str | None = None,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        headers = {"Accept": "application/json"}
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        if secret is not None:
            headers["X-Arol-Mcp-Secret"] = secret
        if protocol_version is not None:
            headers["MCP-Protocol-Version"] = protocol_version

        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        return response.status, json.loads(response_body)

    def mcp(self, payload: object) -> tuple[int, dict]:
        return self.request(
            "POST",
            "/mcp",
            payload=payload,
            secret="telemetry-test-secret",
            protocol_version=MCP_PROTOCOL_VERSION,
        )

    def test_http_latest_exposes_dataset_snapshot_and_freshness_contract(self) -> None:
        status, snapshot = self.request(
            "GET",
            "/api/v1/machines/MCH-0004/telemetry/latest",
            secret="telemetry-test-secret",
        )

        self.assertEqual(status, 200)
        self.assertEqual(snapshot["machineId"], "MCH-0004")
        # Age is measured against the dataset's frozen reference date, so the
        # newest snapshot reads as fresh however long after delivery this runs.
        self.assertEqual(snapshot["quality"], "fresh")
        self.assertEqual(snapshot["missingFields"], [])
        self.assertEqual(snapshot["source"], "fleet-dataset")
        # Telemetry must be judged against this machine's own nominal rate.
        self.assertEqual(snapshot["nominalRateBph"], 4500)

    def test_http_read_api_requires_authentication(self) -> None:
        status, unauthorized = self.request(
            "GET",
            "/api/v1/machines/MCH-0004/telemetry/latest",
        )
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error"], "MCP authentication failed.")

    def test_mcp_rejects_missing_auth_and_incompatible_protocol(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "ping",
            "params": {},
        }
        status, unauthorized = self.request("POST", "/mcp", payload=request)
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error"], "MCP authentication failed.")

        status, incompatible = self.request(
            "POST",
            "/mcp",
            payload=request,
            secret="telemetry-test-secret",
            protocol_version="2024-11-05",
        )
        self.assertEqual(status, 400)
        self.assertEqual(incompatible["error"], "Unsupported MCP protocol version.")

    def test_mcp_initialize_and_tools_list_publish_supported_contract(self) -> None:
        status, initialized = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            initialized["result"]["protocolVersion"],
            MCP_PROTOCOL_VERSION,
        )

        status, listed = self.mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            {tool["name"] for tool in listed["result"]["tools"]},
            {
                "telemetry.latest_snapshot",
                "telemetry.history",
                "telemetry.active_alarms",
                "telemetry.alarm_lookup",
                "telemetry.alarm_frequency",
            },
        )

    def test_mcp_tool_call_returns_structured_snapshot_and_unknown_machine_error(self) -> None:
        status, called = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "telemetry.latest_snapshot",
                    "arguments": {"machineId": "MCH-0004"},
                },
            }
        )
        self.assertEqual(status, 200)
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(called["result"]["structuredContent"]["quality"], "fresh")

        status, missing = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "telemetry.latest_snapshot",
                    "arguments": {"machineId": "unknown-machine"},
                },
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(missing["result"]["isError"])
        self.assertEqual(
            missing["result"]["structuredContent"]["error"],
            "Machine not found.",
        )

    def test_alarm_lookup_resolves_exact_dataset_code_without_manual_inference(self) -> None:
        status, called = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "telemetry.alarm_lookup",
                    "arguments": {"machineId": "MCH-0008", "code": "AL031"},
                },
            }
        )

        self.assertEqual(status, 200)
        self.assertFalse(called["result"]["isError"])
        alarm = called["result"]["structuredContent"]
        self.assertEqual(alarm["requestedCode"], "AL031")
        self.assertEqual(alarm["alarmCode"], "AL031_HEADS_MOTOR_OVERLOAD")
        self.assertEqual(alarm["severity"], "High")
        self.assertEqual(alarm["occurrences"], 4)
        self.assertEqual(alarm["unresolved"], 1)

    def test_mcp_rejects_malformed_json(self) -> None:
        status, response = self.request(
            "POST",
            "/mcp",
            secret="telemetry-test-secret",
            protocol_version=MCP_PROTOCOL_VERSION,
            raw_body=b"{not-json",
        )
        self.assertEqual(status, 400)
        self.assertEqual(response["error"], "Invalid JSON.")

    def test_mcp_rejects_non_object_params_and_arguments_without_crashing(self) -> None:
        status, invalid_params = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/list",
                "params": [],
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(invalid_params["error"]["code"], -32602)

        status, invalid_arguments = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "telemetry.latest_snapshot",
                    "arguments": [],
                },
            }
        )
        self.assertEqual(status, 200)
        self.assertTrue(invalid_arguments["result"]["isError"])
        self.assertEqual(
            invalid_arguments["result"]["structuredContent"]["error"],
            "Tool arguments must be an object.",
        )

    def test_malformed_notification_is_suppressed(self) -> None:
        self.assertIsNone(
            dispatch_mcp(
                {
                    "jsonrpc": "2.0",
                    "method": "tools/list",
                    "params": [],
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
