"""App-produced reseed files must be readable by the dev loop's actual CLI."""
import json

from app.core.provider.fluid_transcript import build_cli_transcript_entries
from scripts.devloop import fake_claude
from tests.scripts.test_devloop_fake_claude import Cli


def test_resume_reads_real_cli_entries_after_app_reseed(tmp_path):
    cli = Cli(tmp_path)
    cli.turn("Retain this context.")
    old_sid = cli.init["session_id"]
    cli.close()
    files = list((tmp_path / "home" / ".claude" / "projects").glob(f"*/{old_sid}.jsonl"))
    assert len(files) == 1
    original = [json.loads(line) for line in files[0].read_text().splitlines()]
    sid = "11111111-2222-4333-8444-555555555555"
    # The real app producer deliberately writes no fakeRow metadata.
    entries = build_cli_transcript_entries(original, sid, str(tmp_path / "ws"))
    assert len(entries) == 2 and not any("fakeRow" in entry for entry in entries)
    files[0].with_name(f"{sid}.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    resumed = Cli(tmp_path, "--resume", sid)
    try:
        resumed.turn("After rewind.")
    finally:
        resumed.close()
    log = [json.loads(line) for line in fake_claude.log_path(tmp_path).read_text().splitlines()]
    init = next(entry for entry in log if entry["kind"] == "init" and entry["session_id"] == sid)
    assert init["resumed"] and init["rows"] == 2
    assert init["history"][0] == {"role": "user", "text": "Retain this context."}
    assert "Retain this context" in init["history"][1]["text"]
