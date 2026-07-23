"""MCP transport security tests (offline): secrets referenced as ${VAR} in a
server config's url/headers/env must be expanded from the process environment
at connect time only — never persisted — and an unset variable must fail the
connection closed with a clear error instead of sending an empty secret."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from app.mcp_manager import MCPConnection, MCPManager, MCPServerConfig, _expand_env


class TestExpandEnv(unittest.TestCase):
    def test_expands_set_variables(self):
        os.environ["MCP_TEST_TOKEN"] = "sekret-value"
        try:
            missing: set[str] = set()
            out = _expand_env("Bearer ${MCP_TEST_TOKEN}", missing)
        finally:
            del os.environ["MCP_TEST_TOKEN"]
        self.assertEqual(out, "Bearer sekret-value")
        self.assertEqual(missing, set())

    def test_collects_missing_variables(self):
        missing: set[str] = set()
        out = _expand_env("Bearer ${MCP_TEST_UNSET_VAR}", missing)
        self.assertEqual(out, "Bearer ")
        self.assertEqual(missing, {"MCP_TEST_UNSET_VAR"})

    def test_literal_text_untouched(self):
        missing: set[str] = set()
        self.assertEqual(_expand_env("plain $VAR {x} $}", missing), "plain $VAR {x} $}")
        self.assertEqual(missing, set())


class TestConnectionFailsClosed(unittest.TestCase):
    def test_unset_variable_blocks_connection(self):
        cfg = MCPServerConfig(
            name="canvas-test",
            transport="http",
            url="http://127.0.0.1:9/mcp",
            headers={"Authorization": "Bearer ${MCP_TEST_UNSET_VAR}"},
        )
        conn = MCPConnection(cfg)
        asyncio.run(conn.start())
        self.assertFalse(conn.connected)
        self.assertIn("MCP_TEST_UNSET_VAR", conn.error or "")


class TestRegistryKeepsPlaceholder(unittest.TestCase):
    def test_persisted_config_stores_placeholder_not_secret(self):
        os.environ["MCP_TEST_TOKEN"] = "sekret-value"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "servers.json"
                mgr = MCPManager(registry_path=str(path))
                mgr.configs["canvas-test"] = MCPServerConfig(
                    name="canvas-test",
                    transport="http",
                    url="http://127.0.0.1:9/mcp",
                    headers={"Authorization": "Bearer ${MCP_TEST_TOKEN}"},
                )
                mgr._persist()
                raw = json.loads(path.read_text(encoding="utf-8"))
        finally:
            del os.environ["MCP_TEST_TOKEN"]
        header = raw["servers"][0]["headers"]["Authorization"]
        self.assertEqual(header, "Bearer ${MCP_TEST_TOKEN}")
        self.assertNotIn("sekret-value", path.name + json.dumps(raw))


if __name__ == "__main__":
    unittest.main()
