"""Tests for budget_exhausted detection in claude_runner.py / gemini_runner.py.

subprocess.run is mocked - no live CLI or fixture needed. The mocked stdout/
stderr shapes are the ones verified/reverse-engineered from the installed
CLI binaries (see claude_runner.py's and gemini_runner.py's inline comments
for sourcing); these tests pin the harness's *parsing* of those shapes, not
the shapes themselves.

Run: harness/.venv/bin/python -m unittest test_budget_exhausted -v
     (from harness/)
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import claude_runner
import gemini_runner


def _completed(returncode=0, stdout="", stderr=""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def _fake_mcp_config_path() -> Path:
    """gemini_runner.run() shutil.copyfile()s the config into a staged
    workspace before invoking the CLI, so (unlike claude_runner, which only
    passes the path as a CLI arg string) it needs the file to actually
    exist."""
    fd, path = tempfile.mkstemp(suffix=".json")
    import os
    os.close(fd)
    Path(path).write_text("{}")
    return Path(path)


class ClaudeInfraErrorTests(unittest.TestCase):
    """Regression for run d78bb27b: the CLI reports transport failures as a
    NORMAL success envelope whose result text starts with "API Error:", exit
    code 0 - 18/36 trials were scored against ground truth that way."""

    _outage_payload = json.dumps({
        "type": "result", "subtype": "success",
        "result": "API Error: Unable to connect to API (ENOTIMP)",
        "num_turns": 1, "usage": {"input_tokens": 0, "output_tokens": 0},
    })

    def test_api_error_answer_classified_and_retried(self):
        ok_payload = json.dumps({
            "type": "result", "subtype": "success",
            "result": "real answer", "num_turns": 3,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        calls = [
            _completed(0, self._outage_payload),
            _completed(0, ok_payload),
        ]
        with mock.patch("subprocess.run", side_effect=calls), \
             mock.patch("time.sleep") as slept:
            r = claude_runner.run(prompt="x", mcp_config_path=Path("/fake.json"))
        self.assertTrue(r.success)
        self.assertEqual(r.answer, "real answer")
        slept.assert_called()  # retried after a backoff, not immediately

    def test_persistent_outage_returns_infra_error_with_empty_answer(self):
        calls = [_completed(0, self._outage_payload)] * (1 + claude_runner._INFRA_RETRIES)
        with mock.patch("subprocess.run", side_effect=calls), \
             mock.patch("time.sleep"):
            r = claude_runner.run(prompt="x", mcp_config_path=Path("/fake.json"))
        self.assertFalse(r.success)
        self.assertEqual(r.error, claude_runner.INFRA_ERROR)
        self.assertEqual(r.answer, "")  # no scorer must ever see the error text
        self.assertIn("API Error", r.raw_output.get("result", ""))


class ClaudeBudgetExhaustedTests(unittest.TestCase):
    def test_normal_success_not_flagged(self):
        payload = {
            "type": "result", "subtype": "success",
            "result": "the answer", "num_turns": 3,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        with mock.patch("subprocess.run", return_value=_completed(0, json.dumps(payload))):
            r = claude_runner.run(prompt="x", mcp_config_path=Path("/fake.json"))
        self.assertFalse(r.budget_exhausted)
        self.assertTrue(r.success)
        self.assertEqual(r.answer, "the answer")
        self.assertIsNone(r.error)

    def test_error_max_budget_usd_flagged(self):
        payload = {
            "type": "result", "subtype": "error_max_budget_usd",
            "is_error": True, "result": "", "num_turns": 7,
            "usage": {"input_tokens": 900, "output_tokens": 10},
            "errors": ["Reached maximum budget ($0.50)"],
        }
        # Exercise both plausible exit-code conventions - the harness must
        # not depend on which one the CLI actually uses (unverified without
        # a live trial; see gemini_runner comment for the same caveat).
        for rc in (0, 1):
            with self.subTest(returncode=rc):
                with mock.patch("subprocess.run", return_value=_completed(rc, json.dumps(payload))):
                    r = claude_runner.run(prompt="x", mcp_config_path=Path("/fake.json"))
                self.assertTrue(r.budget_exhausted)
                self.assertTrue(r.success)  # mechanically got a structured result
                self.assertEqual(r.error, "error_max_budget_usd")
                self.assertEqual(r.input_tokens, 900)

    def test_error_max_turns_not_flagged_as_budget(self):
        payload = {
            "type": "result", "subtype": "error_max_turns",
            "is_error": True, "result": "", "num_turns": 50,
            "usage": {"input_tokens": 500, "output_tokens": 5},
        }
        with mock.patch("subprocess.run", return_value=_completed(1, json.dumps(payload))):
            r = claude_runner.run(prompt="x", mcp_config_path=Path("/fake.json"))
        self.assertFalse(r.budget_exhausted)
        self.assertEqual(r.error, "error_max_turns")

    def test_hard_crash_no_json_still_handled(self):
        with mock.patch("subprocess.run", return_value=_completed(1, "", "segfault")):
            r = claude_runner.run(prompt="x", mcp_config_path=Path("/fake.json"))
        self.assertFalse(r.success)
        self.assertFalse(r.budget_exhausted)
        self.assertEqual(r.error, "rc=1")

    def test_timeout(self):
        import subprocess as sp
        with mock.patch("subprocess.run", side_effect=sp.TimeoutExpired(cmd="claude", timeout=180)):
            r = claude_runner.run(prompt="x", mcp_config_path=Path("/fake.json"), timeout_sec=180)
        self.assertFalse(r.success)
        self.assertFalse(r.budget_exhausted)
        self.assertEqual(r.error, "timeout")


class GeminiBudgetExhaustedTests(unittest.TestCase):
    def setUp(self):
        self.cfg = _fake_mcp_config_path()
        self.addCleanup(lambda: self.cfg.unlink(missing_ok=True))

    def test_normal_success_not_flagged(self):
        payload = {"response": "the answer", "stats": {"models": {}, "tools": {"totalCalls": 0}}}
        with mock.patch("subprocess.run", return_value=_completed(0, json.dumps(payload))):
            r = gemini_runner.run(prompt="x", mcp_config_path=self.cfg)
        self.assertFalse(r.budget_exhausted)
        self.assertTrue(r.success)

    def test_fatal_turn_limited_error_flagged(self):
        stderr = json.dumps({
            "error": {
                "type": "FatalTurnLimitedError",
                "message": "Reached max session turns for this session.",
                "code": 53,
            }
        })
        with mock.patch("subprocess.run", return_value=_completed(53, "", stderr)):
            r = gemini_runner.run(prompt="x", mcp_config_path=self.cfg)
        self.assertTrue(r.budget_exhausted)
        self.assertFalse(r.success)
        self.assertIn("FatalTurnLimitedError", r.error)

    def test_context_overflow_message_pattern_flagged(self):
        stderr = json.dumps({
            "error": {
                "type": "Error",
                "message": "[API Error: The input token count (500000) exceeds the "
                            "maximum number of tokens allowed (200000). (Status: INVALID_ARGUMENT)]",
                "code": 1,
            }
        })
        with mock.patch("subprocess.run", return_value=_completed(1, "", stderr)):
            r = gemini_runner.run(prompt="x", mcp_config_path=self.cfg)
        self.assertTrue(r.budget_exhausted)

    def test_unrelated_api_error_not_flagged(self):
        stderr = json.dumps({
            "error": {
                "type": "Error",
                "message": "[API Error: Service unavailable (Status: UNAVAILABLE)]",
                "code": 1,
            }
        })
        with mock.patch("subprocess.run", return_value=_completed(1, "", stderr)):
            r = gemini_runner.run(prompt="x", mcp_config_path=self.cfg)
        self.assertFalse(r.budget_exhausted)

    def test_non_json_stderr_does_not_crash(self):
        with mock.patch("subprocess.run", return_value=_completed(1, "", "raw crash text, not json")):
            r = gemini_runner.run(prompt="x", mcp_config_path=self.cfg)
        self.assertFalse(r.success)
        self.assertFalse(r.budget_exhausted)


if __name__ == "__main__":
    unittest.main()
