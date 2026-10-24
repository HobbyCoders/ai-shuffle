"""Reruns must replace only their own shard's evidence, never the gate itself."""
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_workflows_namespace_attempts_and_select_before_validation() -> None:
    for filename, producer, gate, prefix, count in [
        ("windows-suite.yml", "desktop", "desktop-gate", "windows-desktop", 3),
        ("ci-backend.yml", "backend-shard", "backend", "backend-shard", 4),
    ]:
        jobs = yaml.load(
            (ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader,
        )["jobs"]
        upload = next(
            s for s in jobs[producer]["steps"] if s.get("uses") == "actions/upload-artifact@v4"
        )
        assert upload["with"]["name"] == prefix + "-${{ matrix.shard }}-a${{ github.run_attempt }}"
        assert upload["with"]["if-no-files-found"] == "error"
        steps = jobs[gate]["steps"]
        download = next(s for s in steps if s.get("uses") == "actions/download-artifact@v4")
        assert download["with"]["pattern"] == prefix + "-*-a*"
        select = next(s for s in steps if "shard_evidence.py" in s.get("run", ""))
        assert f"--prefix {prefix} --count {count}" in select["run"]
        assert steps.index(download) < steps.index(select) < len(steps) - 1
        assert "continue-on-error" not in select


def artifact(root: Path, name: str, content: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "result").write_text(content)
    return directory


@pytest.mark.parametrize("prefix,count", [("windows-desktop", 3), ("backend-shard", 4)])
def test_partial_rerun_selects_numeric_latest_per_shard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], prefix: str, count: int,
) -> None:
    from scripts.ci.shard_evidence import select_evidence
    source, target = tmp_path / "downloads", tmp_path / "selected"
    for shard in range(1, count + 1):
        artifact(source, f"{prefix}-{shard}-a1", "original")
    artifact(source, f"{prefix}-2-a2", "stale failure")
    artifact(source, f"{prefix}-2-a10", "fresh success")
    select_evidence(source, target, prefix, count)
    assert (target / f"{prefix}-2/result").read_text() == "fresh success"
    assert (target / f"{prefix}-1/result").read_text() == "original"
    assert len(list(target.iterdir())) == count
    assert f"Selected {prefix}-2-a10" in capsys.readouterr().out


def test_missing_shard_fails_without_partial_output(tmp_path: Path) -> None:
    from scripts.ci.shard_evidence import select_evidence
    artifact(tmp_path / "downloads", "windows-desktop-1-a1", "success")
    with pytest.raises(ValueError, match="missing.*2.*3"):
        select_evidence(tmp_path / "downloads", tmp_path / "selected", "windows-desktop", 3)
    assert not (tmp_path / "selected").exists()


def test_latest_failed_or_incomplete_evidence_cannot_fall_back(tmp_path: Path) -> None:
    from scripts.ci.shard_evidence import select_evidence
    from tests.scripts.test_ci_parallel_gates import desktop_evidence
    source = tmp_path / "downloads"
    files = sorted(p.relative_to(ROOT / "desktop").as_posix()
                   for p in (ROOT / "desktop/test").rglob("*.test.mjs"))
    for shard in range(1, 4):
        directory = artifact(source, f"windows-desktop-{shard}-a1", "old success")
        (directory / "inventory.json").write_text(json.dumps(files[shard - 1::3]))
        (directory / "desktop.tap").write_text("# tests 1\n# fail 0\n")
    newer = artifact(source, "windows-desktop-2-a2", "failure")
    (newer / "inventory.json").write_text(json.dumps(files[1::3]))
    (newer / "desktop.tap").write_text("# tests 1\n# fail 1\n")
    target = tmp_path / "failed"
    select_evidence(source, target, "windows-desktop", 3)
    with pytest.raises(AssertionError, match="failing shard"):
        desktop_evidence(target)
    (newer / "desktop.tap").unlink()
    target = tmp_path / "incomplete"
    select_evidence(source, target, "windows-desktop", 3)
    with pytest.raises(FileNotFoundError):
        desktop_evidence(target)


def test_selector_cli_preserves_nested_receipts_and_refuses_stale_destination(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys
    source, target = tmp_path / "downloads", tmp_path / "selected"
    artifact(source, "backend-shard-1-a2/backend-results", "receipt")
    command = [sys.executable, str(ROOT / "scripts/ci/shard_evidence.py"),
               "--source", str(source), "--destination", str(target),
               "--prefix", "backend-shard", "--count", "1"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Selected backend-shard-1-a2" in result.stdout
    assert (target / "backend-shard-1/backend-results/result").read_text() == "receipt"
    assert subprocess.run(command, capture_output=True).returncode != 0


def test_unversioned_evidence_is_never_used(tmp_path: Path) -> None:
    from scripts.ci.shard_evidence import select_evidence
    artifact(tmp_path / "downloads", "windows-desktop-1", "stale")
    with pytest.raises(ValueError, match="missing"):
        select_evidence(tmp_path / "downloads", tmp_path / "selected", "windows-desktop", 1)
