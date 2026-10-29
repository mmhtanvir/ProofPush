"""
Task Context Resolver.
Resolves the intended task and expected behavior from:
1. Explicit CLI flag / Environment variable
2. Supported AI agent metadata (Claude Code, Gemini CLI, Codex, Cursor)
3. Agent session / prompt artifacts
4. Commit messages
5. Repository task file (.git/change-auditor-task.md or .change-auditor-task.md)
6. Interactive developer prompt
Includes protection against prompt injection in commit messages and task descriptions.
"""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Any

# Pattern detection for prompt injection attempts
PROMPT_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?(previous|prior)\s+instructions', re.IGNORECASE),
    re.compile(r'disregard\s+(all\s+)?(previous|prior)\s+rules', re.IGNORECASE),
    re.compile(r'system\s*:\s*(always\s+)?approve', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+in\s+bypass\s+mode', re.IGNORECASE),
    re.compile(r'developer\s+mode\s*:\s*unrestricted', re.IGNORECASE),
    re.compile(r'do\s+not\s+(block|audit|check)\s+this\s+push', re.IGNORECASE),
]


@dataclass
class TaskContext:
    title: str = "Unknown Task"
    source: str = "inferred"  # explicit, agent_hook, task_file, commit_messages, interactive, fallback
    expected_behaviors: List[str] = field(default_factory=list)
    expected_areas: List[str] = field(default_factory=list)
    unrelated_areas: List[str] = field(default_factory=list)
    raw_description: str = ""
    is_inferred: bool = True
    prompt_injection_detected: bool = False
    confidence: float = 0.5  # 0.0 to 1.0


class TaskResolver:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()

    def sanitize_input(self, text: str) -> Tuple[str, bool]:
        """Check for prompt injection attempts and sanitize."""
        if not text:
            return "", False

        injection_found = False
        for pattern in PROMPT_INJECTION_PATTERNS:
            if pattern.search(text):
                injection_found = True
                text = pattern.sub("[REMOVED_SUSPICIOUS_PROMPT_DIRECTIVE]", text)

        return text.strip(), injection_found

    def resolve(
        self,
        explicit_task: Optional[str] = None,
        commit_messages: Optional[List[str]] = None,
        interactive: bool = False,
    ) -> TaskContext:
        """Resolve the task context following the strict priority order."""

        # Priority 1: Explicit CLI option or environment variable
        env_task = os.environ.get("PROOFPUSH_TASK") or os.environ.get("CHANGE_AUDITOR_TASK")
        task_str = explicit_task or env_task
        if task_str and task_str.strip():
            clean, injected = self.sanitize_input(task_str)
            return TaskContext(
                title=clean.splitlines()[0][:120],
                source="explicit",
                expected_behaviors=[clean],
                raw_description=clean,
                is_inferred=False,
                prompt_injection_detected=injected,
                confidence=1.0,
            )

        # Priority 2: AI Agent Lifecycle Hooks & Metadata
        agent_task = self._check_ai_agent_metadata()
        if agent_task:
            return agent_task

        # Priority 3: Task Context File (.git/change-auditor-task.md or .change-auditor-task.md)
        file_task = self._read_task_file()
        if file_task:
            return file_task

        # Priority 4: Commit Messages
        if commit_messages and len(commit_messages) > 0:
            commit_task = self._parse_commit_messages(commit_messages)
            if commit_task:
                return commit_task

        # Priority 5: Interactive Question (if enabled and TTY)
        if interactive and sys.stdin.isatty():
            try:
                print("\n[proofpush] No task description found.")
                user_task = input("What change or feature were you working on? ").strip()
                if user_task:
                    clean, injected = self.sanitize_input(user_task)
                    return TaskContext(
                        title=clean.splitlines()[0][:120],
                        source="interactive",
                        expected_behaviors=[clean],
                        raw_description=clean,
                        is_inferred=False,
                        prompt_injection_detected=injected,
                        confidence=0.9,
                    )
            except Exception:
                pass

        # Fallback: Inferred generic task
        return TaskContext(
            title="General Website Update",
            source="fallback",
            expected_behaviors=["Maintain existing website functionality and prevent regressions."],
            raw_description="Automated baseline comparison against proposed changes.",
            is_inferred=True,
            confidence=0.3,
        )

    def _check_ai_agent_metadata(self) -> Optional[TaskContext]:
        """Check for agent metadata or recent task instructions from Claude Code, Gemini CLI, Cursor, etc."""
        # 1. Claude Code task / state
        claude_state = self.repo_path / ".claude" / "current_task.json"
        if claude_state.is_file():
            try:
                import json
                data = json.loads(claude_state.read_text(encoding="utf-8"))
                desc = data.get("task") or data.get("description")
                if desc:
                    clean, injected = self.sanitize_input(str(desc))
                    return TaskContext(
                        title=clean.splitlines()[0][:120],
                        source="agent_hook",
                        expected_behaviors=[clean],
                        raw_description=clean,
                        is_inferred=False,
                        prompt_injection_detected=injected,
                        confidence=0.95,
                    )
            except Exception:
                pass

        # 2. Antigravity / Gemini CLI session task
        gemini_task_file = self.repo_path / ".gemini" / "task.md"
        if gemini_task_file.is_file():
            try:
                content = gemini_task_file.read_text(encoding="utf-8").strip()
                if content:
                    clean, injected = self.sanitize_input(content)
                    return TaskContext(
                        title=clean.splitlines()[0][:120],
                        source="agent_hook",
                        expected_behaviors=[clean],
                        raw_description=clean,
                        is_inferred=False,
                        prompt_injection_detected=injected,
                        confidence=0.95,
                    )
            except Exception:
                pass

        return None

    def _read_task_file(self) -> Optional[TaskContext]:
        """Read and parse repository task file."""
        candidates = [
            self.repo_path / ".git" / "proofpush-task.md",
            self.repo_path / ".proofpush-task.md",
            self.repo_path / "proofpush-task.md",
            self.repo_path / ".git" / "change-auditor-task.md",
            self.repo_path / ".change-auditor-task.md",
            self.repo_path / "change-auditor-task.md",
        ]

        task_file = None
        for c in candidates:
            if c.is_file():
                task_file = c
                break

        if not task_file:
            return None

        try:
            content = task_file.read_text(encoding="utf-8")
            clean_content, injected = self.sanitize_input(content)

            task_title = "Repository Task"
            expected_behavior = []
            expected_areas = []
            unrelated_areas = []

            current_section = None
            for line in clean_content.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue

                if stripped.lower().startswith("task:"):
                    task_title = stripped[5:].strip()
                    current_section = "task"
                elif stripped.lower().startswith("expected behavior:") or stripped.lower() == "expected behavior":
                    current_section = "behavior"
                elif stripped.lower().startswith("expected areas:") or stripped.lower() == "expected areas":
                    current_section = "expected_areas"
                elif stripped.lower().startswith("potentially unrelated areas:") or stripped.lower() == "potentially unrelated areas":
                    current_section = "unrelated_areas"
                elif stripped.startswith("-") or stripped.startswith("*"):
                    item = stripped.lstrip("-* ").strip()
                    if current_section == "behavior":
                        expected_behavior.append(item)
                    elif current_section == "expected_areas":
                        expected_areas.append(item)
                    elif current_section == "unrelated_areas":
                        unrelated_areas.append(item)
                elif current_section == "task" and not expected_behavior:
                    task_title += " " + stripped

            return TaskContext(
                title=task_title,
                source="task_file",
                expected_behaviors=expected_behavior or [task_title],
                expected_areas=expected_areas,
                unrelated_areas=unrelated_areas,
                raw_description=clean_content,
                is_inferred=False,
                prompt_injection_detected=injected,
                confidence=0.9,
            )
        except Exception:
            return None

    def _parse_commit_messages(self, messages: List[str]) -> Optional[TaskContext]:
        """Synthesize task context from commit subjects and bodies."""
        combined_text = "\n".join(messages)
        clean_text, injected = self.sanitize_input(combined_text)

        first_subject = messages[0].splitlines()[0] if messages else "Code Update"
        # Remove conventional commit prefixes if present (e.g. feat:, fix:)
        cleaned_title = re.sub(r'^(feat|fix|refactor|perf|chore|test|docs)(\([^)]+\))?:\s*', '', first_subject, flags=re.IGNORECASE)

        behaviors = []
        for msg in messages:
            lines = msg.splitlines()
            subject = lines[0].strip()
            behaviors.append(subject)
            for line in lines[1:]:
                stripped = line.strip()
                if stripped.startswith("-") or stripped.startswith("*"):
                    behaviors.append(stripped.lstrip("-* ").strip())

        return TaskContext(
            title=cleaned_title,
            source="commit_messages",
            expected_behaviors=behaviors[:10],
            raw_description=clean_text,
            is_inferred=True,
            prompt_injection_detected=injected,
            confidence=0.7,
        )
