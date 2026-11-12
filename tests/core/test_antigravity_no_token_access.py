"""The app never reads, copies, stores or refreshes the Antigravity agent's sign-in.

Google's terms let a third-party client launch the unmodified ``agy`` binary
but not handle its credentials. Sign-in state comes from asking the agent over
ACP; sign-in runs the binary's own ``--login`` flow; sign-out removes the row's
private agent home without opening anything in it. No module names the
binary's token files or its keyring entry, and none calls Google's backend.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Names only a reader of the agent's sign-in, or a client of Google's private
# backend, would need.
_FORBIDDEN_MARKERS = (
    "oauth_creds.json",
    "acp_token.json",
    "mcp_oauth_tokens.json",
    "plugin-secrets.json",
    "google_accounts.json",
    "jetski-standalone-oauth-token",
    "cloudcode-pa.googleapis.com",
)
_SCANNED_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".cjs", ".svelte", ".rs", ".sh"}


def _offenders(root: Path) -> list[str]:
    """Every scanned file under *root* that names a forbidden marker."""
    paths = [root / rel for rel in ("run_local.py", "entrypoint.sh")]
    for top in ("app", "fluid/fluid", "frontend/src", "desktop/src", "browser-mcp/src", "tools"):
        if (root / top).is_dir():
            paths += [p for p in (root / top).rglob("*")
                      if p.suffix in _SCANNED_SUFFIXES and "node_modules" not in p.parts]
    found = []
    for path in paths:
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        text = path.read_text(encoding="utf-8", errors="ignore")
        found += [f"{rel}: {m}" for m in _FORBIDDEN_MARKERS if m in text]
    return found


def test_no_code_names_the_agent_sign_in():
    """No app, Fluid, frontend or desktop code names the agent's token files."""
    assert _offenders(ROOT) == []


def test_scan_catches_a_reader(tmp_path):
    """The scan is not vacuous: a reader anywhere it looks is caught."""
    (tmp_path / "app" / "core").mkdir(parents=True)
    (tmp_path / "app" / "core" / "x.py").write_text('P = "~/.gemini/oauth_creds.json"\n')
    (tmp_path / "fluid" / "fluid" / "engine").mkdir(parents=True)
    (tmp_path / "fluid" / "fluid" / "engine" / "y.py").write_text('K = "jetski-standalone-oauth-token"\n')
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "z.ts").write_text("fetch('https://cloudcode-pa.googleapis.com/')\n")
    assert sorted(_offenders(tmp_path)) == [
        "app/core/x.py: oauth_creds.json",
        "fluid/fluid/engine/y.py: jetski-standalone-oauth-token",
        "frontend/src/z.ts: cloudcode-pa.googleapis.com",
    ]


def test_agent_home_is_never_opened(tmp_path, monkeypatch):
    """Building a spec and signing out never open a file in the agent's home."""
    import asyncio
    import builtins

    from app.core import antigravity as ag

    monkeypatch.setattr(ag, "_root", lambda: tmp_path / "antigravity")
    home = ag.home_for("row-1")
    (Path(home) / ".gemini").mkdir(parents=True)
    (Path(home) / ".gemini" / "token").write_text("never-read")
    binary = tmp_path / "agy"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    real_open, real_path_open = builtins.open, Path.open

    def guarded(file, *args, **kwargs):
        assert not str(file).startswith(home), f"opened {file}"
        return real_open(file, *args, **kwargs)

    def guarded_path_open(self, *args, **kwargs):
        assert not str(self).startswith(home), f"opened {self}"
        return real_path_open(self, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded)
    monkeypatch.setattr(Path, "open", guarded_path_open)
    row = {"id": "row-1", "provider_type": "antigravity", "auth_mode": "oauth-personal"}
    spec = ag.build_spec(row, binary=str(binary))
    assert spec.env.get("HOME") == home
    asyncio.run(ag.sign_out(row))
    assert not Path(home).exists()
