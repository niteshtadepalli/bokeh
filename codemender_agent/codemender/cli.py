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

"""CodeMender CLI output parsing utilities for CodeMender Agent."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("codemender-orchestrator")


def parse_findings_json(json_str: str) -> List[Dict[str, Any]]:
  """Parses `cm report --format json` output handling empty strings for optional fields."""
  clean_str = json_str.strip()
  if not clean_str:
    return []

  # Find the start of the JSON array or object
  start_idx = clean_str.find("[")
  if start_idx == -1:
    start_idx = clean_str.find("{")

  if start_idx == -1:
    logger.error("No valid JSON array or object found in report.")
    return []

  try:
    decoder = json.JSONDecoder()
    data, _ = decoder.raw_decode(clean_str, start_idx)
  except json.JSONDecodeError as e:
    logger.error(
        "Failed to parse JSON findings report: %s\nOriginal string: %s",
        e,
        json_str,
    )
    return []

  if isinstance(data, dict):
    findings = data.get("findings", data.get("items", []))
  elif isinstance(data, list):
    findings = data
  else:
    findings = []

  cleaned_findings = []
  for item in findings:
    if not isinstance(item, dict):
      continue
    cleaned = {}
    for k, v in item.items():
      if v == "":
        cleaned[k] = None
      else:
        cleaned[k] = v
    cleaned_findings.append(cleaned)

  return cleaned_findings


def extract_session_id(find_stdout: str) -> Optional[str]:
  """Extracts the CodeMender session ID from 'cm find' output."""
  # Match UUID format session ID (e.g. Session: f7f7b492-3564-4dc0-bc8f-2020554ebe24)
  match = re.search(
      r"Session:\s*([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
      find_stdout,
      re.IGNORECASE,
  )
  if match:
    return match.group(1)
  return None
