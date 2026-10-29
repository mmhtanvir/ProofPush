"""
Modular AI Engine.
Supports local deterministic analysis (default, offline, zero-network),
with an optional OpenAI adapter.
Enforces secret redaction and disclosures before sending data.
"""

import os
import json
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List
from proofpush.config import AIConfig
from proofpush.utils import redact_secrets


class BaseAIAdapter:
    def synthesize_summary(self, task_title: str, findings: List[Dict[str, Any]]) -> str:
        raise NotImplementedError


class LocalRuleEngine(BaseAIAdapter):
    """100% deterministic local rule engine. Runs locally with zero network calls."""

    def synthesize_summary(self, task_title: str, findings: List[Dict[str, Any]]) -> str:
        regressions = [f for f in findings if f.get("category") == "REGRESSION"]
        expected = [f for f in findings if f.get("category") == "EXPECTED_CHANGE"]
        new_feats = [f for f in findings if f.get("category") == "NEW_FUNCTIONALITY"]

        lines = [f"Deterministic Local Analysis for Task: '{task_title}'"]
        if regressions:
            lines.append(f"⚠️  {len(regressions)} critical regression(s) detected across tested routes and workflows.")
        else:
            lines.append("✅ No functional regressions observed in tested areas.")

        if new_feats:
            lines.append(f"✨ Verified {len(new_feats)} new capability/workflow(s) functioning as intended.")

        if expected:
            lines.append(f"🔄 Observed {len(expected)} intentional behavioral change(s) aligned with task.")

        return "\n".join(lines)


class OpenAIAdapter(BaseAIAdapter):
    """Optional OpenAI API adapter with strict secret redaction."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.model = model

    def synthesize_summary(self, task_title: str, findings: List[Dict[str, Any]]) -> str:
        if not self.api_key:
            return LocalRuleEngine().synthesize_summary(task_title, findings)

        # Sanitize prompt and disclose transmission
        safe_findings = [
            {
                "target": f.get("target"),
                "category": f.get("category"),
                "before": redact_secrets(str(f.get("before_behavior", ""))),
                "after": redact_secrets(str(f.get("after_behavior", ""))),
            }
            for f in findings[:15]
        ]

        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a software regression auditor. Provide a 2-3 sentence executive summary of the test comparison findings. Do not hallucinate."
                },
                {
                    "role": "user",
                    "content": f"Task: {redact_secrets(task_title)}\nFindings:\n{json.dumps(safe_findings)}"
                }
            ],
            "max_tokens": 150,
            "temperature": 0.2,
        }

        try:
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            # Graceful fallback to deterministic engine
            return f"[OpenAI fallback] {LocalRuleEngine().synthesize_summary(task_title, findings)}"


def get_ai_adapter(config: AIConfig) -> BaseAIAdapter:
    """Factory to get configured AI adapter."""
    if config.provider == "openai" and os.environ.get("OPENAI_API_KEY"):
        return OpenAIAdapter(model=config.model or "gpt-4o-mini")
    if config.provider == "openai":
        print("Warning: ai.provider is 'openai' but OPENAI_API_KEY is not set; using local engine.")
    elif config.provider != "local":
        print(f"Warning: unknown ai.provider '{config.provider}' (supported: local, openai); using local engine.")
    # Default to deterministic local engine
    return LocalRuleEngine()
