import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer

import server as doc_server
from server import DocumentMcpHandler, MCP_PROTOCOL_VERSION, dispatch_mcp


class DocumentMcpServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_secret = os.environ.get("MCP_SHARED_SECRET")
        os.environ["MCP_SHARED_SECRET"] = "document-test-secret"
        cls.original_search = doc_server.search_manual
        doc_server.search_manual = lambda *_args, **_kwargs: [
            {
                "source": "manual",
                "title": "Torque alarm",
                "excerpt": "Inspect the capping head.",
                "page": 42,
                "sourceUri": "/manuals/capper.pdf",
                "chunkId": "chunk-42",
                "chunkKind": "troubleshooting",
                "topics": ["torque"],
                "alarmCodes": ["TORQUE_HIGH"],
                "safetyLevel": "technician",
                "confidence": 0.91,
            }
        ]
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DocumentMcpHandler)
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
        doc_server.search_manual = cls.original_search
        if cls.previous_secret is None:
            os.environ.pop("MCP_SHARED_SECRET", None)
        else:
            os.environ["MCP_SHARED_SECRET"] = cls.previous_secret

    def request(
        self,
        *,
        payload: object,
        secret: str | None = "document-test-secret",
        protocol_version: str | None = MCP_PROTOCOL_VERSION,
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        }
        if secret is not None:
            headers["X-Arol-Mcp-Secret"] = secret
        if protocol_version is not None:
            headers["MCP-Protocol-Version"] = protocol_version
        connection.request("POST", "/mcp", body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        connection.close()
        return response.status, json.loads(response_body)

    def test_authentication_and_protocol_version_are_enforced(self) -> None:
        request = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
        status, _payload = self.request(payload=request, secret=None)
        self.assertEqual(status, 401)
        status, _payload = self.request(
            payload=request,
            protocol_version="2024-11-05",
        )
        self.assertEqual(status, 400)

    def test_initialize_and_tools_list_publish_manual_search(self) -> None:
        status, initialized = self.request(
            payload={
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
        status, listed = self.request(
            payload={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [tool["name"] for tool in listed["result"]["tools"]],
            ["manual.search"],
        )

    def test_manual_search_preserves_retrieval_metadata(self) -> None:
        status, called = self.request(
            payload={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "manual.search",
                    "arguments": {
                        "machineId": "euro-vp-2019-01",
                        "query": "TORQUE_HIGH",
                    },
                },
            }
        )
        self.assertEqual(status, 200)
        self.assertFalse(called["result"]["isError"])
        evidence = called["result"]["structuredContent"][0]
        self.assertEqual(evidence["chunkKind"], "troubleshooting")
        self.assertEqual(evidence["topics"], ["torque"])
        self.assertEqual(evidence["alarmCodes"], ["TORQUE_HIGH"])
        self.assertEqual(evidence["safetyLevel"], "technician")

    def test_non_object_params_and_arguments_return_structured_errors(self) -> None:
        status, invalid_params = self.request(
            payload={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": [],
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(invalid_params["error"]["code"], -32602)

        status, invalid_arguments = self.request(
            payload={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "manual.search", "arguments": []},
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
