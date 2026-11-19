"""Self-tests for scripts/changelog.py with this repository's flat `- [TAG] text` configuration."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "changelog.py"
CHANGELOG = "# Changelog\n\n## Unreleased\n\n- [FIX] Old fix.\n\n## 1.0.0\n"


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def test_this_repository_notes_pass_the_check() -> None:
    done = run("python3", str(TOOL), "check", cwd=ROOT)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_configuration_names_the_five_tags_and_the_flat_form() -> None:
    config = json.loads((ROOT / "changelog.d" / "sections.json").read_text())
    assert config["sections"] == ["BREAKING", "FEATURE", "FIX", "INTERNAL", "SECURITY"]
    assert config["style"] == "flat"


def test_assemble_keeps_the_tag_and_the_guard_accepts_the_fold(tmp_path: Path) -> None:
    (tmp_path / "changelog.d").mkdir()
    config = (ROOT / "changelog.d" / "sections.json").read_text()
    (tmp_path / "changelog.d" / "sections.json").write_text(config)
    (tmp_path / "changelog.d" / "n.md").write_text("- [INTERNAL] New note.\n")
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.invalid"),
        ("config", "user.name", "t"),
        ("config", "commit.gpgsign", "false"),
        ("add", "-A"),
        ("commit", "-q", "-m", "base"),
    ):
        assert run("git", *args, cwd=tmp_path).returncode == 0
    tool = ["python3", str(TOOL), "--root", str(tmp_path)]
    assert run(*tool, "assemble", cwd=tmp_path).returncode == 0
    assert (tmp_path / "CHANGELOG.md").read_text() == (
        "# Changelog\n\n## Unreleased\n\n- [FIX] Old fix.\n- [INTERNAL] New note.\n\n## 1.0.0\n"
    )
    run("git", "add", "-A", cwd=tmp_path)
    run("git", "commit", "-q", "-m", "assemble", cwd=tmp_path)
    assert run(*tool, "check", "--base", "HEAD^1", cwd=tmp_path).returncode == 0
    edited = (
        (tmp_path / "CHANGELOG.md")
        .read_text()
        .replace("- [FIX] Old fix.\n", "- [FIX] Old fix.\n- [FIX] Direct.\n")
    )
    (tmp_path / "CHANGELOG.md").write_text(edited)
    run("git", "commit", "-qam", "direct", cwd=tmp_path)
    assert run(*tool, "check", "--base", "HEAD^1", cwd=tmp_path).returncode == 1
