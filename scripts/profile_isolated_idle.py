"""Launch a disposable supervised app and private Chromium; never uses live data.

Build frontend and run python -m app.preview stamp first. Requires PostgreSQL 16,
Chromium and an explicit playwright-core module installed outside the repo.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', required=True, type=Path)
parser.add_argument('--playwright-module', required=True, type=Path)
parser.add_argument('--chromium-executable', required=True, type=Path)
args = parser.parse_args()
def terminate(signum, frame):
    raise SystemExit(128 + signum)
signal.signal(signal.SIGTERM, terminate)
root = Path(__file__).resolve().parents[1]
scratch = args.output_dir.resolve()
scratch.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location('perf_boot', root / 'tests/smoke/test_app_boot.py')
helpers = importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
runtime = helpers._load_container_runtime()
with tempfile.TemporaryDirectory(prefix='shuffle-idle-') as temp:
    directory = Path(temp); data = directory / 'data'; home = directory / 'home'; home.mkdir()
    workspace = directory / 'workspace'; workspace.mkdir()
    empty = directory / 'empty.env'; empty.write_text('')
    db = runtime.Database(SimpleNamespace(effective_data_dir=data), port=helpers._free_port())
    app = sampler = None; proxy = helpers.OutboundRecorder()
    try:
        db.open(); db.publish_descriptor()
        port = helpers._free_port()
        env = {k:v for k,v in os.environ.items() if k in ('PATH','LANG','LD_LIBRARY_PATH')}
        env.update(PYTHONPATH=str(root)+':'+str(root/'fluid'), HOME=str(home),
            XDG_CONFIG_HOME=str(home/'.config'), XDG_DATA_HOME=str(home/'.local/share'),
            XDG_CACHE_HOME=str(home/'.cache'), DATA_DIR=str(data), WORKSPACE_DIR=str(workspace),
            LOCAL_MODE='true', HOST='127.0.0.1', PORT=str(port),
            SHUFFLE_DESKTOP_PROTECTED_PORT=str(helpers._free_port()), AI_SHUFFLE_ENV_FILE=str(empty),
            START_CHROME='0', AI_SHUFFLE_PREVIEW='1', PYTHONUNBUFFERED='1',
            HTTP_PROXY=proxy.url, HTTPS_PROXY=proxy.url, ALL_PROXY=proxy.url,
            NO_PROXY='127.0.0.1,localhost')
        validation = ('from pathlib import Path; from app.core.config import settings; '
                      'from app.core.plugin_models import _get_fluid_dir, _get_claude_dir; '
                      'home = Path.home(); '
                      'assert _get_fluid_dir().is_relative_to(home); '
                      'assert _get_claude_dir().is_relative_to(home); '
                      'assert settings.effective_data_dir == Path(' + repr(str(data)) + ')')
        subprocess.run([sys.executable, '-c', validation], env=env, cwd=root, check=True)
        with (scratch/'isolated-console.log').open('wb') as log:
            app = subprocess.Popen([sys.executable,'-m','app.main'],cwd=root,env=env,
                stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            base = f'http://127.0.0.1:{port}'
            with httpx.Client(trust_env=False) as client:
                deadline = time.monotonic()+180
                while time.monotonic()<deadline:
                    if app.poll() is not None: raise RuntimeError('isolated app exited; inspect private console')
                    try:
                        if client.get(base+'/health/ready',timeout=2).status_code==200: break
                    except httpx.HTTPError: pass
                    time.sleep(.5)
                else: raise RuntimeError('isolated readiness timed out')
                # Complete only the throwaway fixture's local install, no provider credentials.
                response = client.post(base + '/api/v1/workspace/config',
                    json={'workspace_path': str(workspace)}, headers={'Origin': base})
                response.raise_for_status()
                for path in ('/api/v1/auth/setup', '/api/v1/auth/onboarding/complete'):
                    response = client.post(base + path, headers={'Origin': base})
                    response.raise_for_status()
            with (scratch / 'backend-resources.jsonl').open('w') as samples:
                sampler = subprocess.Popen([sys.executable, str(root / 'scripts/perf_resources.py'),
                    '--pid', str(app.pid), '--seconds', '75', '--interval', '5'],
                    cwd=root, env=env, stdout=samples, stderr=subprocess.STDOUT)
                subprocess.run(['node',str(root/'scripts/profile_idle_client.mjs'),base,
                    str(args.playwright_module.resolve()), str(args.chromium_executable.resolve())],
                    check=True,cwd=root,env=env)
                sampler.wait(timeout=20)
                if sampler.returncode:
                    raise RuntimeError('isolated resource sampler failed')
    finally:
        if sampler is not None and sampler.poll() is None:
            sampler.terminate()
            sampler.wait(timeout=5)
        if app is not None and app.poll() is None:
            os.killpg(app.pid,signal.SIGTERM)
            try: app.wait(timeout=40)
            except subprocess.TimeoutExpired:
                os.killpg(app.pid,signal.SIGKILL); app.wait(timeout=5)
        proxy.close(); db.close()
