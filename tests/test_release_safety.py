from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def test_repository_contains_no_competition_payload() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden = {"oracles", "reference-outputs", "private-partitions.json", "mcp-access.json"}
    assert not any(path.name in forbidden for path in root.rglob("*"))

    git_bin = shutil.which("git")
    if git_bin and (root / ".git").exists():
        proc = subprocess.run(
            [git_bin, "ls-files", "case-set.json", "inputs", "outputs"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        tracked = []
        for line in proc.stdout.splitlines():
            item = line.strip().replace("\\", "/")
            if item == "case-set.json" or (
                item.endswith(".json")
                and (item.startswith("inputs/") or item.startswith("outputs/"))
            ):
                tracked.append(item)
        assert not tracked, f"Competition payload tracked in git: {tracked}"
    elif not (root / "case-set.json").exists():
        assert list((root / "inputs").glob("*.json")) == []
        assert list((root / "outputs").glob("*.json")) == []


def test_example_environment_has_no_real_key() -> None:
    root = Path(__file__).resolve().parents[1]
    content = (root / ".env.example").read_text(encoding="utf-8")
    assert "sk-team-replace_me" in content
    assert content.count("sk-team-") == 1

