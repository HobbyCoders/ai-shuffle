"""Runtime provisioning for a fresh worktree, and the secrets it must refuse.

A worktree starts with none of the things a run needs, because all of them are
gitignored: node_modules, the virtualenv, the built frontend assets — and the
`.env`. Agents working in worktrees filled the gap by hand, and one of them
copied the production `.env`, putting a plaintext database password on disk in
the worktree and silently pointing the branch's app at the real database.

So: link what a run needs, never copy a credential, and pass configuration by
env-var reference at spawn instead.
"""

from pathlib import Path

import pytest

import app.core.checkout_manager as cm


@pytest.fixture(autouse=True)
def no_real_npx(monkeypatch):
    """The suite must not depend on a node toolchain being installed."""
    monkeypatch.setattr(cm, "_run_svelte_kit_sync", lambda package_dir: True)


def _seed_source(root):
    """A source checkout holding every gitignored runtime directory."""
    root.mkdir(parents=True, exist_ok=True)
    for rel in ("venv", "app/static"):
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "marker.txt").write_text(rel, encoding="utf-8")
    (root / ".env").write_text("POSTGRES_PASSWORD=hunter2\n", encoding="utf-8")
    return root


# ─── what a runnable worktree gets ───────────────────────────────────────────


def test_links_the_virtualenv_and_built_assets(tmp_path):
    source = _seed_source(tmp_path / "src")
    dest = tmp_path / "wt"
    dest.mkdir()

    summary = cm.provision_worktree_runtime(str(source), str(dest))

    assert "venv" in summary["linked"]
    assert "app/static" in summary["linked"]
    # Linked, not copied: the content is readable through the worktree.
    assert (dest / "venv" / "marker.txt").read_text(encoding="utf-8") == "venv"
    assert (dest / "app" / "static" / "marker.txt").exists()


def test_a_directory_the_source_lacks_is_skipped_not_an_error(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    dest = tmp_path / "wt"
    dest.mkdir()

    summary = cm.provision_worktree_runtime(str(source), str(dest))

    assert summary["errors"] == []
    assert any("venv" in s for s in summary["skipped"])


def test_an_existing_directory_in_the_worktree_is_left_alone(tmp_path):
    source = _seed_source(tmp_path / "src")
    dest = tmp_path / "wt"
    (dest / "venv").mkdir(parents=True)
    (dest / "venv" / "mine.txt").write_text("do not touch", encoding="utf-8")

    cm.provision_worktree_runtime(str(source), str(dest))

    assert (dest / "venv" / "mine.txt").exists()
    assert not (dest / "venv" / "marker.txt").exists()


def test_provisioning_never_places_a_config_file(tmp_path):
    """The whole point: a worktree must not end up holding a database password."""
    source = _seed_source(tmp_path / "src")
    dest = tmp_path / "wt"
    dest.mkdir()

    cm.provision_worktree_runtime(str(source), str(dest))

    assert not (dest / ".env").exists()


def test_provisioning_survives_a_link_failure(tmp_path, monkeypatch):
    """A degraded worktree must not cost anyone a checkout."""
    source = _seed_source(tmp_path / "src")
    dest = tmp_path / "wt"
    dest.mkdir()

    def boom(src, dst):
        raise OSError("no privilege")

    monkeypatch.setattr(cm, "_link_directory", boom)

    summary = cm.provision_worktree_runtime(str(source), str(dest))

    assert summary["linked"] == []
    assert any("link failed" in e for e in summary["errors"])




def test_provisioner_publishes_receipt_for_session_runtime_facts(tmp_path):
    import subprocess
    from fluid.context.runtime_facts import runtime_facts

    source = tmp_path / "source"
    source.mkdir()
    dest = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    summary = cm.provision_worktree_runtime(str(source), str(dest))
    facts = runtime_facts(str(dest))
    assert "Last provisioning outcome:" in facts
    assert f"skipped={len(summary['skipped'])}" in facts
    assert "Project Python candidates: none" in facts
    assert not (dest / ".env").exists()


# ─── the denylist ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [".env", "app/.env", "prod.env", "config/local.env", r"win\path\.env"],
)
def test_config_files_are_never_provisioned(name):
    assert cm._is_never_provisioned(name)


@pytest.mark.parametrize(
    "name", ["venv", "app/static", ".env.example", "README.md", "envoy.py"]
)
def test_ordinary_paths_are_provisionable(name):
    assert not cm._is_never_provisioned(name)


# ─── configuration by reference ──────────────────────────────────────────────


def test_spawn_env_points_at_the_config_file_instead_of_copying_it(monkeypatch):
    """A worktree process learns WHERE the config is, never what is in it."""
    import app.core.environment as env_mod

    monkeypatch.setattr(env_mod, "ENV_FILE_PATH", r"D:\Projects\ai-shuffle\.env")

    built = env_mod._build_env_with_ai_tools(None, None)

    assert built[env_mod.ENV_FILE_REF_VAR] == r"D:\Projects\ai-shuffle\.env"
    # The reference is a path, not the secret it points at.
    assert "hunter2" not in repr(built)


def test_no_config_file_means_no_reference(monkeypatch):
    """A container configured purely from the process environment needs none."""
    import app.core.environment as env_mod

    monkeypatch.setattr(env_mod, "ENV_FILE_PATH", None)

    built = env_mod._build_env_with_ai_tools(None, None)

    assert env_mod.ENV_FILE_REF_VAR not in built


def test_an_explicit_reference_from_the_profile_wins(monkeypatch):
    import app.core.environment as env_mod

    monkeypatch.setattr(env_mod, "ENV_FILE_PATH", "/app/.env")

    built = env_mod._build_env_with_ai_tools(
        {env_mod.ENV_FILE_REF_VAR: "/custom/.env"}, None
    )

    assert built[env_mod.ENV_FILE_REF_VAR] == "/custom/.env"


def test_config_resolves_the_referenced_file_when_it_exists(tmp_path, monkeypatch):
    from app.core.config import ENV_FILE_REF_VAR, _resolve_env_file

    ref = tmp_path / "canonical.env"
    ref.write_text("X=1\n", encoding="utf-8")
    monkeypatch.setenv(ENV_FILE_REF_VAR, str(ref))

    assert _resolve_env_file() == str(ref)


def test_config_falls_back_when_the_reference_is_missing(tmp_path, monkeypatch):
    """A dangling reference is the old position, not a dead process."""
    from app.core.config import ENV_FILE_REF_VAR, _resolve_env_file

    monkeypatch.setenv(ENV_FILE_REF_VAR, str(tmp_path / "gone.env"))

    assert _resolve_env_file() == ".env"


def test_config_falls_back_when_no_reference_is_set(monkeypatch):
    from app.core.config import ENV_FILE_REF_VAR, _resolve_env_file

    monkeypatch.delenv(ENV_FILE_REF_VAR, raising=False)

    assert _resolve_env_file() == ".env"


# ─── severing the links before the worktree is deleted ───────────────────────

# On Windows the links are junctions, and `git worktree remove` recurses
# through a junction into its target: removing a provisioned worktree gutted
# the main checkout's entire frontend/node_modules (verified incident,
# git 2.50.1.windows.1, with and without --force). The app's removal path must
# therefore detach every link before git touches the directory.


def test_severing_detaches_the_links_and_the_source_survives(tmp_path):
    source = _seed_source(tmp_path / "src")
    (source / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (source / "frontend" / "node_modules" / "pkg" / "index.js").write_text(
        "hello", encoding="utf-8"
    )
    dest = tmp_path / "wt"
    (dest / "frontend").mkdir(parents=True)
    (dest / "frontend" / "package.json").write_text("{}", encoding="utf-8")

    summary = cm.provision_worktree_runtime(str(source), str(dest))
    assert "frontend" in summary["linked"]

    severed = cm.sever_runtime_links(str(dest))

    assert severed["errors"] == []
    assert set(severed["severed"]) >= {
        str(Path("frontend") / "node_modules"),
        "venv",
    }
    # The link entries are gone from the worktree...
    assert not (dest / "frontend" / "node_modules").exists()
    assert not (dest / "venv").exists()
    # ...and the source kept every byte.
    assert (
        source / "frontend" / "node_modules" / "pkg" / "index.js"
    ).read_text(encoding="utf-8") == "hello"
    assert (source / "venv" / "marker.txt").exists()
    # The worktree's own real files are untouched.
    assert (dest / "frontend" / "package.json").exists()


def test_severing_leaves_a_real_directory_alone(tmp_path):
    dest = tmp_path / "wt"
    (dest / "venv").mkdir(parents=True)
    (dest / "venv" / "mine.txt").write_text("real install", encoding="utf-8")

    severed = cm.sever_runtime_links(str(dest))

    assert severed["severed"] == []
    assert severed["errors"] == []
    assert (dest / "venv" / "mine.txt").exists()


def test_severing_an_unprovisioned_or_missing_worktree_is_a_quiet_noop(tmp_path):
    empty = tmp_path / "wt"
    empty.mkdir()
    assert cm.sever_runtime_links(str(empty)) == {"severed": [], "errors": []}
    assert cm.sever_runtime_links(str(tmp_path / "never-existed")) == {
        "severed": [],
        "errors": [],
    }


def test_git_worktree_remove_cannot_gut_the_source(tmp_path):
    """The incident, end to end: provision, then remove through the app path."""
    import subprocess

    from app.core.git_service import git_service

    main = tmp_path / "main"
    (main / "frontend").mkdir(parents=True)

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(main), check=True,
            capture_output=True, text=True,
        )

    git("init")
    git("config", "user.email", "t@t.test")
    git("config", "user.name", "T")
    (main / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (main / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    (main / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (main / "frontend" / "node_modules" / "pkg" / "index.js").write_text(
        "hello", encoding="utf-8"
    )

    wt = tmp_path / "wt"
    git("worktree", "add", str(wt), "-b", "feature")
    summary = cm.provision_node_modules(str(main), str(wt))
    assert "frontend" in summary["linked"]

    assert git_service.remove_worktree(str(main), str(wt), force=True)

    assert not wt.exists()
    assert (
        main / "frontend" / "node_modules" / "pkg" / "index.js"
    ).read_text(encoding="utf-8") == "hello"


def test_git_worktree_remove_aborts_if_links_cannot_be_severed(tmp_path, monkeypatch):
    """A locked junction must not be handed to git — that is the gutting path."""
    import subprocess

    from app.core.git_service import git_service

    main = tmp_path / "main"
    (main / "frontend").mkdir(parents=True)

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(main), check=True,
            capture_output=True, text=True,
        )

    git("init")
    git("config", "user.email", "t@t.test")
    git("config", "user.name", "T")
    (main / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (main / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    git("add", "-A")
    git("commit", "-m", "init")
    (main / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (main / "frontend" / "node_modules" / "pkg" / "index.js").write_text(
        "hello", encoding="utf-8"
    )

    wt = tmp_path / "wt"
    git("worktree", "add", str(wt), "-b", "feature")
    summary = cm.provision_node_modules(str(main), str(wt))
    assert "frontend" in summary["linked"]

    def boom(_path):
        raise OSError("locked")

    monkeypatch.setattr(cm, "_remove_directory_link", boom)

    assert git_service.remove_worktree(str(main), str(wt), force=True) is False
    assert wt.exists()
    assert (
        main / "frontend" / "node_modules" / "pkg" / "index.js"
    ).read_text(encoding="utf-8") == "hello"


# ─── the guidance the model reads ────────────────────────────────────────────


def test_git_guidance_covers_multi_worktree_merge_and_env_copying():
    from app.core.provider.harness_adapter import GITHUB_BLOCK

    assert "--delete-branch" in GITHUB_BLOCK
    assert "push origin --delete" in GITHUB_BLOCK
    assert "gh pr view" in GITHUB_BLOCK
    assert "Never copy a `.env`" in GITHUB_BLOCK


def test_git_guidance_forbids_removing_a_provisioned_worktree_by_hand():
    from app.core.provider.harness_adapter import GITHUB_BLOCK

    assert "Never run `git worktree remove`" in GITHUB_BLOCK


def test_a_hand_copied_config_already_in_the_worktree_is_flagged(tmp_path, caplog):
    """Not deleted — that is not provisioning's call — but never silent."""
    source = _seed_source(tmp_path / "src")
    dest = tmp_path / "wt"
    dest.mkdir()
    (dest / ".env").write_text("POSTGRES_PASSWORD=hunter2\n", encoding="utf-8")

    summary = cm.provision_worktree_runtime(str(source), str(dest))

    assert any("hand-copied config" in e for e in summary["errors"])
    assert (dest / ".env").exists()
