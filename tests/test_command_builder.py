# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import unittest
from unittest.mock import patch

from codemender_agent.utils import accumulate_model_token_usage
from codemender_agent.utils import build_cm_command
from codemender_agent.utils import parse_token_metric
from codemender_agent.utils import render_token_usage_markdown
from codemender_agent.utils import resolve_command_model


class TestCommandBuilder(unittest.TestCase):

  def test_accumulate_model_token_usage(self):
    usage = {}
    accumulate_model_token_usage(usage, "gemini-flash", {"in_tokens": 100, "out_tokens": 20, "total_tokens": 120})
    self.assertEqual(usage, {"gemini-flash": {"in_tokens": 100, "out_tokens": 20, "total_tokens": 120}})

    accumulate_model_token_usage(usage, "gemini-flash", {"in_tokens": 50, "out_tokens": 10, "total_tokens": 60})
    self.assertEqual(usage, {"gemini-flash": {"in_tokens": 150, "out_tokens": 30, "total_tokens": 180}})

    accumulate_model_token_usage(usage, "gemini-pro", {"in_tokens": 200, "out_tokens": 40, "total_tokens": 240})
    self.assertEqual(len(usage), 2)
    self.assertEqual(usage["gemini-pro"], {"in_tokens": 200, "out_tokens": 40, "total_tokens": 240})

    # None or non-dict handling
    accumulate_model_token_usage(usage, "gemini-pro", None)
    self.assertEqual(usage["gemini-pro"]["total_tokens"], 240)

  def test_render_token_usage_markdown(self):
    # Empty / None handling
    self.assertEqual(render_token_usage_markdown(None), "")
    self.assertEqual(render_token_usage_markdown({}), "")

    # Multi-model markdown table formatting
    totals = {
        "gemini-2.5-flash": {"in_tokens": 12000, "out_tokens": 500, "total_tokens": 12500},
        "gemini-2.5-pro": {"in_tokens": 45000, "out_tokens": 3200, "total_tokens": 48200},
    }
    md = render_token_usage_markdown(totals)
    self.assertIn("### ⚡ LLM Token Usage Summary", md)
    self.assertIn("- **Input Tokens:** 57,000", md)
    self.assertIn("- **Output Tokens:** 3,700", md)
    self.assertIn("- **Grand Total Tokens:** 60,700", md)
    self.assertIn("| Model | Input Tokens | Output Tokens | Total Tokens |", md)
    self.assertIn("| `gemini-2.5-flash` | 12,000 | 500 | 12,500 |", md)
    self.assertIn("| `gemini-2.5-pro` | 45,000 | 3,200 | 48,200 |", md)

  def test_parse_token_metric(self):
    self.assertEqual(parse_token_metric("41k"), 41000)
    self.assertEqual(parse_token_metric("41.5k"), 41500)
    self.assertEqual(parse_token_metric("1.2M"), 1200000)
    self.assertEqual(parse_token_metric("1.5G"), 1500000000)
    self.assertEqual(parse_token_metric("561"), 561)
    with self.assertRaises(ValueError):
      parse_token_metric("")
    with self.assertRaises(ValueError):
      parse_token_metric("abc")

  @patch.dict(os.environ, {}, clear=True)
  def test_resolve_command_model(self):
    self.assertIsNone(resolve_command_model("find"))

    with patch.dict(os.environ, {"CODEMENDER_MODEL": "gemini-2.0-flash"}):
      self.assertEqual(resolve_command_model("find"), "gemini-2.0-flash")

    with patch.dict(
        os.environ,
        {
            "CODEMENDER_MODEL": "gemini-2.0-flash",
            "CODEMENDER_FIND_MODEL": "gemini-2.0-pro",
        },
    ):
      self.assertEqual(resolve_command_model("find"), "gemini-2.0-pro")
      self.assertEqual(resolve_command_model("verify"), "gemini-2.0-flash")

  def test_build_cm_command_validation(self):
    with self.assertRaises(ValueError):
      build_cm_command("cm", "find", target_or_id=None)
    with self.assertRaises(ValueError):
      build_cm_command("cm", "verify", target_or_id="")
    with self.assertRaises(ValueError):
      build_cm_command("cm", "fix", target_or_id=None)

  @patch.dict(os.environ, {"CODEMENDER_CLI_VERSION": "preview"}, clear=True)
  def test_build_cm_command_preview(self):
    # find
    cmd = build_cm_command("cm", "find", ".")
    self.assertEqual(cmd, ["cm", "find", "-y", "."])

    # find with model
    with patch.dict(os.environ, {"CODEMENDER_FIND_MODEL": "gemini-pro"}):
      cmd = build_cm_command("cm", "find", ".")
      self.assertEqual(cmd, ["cm", "find", "-y", "--model", "gemini-pro", "."])

    # verify
    cmd = build_cm_command("cm", "verify", "id-123")
    self.assertEqual(cmd, ["cm", "verify", "-y", "--bypass-warning", "id-123"])

    # verify with skip exploit verification
    with patch.dict(os.environ, {"CODEMENDER_SKIP_EXPLOIT_VERIFICATION": "true"}):
      cmd = build_cm_command("cm", "verify", "id-123")
      self.assertEqual(
          cmd,
          [
              "cm",
              "verify",
              "-y",
              "--bypass-warning",
              "--skip-exploit-verification",
              "id-123",
          ],
      )

    # fix
    cmd = build_cm_command("cm", "fix", "id-123")
    self.assertEqual(cmd, ["cm", "fix", "-y", "--bypass-warning", "id-123"])

    # report
    cmd = build_cm_command("cm", "report", extra_flags=["-f", "html"])
    self.assertEqual(cmd, ["cm", "report", "-f", "html"])

  @patch.dict(os.environ, {"CODEMENDER_CLI_VERSION": "legacy"}, clear=True)
  def test_build_cm_command_legacy(self):
    # find
    cmd = build_cm_command("cm", "find", ".")
    self.assertEqual(cmd, ["cm", "find", "."])

    # verify
    cmd = build_cm_command("cm", "verify", "id-123")
    self.assertEqual(cmd, ["cm", "find", "verify", "id-123", "--yes"])

    # fix
    cmd = build_cm_command("cm", "fix", "id-123")
    self.assertEqual(cmd, ["cm", "fix", "id-123", "--yes"])

  @patch.dict(
      os.environ,
      {"CODEMENDER_CLI_VERSION": "preview", "CODEMENDER_SANDBOX_ENABLED": "false"},
      clear=True,
  )
  def test_build_cm_command_sandbox_disabled(self):
    # find omits --unrestricted so allowedRoots stays scoped to target_or_id
    cmd_find = build_cm_command("cm", "find", "src/bokeh/server/views")
    self.assertEqual(cmd_find, ["cm", "find", "-y", "src/bokeh/server/views"])

    # verify and fix include --unrestricted
    cmd_verify = build_cm_command("cm", "verify", "id-123")
    self.assertEqual(
        cmd_verify,
        ["cm", "verify", "-y", "--bypass-warning", "--unrestricted", "id-123"],
    )
    cmd_fix = build_cm_command("cm", "fix", "id-123")
    self.assertEqual(
        cmd_fix,
        ["cm", "fix", "-y", "--bypass-warning", "--unrestricted", "id-123"],
    )

  def test_empty_model_env_does_not_pass_model_flag(self):
    """Verify empty CODEMENDER_MODEL='' and CODEMENDER_FIND_MODEL='' do not pass --model to cm."""
    from codemender_agent.config import OrchestratorConfig

    with patch.dict(
        os.environ,
        {
            "CODEMENDER_CLI_VERSION": "preview",
            "CODEMENDER_MODEL": "   ",
            "CODEMENDER_FIND_MODEL": "",
            "CODEMENDER_FIX_MODEL": "",
        },
        clear=True,
    ):
      self.assertIsNone(resolve_command_model("find"))
      self.assertIsNone(resolve_command_model("fix"))
      cfg = OrchestratorConfig.from_env()
      self.assertIsNone(cfg.model)
      self.assertIsNone(cfg.find_model)
      self.assertIsNone(cfg.fix_model)
      cmd = build_cm_command("cm", "find", ".")
      self.assertNotIn("--model", cmd)

  def test_get_cm_default_model_and_binary_staging(self):
    """Verify dynamic default model detection from `cm find --help` and binary staging/restoration."""
    import tempfile
    from codemender_agent.codemender.cli import (
        _CM_DEFAULT_MODEL_CACHE,
        get_cm_default_model,
        restore_staged_cm_binary,
        stage_cm_binary_for_archive,
    )

    _CM_DEFAULT_MODEL_CACHE.clear()
    with tempfile.TemporaryDirectory() as tmpdir:
      fake_cm = os.path.join(tmpdir, "cm")
      with open(fake_cm, "w", encoding="utf-8") as f:
        f.write(
            '#!/bin/sh\necho \'      --model string     LLM model to use (default "gemini-3.8-flash")\'\n'
        )
      os.chmod(fake_cm, 0o755)

      detected = get_cm_default_model(fake_cm)
      self.assertEqual(detected, "gemini-3.8-flash")

      cm_home = os.path.join(tmpdir, ".codemender")
      staged = stage_cm_binary_for_archive(cm_home, fake_cm)
      self.assertTrue(staged and os.path.isfile(staged))

      install_dest = os.path.join(tmpdir, "installed_bin", "cm")
      os.makedirs(os.path.dirname(install_dest), exist_ok=True)
      restored = restore_staged_cm_binary(cm_home, install_path=install_dest)
      self.assertEqual(restored, install_dest)
      self.assertTrue(os.path.isfile(install_dest))


if __name__ == "__main__":
  unittest.main()

