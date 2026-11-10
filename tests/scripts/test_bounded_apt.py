"""Real process/record-lock regression for bounded privileged CI installs."""
import importlib.util
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import textwrap
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/ci/bounded_apt.py"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux apt process groups")


def load_helper():
    spec = importlib.util.spec_from_file_location("bounded_apt", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_apt(tmp_path):
    def executable(name, body):
        path = tmp_path / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)
        return path

    executable("apt-get", r'''
        import fcntl, os, pathlib, subprocess, sys, time
        root = pathlib.Path(__file__).parent
        config = pathlib.Path(os.environ["APT_CONFIG"]).read_text()
        assert 'APT::Get::Upgrade "false";' in config
        assert 'Dir::Etc::sourceparts "-";' in config
        cache = pathlib.Path(os.environ["npm_config_cache"])
        assert cache.parent == pathlib.Path(os.environ["APT_CONFIG"]).parent
        cache.mkdir(exist_ok=True)
        (cache / "root-write").touch()
        (root / "npm-cache").write_text(str(cache))
        log = root / "events"
        with log.open("a") as stream:
            stream.write("apt\n")
        if (root / "mode").read_text() == "fail":
            sys.exit(100)
        if log.read_text().count("apt\n") == 1 or (root / "mode").read_text() == "always-timeout":
            holder = subprocess.Popen([sys.executable, str(root / "holder")],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (root / "pid").write_text(str(holder.pid))
            while not (root / "ready").exists():
                time.sleep(0.01)
            time.sleep(60)
        with (root / "lock").open("r+") as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert log.read_text() == "apt\nrepair\napt\n"
    ''')
    executable("holder", r'''
        import fcntl, pathlib, signal, time
        root = pathlib.Path(__file__).parent
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with (root / "lock").open("r+") as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX)
            (root / "ready").touch()
            time.sleep(60)
    ''')
    executable("dpkg", r'''
        import fcntl, pathlib, sys
        assert sys.argv[1:] == ["--configure", "-a"]
        root = pathlib.Path(__file__).parent
        with (root / "lock").open("r+") as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (root / "events").open("a") as log:
            log.write("repair\n")
        sys.exit(1 if (root / "repair-fails").exists() else 0)
    ''')
    # Exercise the unmodified supervisor, shortening only its two constants and
    # redirecting real POSIX lock probes to the fixture's lock. No sudo/root.
    helper = load_helper()
    source = SCRIPT.read_text().replace("ATTEMPT_SECONDS = 300", "ATTEMPT_SECONDS = 1")
    source = source.replace("KILL_AFTER_SECONDS = 10", "KILL_AFTER_SECONDS = 0.2")
    for lock in helper.LOCKS:
        source = source.replace(lock, str(tmp_path / "lock"))
    (tmp_path / "bounded_apt.py").write_text(source)
    (tmp_path / "lock").touch()
    (tmp_path / "mode").write_text("timeout")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]}
    try:
        yield tmp_path, env
    finally:
        pid_file = tmp_path / "pid"
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def run_install(root, env):
    return subprocess.run([sys.executable, str(root / "bounded_apt.py"), "--root", "apt-get"],
                          env=env, text=True, capture_output=True, timeout=10)


def test_timeout_kills_term_resistant_lock_holder_before_repair_and_retry(fake_apt):
    root, env = fake_apt
    result = run_install(root, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "events").read_text() == "apt\nrepair\napt\n"
    assert "attempt 1 failed (exit 124" in result.stdout
    assert not Path((root / "npm-cache").read_text()).exists()


def test_two_failures_fail_closed_without_dpkg_repair(fake_apt):
    root, env = fake_apt
    (root / "mode").write_text("fail")
    result = run_install(root, env)
    assert result.returncode == 1
    assert (root / "events").read_text() == "apt\napt\n"
    assert "failed after two bounded attempts" in result.stdout


def test_both_timeouts_fail_closed_and_release_locks(fake_apt):
    import fcntl

    root, env = fake_apt
    (root / "mode").write_text("always-timeout")
    result = run_install(root, env)
    assert result.returncode == 1
    assert (root / "events").read_text() == "apt\nrepair\napt\n"
    with (root / "lock").open("r+") as lock:
        fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_waits_for_an_external_package_manager_without_killing_it(fake_apt):
    root, env = fake_apt
    (root / "mode").write_text("fail")
    code = (
        "import fcntl, sys, time; "
        "lock = open(sys.argv[1], 'r+'); "
        "fcntl.lockf(lock, fcntl.LOCK_EX); "
        "print('locked', flush=True); time.sleep(0.5)"
    )
    with subprocess.Popen([sys.executable, "-c", code, str(root / "lock")],
                          stdout=subprocess.PIPE, text=True) as holder:
        assert holder.stdout.readline().strip() == "locked"
        start = time.monotonic()
        result = run_install(root, env)
        assert time.monotonic() - start >= 0.4
        assert result.returncode == 1
        assert holder.wait(timeout=2) == 0
    assert (root / "events").read_text() == "apt\napt\n"


def test_failed_repair_prevents_the_retry_install(fake_apt):
    root, env = fake_apt
    (root / "repair-fails").touch()
    result = run_install(root, env)
    assert result.returncode == 1
    assert (root / "events").read_text() == "apt\nrepair\n"


def test_elevation_includes_supervisor_and_preserves_runner_cache(monkeypatch):
    helper = load_helper()
    class ExecCalled(Exception):
        pass

    def execute(program, args):
        assert program == "sudo"
        assert args[:3] == ["sudo", "-n", "env"]
        assert f"HOME={os.environ['HOME']}" in args
        assert f"PATH={os.environ['PATH']}" in args
        assert args[-3:] == ["--root", "apt-get", "update"]
        raise ExecCalled

    monkeypatch.setattr(helper.os, "execvp", execute)
    with pytest.raises(ExecCalled):
        helper.main(["apt-get", "update"])
    assert helper.ATTEMPT_SECONDS == 300
    assert helper.KILL_AFTER_SECONDS == 10


def assert_authenticated_apt_options(options):
    # Reject the option names, not just one CLI spelling or one true value.
    # Leaf names also catch apt's nested scope syntax and case-insensitive keys.
    for forbidden in ("--allow-unauthenticated", "allowunauthenticated",
                      "allowinsecurerepositories", "allowdowngradetoinsecurerepositories",
                      "trusted"):
        assert forbidden not in options.lower(), f"insecure apt option: {forbidden}"


@pytest.mark.parametrize("option", [
    '--allow-unauthenticated',
    '-o APT::Get::AllowUnauthenticated=true',
    'APT::Get::AllowUnauthenticated "true";',
    'Acquire::AllowInsecureRepositories "true";',
    'Acquire::AllowDowngradeToInsecureRepositories "true";',
    'deb [trusted=yes] https://example.invalid stable main',
    '--option=APT::Get::AllowUnauthenticated=1',
    '-o Acquire::AllowInsecureRepositories=yes',
    '-o Acquire::AllowDowngradeToInsecureRepositories=1',
    'APT { Get { AllowUnauthenticated "true"; }; };',
    'apt::get::allowunauthenticated "yes";',
    'deb [Trusted=Yes] https://example.invalid stable main',
])
def test_authentication_guard_rejects_insecure_mutations(option):
    helper = load_helper()
    with pytest.raises(AssertionError):
        assert_authenticated_apt_options(helper.APT_OPTIONS + "\n" + option)


def test_apt_config_reuses_installed_packages_without_upgrades(tmp_path):
    helper = load_helper()
    # A non-root simulation rebuilds an unwritable system package cache from
    # every host index. Keep exercising real apt, but with one installed package
    # and no repository lists, independent of runner image size or cache warmth.
    for directory in ("state/lists", "cache", "etc/apt.conf.d", "etc/preferences.d",
                      "etc/sources.list.d", "etc/-"):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "etc/sources.list.d/ubuntu.sources").touch()
    (tmp_path / "state/status").write_text(textwrap.dedent("""\
        Package: libc6
        Status: install ok installed
        Architecture: all
        Version: 1.0
        Description: installed test package

    """))
    config = tmp_path / "apt.conf"
    config.write_text(helper.APT_OPTIONS + textwrap.dedent(f"""\
        Dir::State "{tmp_path}/state";
        Dir::State::status "{tmp_path}/state/status";
        Dir::Cache "{tmp_path}/cache";
        Dir::Etc "{tmp_path}/etc";
    """))
    # No root, network, host apt configuration, or real package database.
    result = subprocess.run(["apt-get", "--simulate", "install", "libc6"],
                            env={**os.environ, "APT_CONFIG": str(config), "LC_ALL": "C"},
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Skipping libc6, it is already installed and upgrade is not set" in result.stdout
    assert "0 upgraded, 0 newly installed" in result.stdout
    assert_authenticated_apt_options(helper.APT_OPTIONS)
    assert 'Acquire::Languages "none"' in helper.APT_OPTIONS


@pytest.mark.parametrize("workflow,job,step_name", [
    ("ci-frontend.yml", "browser-mcp", "Prepare sandboxed Chromium"),
    ("ci-frontend.yml", "journeys", "Install isolated Chromium"),
    ("ci-fluid.yml", "unit", "Install ripgrep"),
    ("ci-browser-workspace.yml", "workspace", "Install isolated Chromium"),
])
def test_every_apt_install_uses_shared_bounded_supervisor(workflow, job, step_name):
    data = yaml.load((ROOT / ".github/workflows" / workflow).read_text(), Loader=yaml.BaseLoader)
    step = next(step for step in data["jobs"][job]["steps"] if step.get("name") == step_name)
    assert "scripts/ci/bounded_apt.py" in step["run"]
    assert step["timeout-minutes"] == "13"
    assert "continue-on-error" not in step
    if workflow in ("ci-frontend.yml", "ci-browser-workspace.yml"):
        assert ("bounded_apt.py --debs ~/.cache/chromium-debs npx --no-install playwright-core "
                "install-deps chromium") in step["run"]
        steps = data["jobs"][job]["steps"]
        cache = next(step for step in steps if step.get("name") == "Cache Chromium's system packages")
        assert cache["uses"] == "actions/cache@v4"
        assert cache["with"]["path"] == "~/.cache/chromium-debs"
        # Keyed on what the image preinstalls and what Chromium needs.
        name = next(step for step in steps if step.get("id") == "chromium-debs")["run"]
        assert "$ImageOS-$ImageVersion" in name and "playwright-core/package.json" in name
        assert steps.index(cache) < steps.index(step)
        assert "timeout --kill-after=10s 90s npx --no-install playwright-core install chromium" in step["run"]
        assert "sudo env" not in step["run"]
        assert "--with-deps" not in step["run"]
        assert_budgets_fit_the_step(step)


def assert_budgets_fit_the_step(step):
    """A failed cache hit, a hung first attempt and a slow second attempt that then
    succeeds, then the browser download, must all finish inside the step deadline,
    or the workflow kills a setup that was about to succeed."""
    browser = re.search(r"timeout --kill-after=(\d+)s (\d+)s npx --no-install playwright-core "
                        r"install chromium", step["run"])
    helper = load_helper()
    grace = helper.KILL_AFTER_SECONDS
    worst = ((helper.CACHED_SECONDS + grace) + 2 * (helper.ATTEMPT_SECONDS + grace)
             + int(browser[2]) + int(browser[1]))
    # Keep a margin for sudo, interpreter and npx start-up and the cache checks.
    assert worst + 20 <= int(step["timeout-minutes"]) * 60, worst


def test_cached_install_budget_is_short_but_covers_observed_hits():
    # Hosted hits took 4.0-11.5s; a stuck hit must not eat the apt fallback's time.
    helper = load_helper()
    assert 15 <= helper.CACHED_SECONDS <= 30


@pytest.fixture
def deb_cache(tmp_path):
    """Fake apt/dpkg tools for the --debs cache paths; no root, network or real apt."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    def executable(name, body):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
        path.chmod(0o755)

    # The install command (Playwright's apt in CI): downloads into apt's archive
    # directory, which already holds an older download that is not this install's.
    archives = tmp_path / "archives"
    (archives / "partial").mkdir(parents=True)
    (archives / "older_1.0_all.deb").write_text("older")
    executable("install-deps", r'''
        import os, pathlib, sys
        root = pathlib.Path(__file__).parents[1]
        with (root / "events").open("a") as log:
            log.write("apt\n")
        config = pathlib.Path(os.environ["APT_CONFIG"]).read_text()
        assert 'APT::Keep-Downloaded-Packages "true";' in config
        archives = root / "archives"
        if (root / "nothing-new").exists():
            sys.exit(0)
        for name in ("fonts-b", "fonts-a"):
            (archives / f"{name}_1.0_all.deb").write_text(name)
        (root / "installed").write_text("fonts-a\nfonts-b\n")
        sys.exit(int((root / "deps-exit").read_text()) if (root / "deps-exit").exists() else 0)
    ''')
    executable("apt-get", r'''
        import pathlib, sys
        root = pathlib.Path(__file__).parents[1]
        with (root / "events").open("a") as log:
            log.write("cached " + " ".join(pathlib.Path(arg).name for arg in sys.argv[1:]) + "\n")
        assert sys.argv[1:5] == ["install", "-y", "-q", "--no-install-recommends"]
        # No remote source: an empty list file and an empty parts directory.
        sources = sys.argv[5].removeprefix("-oDir::Etc::sourcelist=")
        parts = sys.argv[6].removeprefix("-oDir::Etc::sourceparts=")
        assert pathlib.Path(sources).read_text() == "" and not any(pathlib.Path(parts).iterdir())
        assert all(pathlib.Path(arg).is_absolute() for arg in sys.argv[7:])
        if (root / "cached-fails").exists():
            sys.exit(100)
        (root / "installed").write_text("".join(
            pathlib.Path(arg).read_text() + "\n" for arg in sys.argv[7:]))
    ''')
    executable("dpkg-query", r'''
        import pathlib, sys
        root = pathlib.Path(__file__).parents[1]
        installed = (root / "installed").read_text().split() if (root / "installed").exists() else []
        if sys.argv[-1] in installed:
            print("installed", end="")
        else:
            sys.exit(1)
    ''')
    executable("dpkg-deb", r'''
        import pathlib, sys
        print(pathlib.Path(sys.argv[-1]).read_text())
    ''')
    executable("dpkg", r'''
        import pathlib, sys
        assert sys.argv[1:] == ["--configure", "-a"]
        with (pathlib.Path(__file__).parents[1] / "events").open("a") as log:
            log.write("repair\n")
    ''')
    helper = load_helper()
    source = SCRIPT.read_text()
    for lock in helper.LOCKS:
        source = source.replace(lock, str(tmp_path / "lock"))
    source = source.replace('Path("/var/cache/apt/archives")', f'Path("{archives}")')
    (tmp_path / "bounded_apt.py").write_text(source)
    (tmp_path / "lock").touch()
    env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]}
    env.pop("SUDO_UID", None)
    env.pop("SUDO_GID", None)

    def run(debs):
        return subprocess.run([sys.executable, str(tmp_path / "bounded_apt.py"), "--root",
                               "--debs", str(debs), "install-deps"],
                              env=env, text=True, capture_output=True, timeout=30, check=False)

    return tmp_path, run


def events(root):
    return (root / "events").read_text().splitlines()


def fresh_runner(root):
    """The next job's machine: the restored cache, but nothing installed or downloaded."""
    for path in [root / "events", root / "installed", *(root / "archives").glob("fonts-*.deb")]:
        path.unlink(missing_ok=True)


def test_a_miss_installs_with_apt_and_keeps_the_downloads_for_the_cache(deb_cache):
    root, run = deb_cache
    debs = root / "debs"
    result = run(debs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events(root) == ["apt"]
    # Only this install's downloads and their names, readable by the runner.
    assert sorted(path.name for path in debs.iterdir()) == [
        "fonts-a_1.0_all.deb", "fonts-b_1.0_all.deb", "packages.txt"]
    assert (debs / "packages.txt").read_text() == "fonts-a\nfonts-b\n"
    assert "kept 2 downloaded packages" in result.stdout


def test_a_hit_installs_the_cached_debs_without_running_apt(deb_cache):
    root, run = deb_cache
    debs = root / "debs"
    assert run(debs).returncode == 0
    fresh_runner(root)
    result = run(debs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events(root) == [("cached install -y -q --no-install-recommends "
                             "none.list none fonts-a_1.0_all.deb fonts-b_1.0_all.deb")]
    assert "installed 2 cached packages" in result.stdout


def test_a_failed_cached_install_repairs_dpkg_and_falls_back_to_apt(deb_cache):
    root, run = deb_cache
    debs = root / "debs"
    assert run(debs).returncode == 0
    fresh_runner(root)
    (root / "cached-fails").touch()
    result = run(debs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events(root)[1:] == ["repair", "apt"]
    assert "Cached dependency install failed" in result.stdout
    assert (debs / "packages.txt").read_text() == "fonts-a\nfonts-b\n"


def test_a_hit_that_leaves_a_package_missing_falls_back_to_apt(deb_cache):
    root, run = deb_cache
    debs = root / "debs"
    assert run(debs).returncode == 0
    (debs / "packages.txt").write_text("fonts-a\nfonts-b\nfonts-c\n")
    fresh_runner(root)
    result = run(debs)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events(root)[1:] == ["repair", "apt"]
    assert "left fonts-c missing" in result.stdout


def test_a_miss_that_downloads_nothing_records_no_cache(deb_cache):
    root, run = deb_cache
    (root / "nothing-new").touch()
    result = run(root / "debs")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "debs").exists()
    assert "nothing was downloaded; not caching" in result.stdout


def test_a_failed_miss_fails_closed_and_records_no_cache(deb_cache):
    root, run = deb_cache
    (root / "deps-exit").write_text("100")
    debs = root / "debs"
    result = run(debs)
    assert result.returncode == 1
    assert events(root) == ["apt", "apt"]
    assert not (debs / "packages.txt").exists()


def test_elevation_passes_an_absolute_deb_cache(monkeypatch, tmp_path):
    helper = load_helper()
    monkeypatch.chdir(tmp_path)
    seen = []

    def execute(program, args):
        seen.append(args)
        raise SystemExit(0)

    monkeypatch.setattr(helper.os, "execvp", execute)
    with pytest.raises(SystemExit):
        helper.main(["--debs", "debs", "apt-get", "update"])
    assert seen[0][-5:] == ["--root", "--debs", str(tmp_path / "debs"), "apt-get", "update"]
