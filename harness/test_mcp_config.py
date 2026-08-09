"""Tests for mcp_config.py's config writers.

Golden-JSON checks: arm 1 (summary/series, stdio) must stay byte-identical
to the pre-arm-2 shape so arm 1 remains re-runnable identically. Arm 2
(tiered/flat, http) checks the schemas verified against the installed CLIs
(see mcp_config.py's module docstring for how those were verified).

Run: harness/.venv/bin/python -m unittest harness.test_mcp_config -v
     (from the repo root) or `cd harness && ../harness/.venv/bin/python -m
     unittest test_mcp_config -v`
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import mcp_config


FAKE_BINARY = "/fake/path/jaeger-mcp-bench-server"


def _load(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


class StdioBackwardCompatTests(unittest.TestCase):
    """Arm 1 (summary/series) configs must stay byte-identical to the
    pre-arm-2 shape: {"command", "args", "env"} for claude, {"command",
    "args"} for gemini, in that key order."""

    def setUp(self):
        patcher = mock.patch.object(mcp_config, "server_binary_path", return_value=FAKE_BINARY)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_claude_summary_golden(self):
        path = mcp_config.write_claude_config("summary", jaeger_url="http://localhost:16686")
        self.addCleanup(path.unlink)
        got = _load(path)
        want = {
            "mcpServers": {
                "jaeger-bench": {
                    "command": FAKE_BINARY,
                    "args": ["--format", "summary", "--jaeger-url", "http://localhost:16686"],
                    "env": {},
                }
            }
        }
        self.assertEqual(got, want)
        # key order matters for "byte-identical" - dict equality above
        # doesn't check it, so also assert insertion order explicitly.
        self.assertEqual(list(got["mcpServers"]["jaeger-bench"].keys()), ["command", "args", "env"])

    def test_claude_series_golden(self):
        path = mcp_config.write_claude_config("series", jaeger_url="http://localhost:16686")
        self.addCleanup(path.unlink)
        got = _load(path)
        self.assertEqual(got["mcpServers"]["jaeger-bench"]["args"],
                          ["--format", "series", "--jaeger-url", "http://localhost:16686"])
        self.assertIn("env", got["mcpServers"]["jaeger-bench"])

    def test_gemini_summary_golden(self):
        path = mcp_config.write_gemini_config("summary", jaeger_url="http://localhost:16686")
        self.addCleanup(path.unlink)
        got = _load(path)
        want = {
            "mcpServers": {
                "jaeger-bench": {
                    "command": FAKE_BINARY,
                    "args": ["--format", "summary", "--jaeger-url", "http://localhost:16686"],
                }
            }
        }
        self.assertEqual(got, want)
        self.assertNotIn("env", got["mcpServers"]["jaeger-bench"])
        self.assertEqual(list(got["mcpServers"]["jaeger-bench"].keys()), ["command", "args"])

    def test_custom_jaeger_url_passed_through_stdio(self):
        path = mcp_config.write_claude_config("summary", jaeger_url="http://example:9999")
        self.addCleanup(path.unlink)
        got = _load(path)
        self.assertIn("http://example:9999", got["mcpServers"]["jaeger-bench"]["args"])


class HttpConfigTests(unittest.TestCase):
    """Arm 2 (tiered/flat) http-type MCP configs.

    Schemas verified empirically:
      claude: `claude mcp add --transport http testhttp <url> -s local` wrote
              {"type": "http", "url": <url>} to ~/.claude.json.
      gemini: `gemini mcp add --transport http testhttp <url> -s project`
              wrote {"mcpServers": {"testhttp": {"httpUrl": <url>}}} to
              .gemini/settings.json.
    """

    def test_claude_tiered_golden(self):
        path = mcp_config.write_claude_config(
            "tiered", jaeger_url="http://localhost:16686", flat_url="http://localhost:8090/",
        )
        self.addCleanup(path.unlink)
        got = _load(path)
        want = {
            "mcpServers": {
                "jaeger-bench": {
                    "type": "http",
                    "url": "http://localhost:16686/api/ai/mcp/",
                }
            }
        }
        self.assertEqual(got, want)

    def test_claude_tiered_strips_trailing_slash_on_jaeger_url(self):
        path = mcp_config.write_claude_config("tiered", jaeger_url="http://localhost:16686/")
        self.addCleanup(path.unlink)
        got = _load(path)
        self.assertEqual(got["mcpServers"]["jaeger-bench"]["url"], "http://localhost:16686/api/ai/mcp/")

    def test_claude_flat_golden(self):
        path = mcp_config.write_claude_config(
            "flat", jaeger_url="http://localhost:16686", flat_url="http://localhost:8090/",
        )
        self.addCleanup(path.unlink)
        got = _load(path)
        want = {
            "mcpServers": {
                "jaeger-bench": {
                    "type": "http",
                    "url": "http://localhost:8090/",
                }
            }
        }
        self.assertEqual(got, want)

    def test_gemini_tiered_golden(self):
        path = mcp_config.write_gemini_config("tiered", jaeger_url="http://localhost:16686")
        self.addCleanup(path.unlink)
        got = _load(path)
        want = {
            "mcpServers": {
                "jaeger-bench": {
                    "httpUrl": "http://localhost:16686/api/ai/mcp/",
                }
            }
        }
        self.assertEqual(got, want)

    def test_gemini_flat_golden(self):
        path = mcp_config.write_gemini_config("flat", flat_url="http://localhost:8090/")
        self.addCleanup(path.unlink)
        got = _load(path)
        want = {
            "mcpServers": {
                "jaeger-bench": {
                    "httpUrl": "http://localhost:8090/",
                }
            }
        }
        self.assertEqual(got, want)

    def test_gemini_flat_custom_url(self):
        path = mcp_config.write_gemini_config("flat", flat_url="http://localhost:9123/mcp")
        self.addCleanup(path.unlink)
        got = _load(path)
        self.assertEqual(got["mcpServers"]["jaeger-bench"]["httpUrl"], "http://localhost:9123/mcp")

    def test_http_configs_do_not_call_server_binary_path(self):
        # tiered/flat must not require the go binary to exist at all.
        with mock.patch.object(mcp_config, "server_binary_path",
                                side_effect=AssertionError("should not be called for http formats")):
            path = mcp_config.write_claude_config("tiered")
            self.addCleanup(path.unlink)
            path2 = mcp_config.write_gemini_config("flat")
            self.addCleanup(path2.unlink)


class UnknownFormatTests(unittest.TestCase):
    def test_claude_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            mcp_config.write_claude_config("bogus")

    def test_gemini_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            mcp_config.write_gemini_config("bogus")


if __name__ == "__main__":
    unittest.main()
