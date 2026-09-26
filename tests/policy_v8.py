"""Pin legacy suites to the policy-v8 era they assert.

Most existing suites were written against the v8 live policy (size 30, $300
gross, stay-best placement). Importing this module points the default policy
path at an exact copy of that file, so those suites keep testing the v8 code
paths that remain supported. The v9 live policy is tested explicitly through
``LIVE_POLICY`` in the v9 suites. Import this before any
``from gemini_bot.config import DEFAULT_POLICY_PATH``.
"""
from pathlib import Path

import gemini_bot.config
import gemini_bot.orchestrator
import gemini_bot.stage_queue_pilot

V8_POLICY = Path(__file__).with_name("fixtures_live_policy_v8.json")
LIVE_POLICY = Path(gemini_bot.config.__file__).resolve().parents[2] / "config" / "live_policy.json"

for _module in (gemini_bot.config, gemini_bot.orchestrator, gemini_bot.stage_queue_pilot):
    _module.DEFAULT_POLICY_PATH = V8_POLICY
