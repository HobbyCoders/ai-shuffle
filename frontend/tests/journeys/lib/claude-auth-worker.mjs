// One fake Claude worker per case. A browser context cannot isolate the
// subscription pin (database), the saved logins, or the Claude CLI home.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';

const checkout = fileURLToPath(new URL('../../../../', import.meta.url));

function deadline(promise, ms, message) {
	let timer;
	return Promise.race([promise, new Promise((_, reject) => {
		timer = setTimeout(() => reject(new Error(message)), ms);
	})]).finally(() => clearTimeout(timer));
}

export async function startClaudeAuthWorker({ deferCleanup, log }) {
	const directory = mkdtempSync(join(tmpdir(), 'claude-auth-case-'));
	const statusPath = join(directory, 'status.json');
	const python = process.env.DEVLOOP_PYTHON || 'python';
	const child = spawn(python, ['-m', 'scripts.devloop', 'up', '--ci', '--claude-cli', 'fake',
		'--no-pause', '--ttl-seconds', '600', '--status', statusPath],
	{ cwd: checkout, stdio: ['ignore', 'pipe', 'ignore'] });
	const exited = new Promise((resolve) => child.once('close', resolve));
	let scratch;
	deferCleanup(async () => {
		try {
			if (!scratch && existsSync(statusPath)) scratch = JSON.parse(readFileSync(statusPath, 'utf8')).scratch_dir;
		} finally {
			child.kill('SIGTERM');
			try {
				await deadline(exited, 60_000, 'Disposable Claude worker did not stop');
			} catch (error) {
				child.kill('SIGKILL'); // The devloop guardian reaps orphaned services.
				throw error;
			}
		}
		if (existsSync(statusPath) || (scratch && existsSync(scratch))) {
			throw new Error('Disposable Claude worker left state on disk');
		}
		rmSync(directory, { recursive: true }); // only our unique status directory
		log(`disposed fake Claude worker ${child.pid ?? '(not started)'} and its auth state`);
	});

	const lines = createInterface({ input: child.stdout });
	try {
		await deadline(new Promise((resolve, reject) => {
			child.once('error', reject);
			child.once('close', (code) => reject(new Error(`Disposable Claude worker exited before ready (code ${code})`)));
			lines.on('line', (line) => {
				if (line.startsWith('DEVLOOP_READY ')) resolve();
				if (line.startsWith('DEVLOOP_FAILED ')) {
					const { error } = JSON.parse(line.slice('DEVLOOP_FAILED '.length));
					reject(new Error(`Disposable Claude worker failed to start: ${error}`));
				}
			});
		}), 420_000, 'Disposable Claude worker did not become ready');
	} finally {
		lines.close();
		child.stdout.resume();
	}
	const worker = JSON.parse(readFileSync(statusPath, 'utf8'));
	scratch = worker.scratch_dir;
	if (worker.pid !== child.pid || worker.claude_cli?.mode !== 'fake' || !worker.app.local_port) {
		throw new Error('Expected the private fake Claude worker started for this case');
	}
	log(`started disposable fake Claude worker ${child.pid}`);
	return worker;
}
