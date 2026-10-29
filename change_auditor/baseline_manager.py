"""
Baseline Manager.
Handles establishing, capturing, storing, and loading baseline metadata.
Ensures we compare against a true prior working revision rather than the dirty working tree.
"""

import os
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Any, List, Optional
from change_auditor.config import BaselineConfig
from change_auditor.utils import get_repo_identifier, run_command, sanitize_dict_secrets


@dataclass
class BaselineMetadata:
    repo_id: str
    repo_path: str
    git_revision: str
    created_at: str
    passed: bool
    name: str = "default"
    routes: List[str] = field(default_factory=list)
    functional_results: Dict[str, Any] = field(default_factory=dict)
    screenshots: Dict[str, str] = field(default_factory=dict)  # route/viewport -> relative file path
    env_keys: List[str] = field(default_factory=list)  # only names, never values!
    startup_command: str = ""
    app_type: str = "custom"


class BaselineManager:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path.resolve()
        self.repo_id = get_repo_identifier(self.repo_path)
        legacy_dir = Path.home() / ".change-auditor" / "baselines" / self.repo_id
        # Keep using baselines captured before the ProofPush rename
        self.storage_dir = legacy_dir if legacy_dir.is_dir() else Path.home() / ".proofpush" / "baselines" / self.repo_id
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def resolve_baseline_sha(
        self,
        strategy: str,
        proposed_sha: str,
        remote_sha: Optional[str] = None,
        reference_ref: Optional[str] = None,
    ) -> Optional[str]:
        """Determine baseline Git commit SHA according to the configured strategy."""

        # 1. Captured baseline snapshot
        if strategy == "captured":
            latest = self.get_latest_baseline()
            if latest and latest.git_revision:
                return latest.git_revision

        # 2. Latest passed audit baseline
        if strategy == "latest-passed":
            latest = self.get_latest_passed_baseline()
            if latest and latest.git_revision:
                return latest.git_revision

        # 3. Explicit commit or reference ref
        if strategy == "commit" and reference_ref:
            code, out, _ = run_command(["git", "rev-parse", reference_ref], cwd=self.repo_path)
            if code == 0 and out.strip():
                return out.strip()

        # 4. Merge-base strategy (default and most reliable)
        # If remote_sha is given and valid, check merge-base with remote
        if remote_sha and remote_sha != "0000000000000000000000000000000000000000":
            code, out, _ = run_command(
                ["git", "merge-base", remote_sha, proposed_sha],
                cwd=self.repo_path
            )
            if code == 0 and out.strip():
                return out.strip()

        # If reference_ref is configured (e.g. origin/main)
        ref = reference_ref or "origin/main"
        code, out, _ = run_command(["git", "merge-base", ref, proposed_sha], cwd=self.repo_path)
        if code == 0 and out.strip():
            return out.strip()

        # If origin/main not found, try origin/master or main or master
        for candidate in ["origin/master", "main", "master"]:
            code, out, _ = run_command(["git", "merge-base", candidate, proposed_sha], cwd=self.repo_path)
            if code == 0 and out.strip():
                return out.strip()

        # Fallback to parent commit HEAD~1 if valid commit
        code, out, _ = run_command(["git", "rev-parse", f"{proposed_sha}^"], cwd=self.repo_path)
        if code == 0 and out.strip():
            return out.strip()

        # Fallback to captured baseline if exists
        latest = self.get_latest_baseline()
        if latest and latest.git_revision:
            return latest.git_revision

        return None

    def save_baseline(self, metadata: BaselineMetadata) -> Path:
        """Save baseline metadata and artifacts."""
        ts = int(time.time())
        filename = f"baseline_{metadata.git_revision[:10]}_{ts}.json"
        dest = self.storage_dir / filename

        # Ensure environment secrets are NEVER saved
        cleaned_env_keys = [k for k in metadata.env_keys if not any(s in k.lower() for s in ["key", "secret", "token", "pass"])]
        metadata.env_keys = cleaned_env_keys

        # Ensure no plaintext passwords in functional results
        metadata.functional_results = sanitize_dict_secrets(metadata.functional_results)

        data = asdict(metadata)
        dest.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Also update 'latest.json'
        latest_file = self.storage_dir / "latest.json"
        latest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

        return dest

    def get_latest_baseline(self) -> Optional[BaselineMetadata]:
        """Fetch the most recent baseline."""
        latest_file = self.storage_dir / "latest.json"
        if latest_file.is_file():
            try:
                data = json.loads(latest_file.read_text(encoding="utf-8"))
                return BaselineMetadata(**data)
            except Exception:
                pass

        baselines = self.list_baselines()
        return baselines[0] if baselines else None

    def get_latest_passed_baseline(self) -> Optional[BaselineMetadata]:
        """Fetch the most recent baseline that passed all tests."""
        for b in self.list_baselines():
            if b.passed:
                return b
        return None

    def list_baselines(self) -> List[BaselineMetadata]:
        """List all stored baselines ordered newest first."""
        results = []
        files = sorted(self.storage_dir.glob("baseline_*.json"), key=os.path.getmtime, reverse=True)
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                results.append(BaselineMetadata(**data))
            except Exception:
                continue
        return results
