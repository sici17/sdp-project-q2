import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

from server import BusinessMcpHandler, MCP_PROTOCOL_VERSION, dispatch_mcp


class BusinessMcpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_secret = os.environ.get("MCP_SHARED_SECRET")
        os.environ["MCP_SHARED_SECRET"] = "business-test-secret"
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), BusinessMcpHandler)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever,
            daemon=True,
        )
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
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
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
            secret="business-test-secret",
            protocol_version=MCP_PROTOCOL_VERSION,
        )

    def test_authentication_is_fail_closed_and_protocol_is_enforced(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        status, _payload = self.request("POST", "/mcp", payload=request)
        self.assertEqual(status, 401)

        status, _payload = self.request(
            "POST",
            "/mcp",
            payload=request,
            secret="business-test-secret",
            protocol_version="2024-11-05",
        )
        self.assertEqual(status, 400)

        previous = os.environ.pop("MCP_SHARED_SECRET")
        try:
            status, _payload = self.request(
                "POST",
                "/mcp",
                payload=request,
                secret="business-test-secret",
                protocol_version=MCP_PROTOCOL_VERSION,
            )
            self.assertEqual(status, 401)
        finally:
            os.environ["MCP_SHARED_SECRET"] = previous

    def test_initialize_and_tools_list_publish_business_contract(self) -> None:
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
                "business.list_quotes",
                "business.get_quote",
                "business.compare_quote_revisions",
                "business.list_orders",
                "business.machine_purchase_summary",
                "business.list_maintenance_tickets",
                "business.get_service_entitlement",
                "business.list_service_history",
            },
        )

    def test_machine_tools_return_dataset_backed_shapes(self) -> None:
        expected_shapes = {
            "business.get_service_entitlement": dict,
            "business.list_orders": list,
            "business.list_service_history": list,
            "business.machine_purchase_summary": dict,
            "business.list_maintenance_tickets": list,
        }
        for request_id, (tool_name, expected_type) in enumerate(
            expected_shapes.items(),
            start=1,
        ):
            status, called = self.mcp(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {
                        "name": tool_name,
                        "arguments": {"machineId": "MCH-0004"},
                    },
                }
            )
            self.assertEqual(status, 200)
            self.assertFalse(called["result"]["isError"])
            self.assertIsInstance(
                called["result"]["structuredContent"],
                expected_type,
            )

    def test_quote_tools_follow_the_revision_lifecycle(self) -> None:
        status, listed = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "business.list_quotes",
                    "arguments": {"companyId": "CMP-002"},
                },
            }
        )
        self.assertEqual(status, 200)
        quotes = listed["result"]["structuredContent"]
        self.assertTrue(quotes)
        # A quote carries no status of its own: the lifecycle is on the revision.
        self.assertTrue(all("currentRevisionStatus" in quote for quote in quotes))

        rejected = next(
            quote for quote in quotes if quote["quoteId"] == "QTE-2025-0003"
        )
        self.assertEqual(rejected["currentRevisionStatus"], "Rejected")
        self.assertEqual(rejected["currentRevisionNumber"], 3)

        status, compared = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "business.compare_quote_revisions",
                    "arguments": {"quoteId": "QTE-2025-0003"},
                },
            }
        )
        self.assertEqual(status, 200)
        diff = compared["result"]["structuredContent"]
        self.assertTrue(diff["comparable"])
        self.assertEqual(diff["currentRevision"]["revisionNumber"], 3)
        self.assertEqual(diff["previousRevision"]["revisionNumber"], 2)

    def test_non_object_params_and_arguments_return_structured_errors(self) -> None:
        status, invalid_params = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": [],
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(invalid_params["error"]["code"], -32602)

        status, invalid_arguments = self.mcp(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "business.list_orders",
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
