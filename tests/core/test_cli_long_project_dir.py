"""Exact Claude Code directory names, including collisions and UTF-16 cwd hashes."""
import json
import subprocess

import pytest

from app.core.provider.fluid_transcript import (
    _cli_project_dir, _write_cli_transcript, find_cli_transcript,
)


@pytest.mark.parametrize("cwd", [
    "/repo/" + "long-workspace/" * 25,
    "C:\\workspace\\" + "feature_" * 40,
    "/repo/" + "nested/" * 40 + "astral-😀",
])
def test_matches_cli_javascript_algorithm(tmp_path, cwd):
    # Independently execute the installed CLI's JS algorithm (2.1.319), not
    # the implementation under test. It operates on UTF-16, not code points.
    script = r"""
const p=JSON.parse(process.argv[1]);
let name=p.replace(/[^a-zA-Z0-9]/g,"-");
if(name.length>200){let h=0;for(let i=0;i<p.length;i++)h=(h<<5)-h+p.charCodeAt(i)|0;
name=name.slice(0,200)+"-"+Math.abs(h).toString(36);}
process.stdout.write(name);
"""
    expected = subprocess.run(["node", "-e", script, json.dumps(cwd)],
                              text=True, capture_output=True, check=True).stdout
    assert _cli_project_dir(tmp_path, cwd).name == expected


def test_new_long_cwd_never_reuses_a_same_prefix_sibling(tmp_path):
    cwd = "/workspace/" + "long-segment/" * 20 + "checkout"
    sibling = _cli_project_dir(tmp_path, cwd + "-other")
    sibling.mkdir()
    (sibling / "fresh.jsonl").write_text("foreign")
    path = _write_cli_transcript([{"type": "user"}], "fresh", cwd, tmp_path)
    assert path.parent != sibling
    assert find_cli_transcript("fresh", tmp_path, cwd) == path
    path.unlink()
    assert find_cli_transcript("fresh", tmp_path, cwd) is None
