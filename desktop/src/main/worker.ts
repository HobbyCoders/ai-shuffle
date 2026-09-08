/**
 * The local worker: where its runtime lives, how it is started, watched and
 * stopped.
 *
 * Packaged builds carry the runtime under resources/worker (see
 * scripts/build-worker.mjs). Development runs fall back to the repository:
 * its venv, its app/ package with a built app/static, and Node from PATH.
 * `SHUFFLE_DESKTOP_WORKER_DIR` points a development run at an assembled
 * runtime instead.
 *
 * The worker is `python -m app.main` in local mode, listening on 127.0.0.1.
 * The app's own protected listener on port 8001 is fixed by the server, so
 * only one worker can run per machine; if one is already answering on the
 * default port the app attaches to it instead of starting a second.
 */
import { EventEmitter } from 'node:events';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { createWriteStream, existsSync, type WriteStream } from 'node:fs';
import { promises as fs } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import type { PostgresConfig } from './settings';

export const DEFAULT_PORT = 8000;
/** Fixed by app/serve.py; the worker refuses to start without it. */
export const PROTECTED_PORT = 8001;

export interface WorkerRuntime {
	mode: 'bundled' | 'repo';
	/** Directory the runtime was resolved from, for diagnostics. */
	root: string;
	python: string;
	/** Working directory for the server; contains the `app` package. */
	serverDir: string;
	/** Directory prepended to PATH so `node` resolves to the bundled binary; null to use PATH. */
	nodeBinDir: string | null;
	toolsDir: string;
	browserMcpEntry: string;
	manifest: Record<string, unknown> | null;
}

export type WorkerPhase = 'idle' | 'starting' | 'ready' | 'stopping' | 'stopped' | 'failed';

export interface WorkerState {
	phase: WorkerPhase;
	port: number;
	url: string;
	pid: number | null;
	/** True when the app attached to a worker it did not start. */
	external: boolean;
	error: string | null;
	exitCode: number | null;
	startedAt: string | null;
	logPath: string;
}

export interface ResolveOptions {
	packaged: boolean;
	resourcesPath: string;
	repoDir: string;
	platform?: NodeJS.Platform;
	env?: NodeJS.ProcessEnv;
}

export function resolveRuntime(opts: ResolveOptions): WorkerRuntime | { error: string } {
	const platform = opts.platform ?? process.platform;
	const env = opts.env ?? process.env;
	const bundledRoot = opts.packaged ? path.join(opts.resourcesPath, 'worker') : env.SHUFFLE_DESKTOP_WORKER_DIR ?? null;
	if (bundledRoot) {
		const manifestPath = path.join(bundledRoot, 'manifest.json');
		let manifest: Record<string, unknown> | null = null;
		try {
			manifest = JSON.parse(require('node:fs').readFileSync(manifestPath, 'utf8')) as Record<string, unknown>;
		} catch {
			return { error: `The worker runtime is missing (${manifestPath}). Reinstall the app.` };
		}
		if (manifest.stub) return { error: 'This build of the app was packaged without a worker runtime.' };
		const python = platform === 'win32' ? path.join(bundledRoot, 'python', 'python.exe') : path.join(bundledRoot, 'python', 'bin', 'python3');
		const nodeBinDir = platform === 'win32' ? path.join(bundledRoot, 'node') : path.join(bundledRoot, 'node', 'bin');
		const serverDir = path.join(bundledRoot, 'server');
		if (!existsSync(python)) return { error: `The bundled Python is missing (${python}). Reinstall the app.` };
		if (!existsSync(path.join(serverDir, 'app', 'main.py'))) return { error: `The bundled server is missing (${serverDir}). Reinstall the app.` };
		return {
			mode: 'bundled',
			root: bundledRoot,
			python,
			serverDir,
			nodeBinDir,
			toolsDir: path.join(serverDir, 'tools'),
			browserMcpEntry: path.join(serverDir, 'browser-mcp', 'dist', 'index.js'),
			manifest
		};
	}
	// Development runs from a worktree can borrow another checkout's venv and build.
	const repo = env.SHUFFLE_DESKTOP_REPO_DIR || opts.repoDir;
	const python = platform === 'win32' ? path.join(repo, 'venv', 'Scripts', 'python.exe') : path.join(repo, 'venv', 'bin', 'python');
	if (!existsSync(python)) {
		return { error: `Development run: no venv at ${python}. Run \`python run_local.py --setup\` in the repository first, or set SHUFFLE_DESKTOP_WORKER_DIR.` };
	}
	if (!existsSync(path.join(repo, 'app', 'static', 'index.html'))) {
		return { error: `Development run: app/static has no frontend build. Run \`python run_local.py --setup\` in the repository first.` };
	}
	return {
		mode: 'repo',
		root: repo,
		python,
		serverDir: repo,
		nodeBinDir: null,
		toolsDir: path.join(repo, 'tools'),
		browserMcpEntry: path.join(repo, 'browser-mcp', 'dist', 'index.js'),
		manifest: null
	};
}

export interface EnvOptions {
	runtime: WorkerRuntime;
	postgres: PostgresConfig;
	port: number;
	envFile: string;
	baseEnv?: NodeJS.ProcessEnv;
	platform?: NodeJS.Platform;
}

/** The environment the worker process starts with. Pure, for tests. */
export function buildWorkerEnv(opts: EnvOptions): NodeJS.ProcessEnv {
	const platform = opts.platform ?? process.platform;
	const base = { ...(opts.baseEnv ?? process.env) };
	const sep = platform === 'win32' ? ';' : ':';
	const pathKey = platform === 'win32' ? Object.keys(base).find((k) => k.toLowerCase() === 'path') ?? 'Path' : 'PATH';
	const pathParts = [path.dirname(opts.runtime.python)];
	if (opts.runtime.nodeBinDir) pathParts.push(opts.runtime.nodeBinDir);
	if (base[pathKey]) pathParts.push(base[pathKey] as string);
	// Anything a parent shell set must not leak into the worker's database choice.
	for (const key of Object.keys(base)) {
		if (/^(POSTGRES_|DATABASE_URL$|PYTHONHOME$|PYTHONPATH$|VIRTUAL_ENV$)/.test(key)) delete base[key];
	}
	return {
		...base,
		[pathKey]: pathParts.join(sep),
		LOCAL_MODE: 'true',
		HOST: '127.0.0.1',
		PORT: String(opts.port),
		POSTGRES_HOST: opts.postgres.host,
		POSTGRES_PORT: String(opts.postgres.port),
		POSTGRES_USER: opts.postgres.user,
		POSTGRES_PASSWORD: opts.postgres.password,
		POSTGRES_DB: opts.postgres.database,
		AI_SHUFFLE_ENV_FILE: opts.envFile,
		TOOLS_DIR: opts.runtime.toolsDir,
		BROWSER_MCP_ENTRY: opts.runtime.browserMcpEntry,
		PYTHONNOUSERSITE: '1',
		PYTHONUNBUFFERED: '1',
		PYTHONIOENCODING: 'utf-8',
		SHUFFLE_DESKTOP: '1'
	};
}

/** Python that answers whether the configured database can be reached, and can create it. */
const PG_CHECK_SCRIPT = `
import json, sys
cfg = json.load(sys.stdin)
try:
    import psycopg2
except Exception as e:
    print(json.dumps({"ok": False, "code": "runtime", "error": "psycopg2 is missing from the worker runtime: %s" % e})); sys.exit(0)

def connect(db):
    return psycopg2.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"], dbname=db, connect_timeout=8)

def classify(msg):
    low = msg.lower()
    if "does not exist" in low and "database" in low: return "no-database"
    if "password authentication failed" in low or "authentication failed" in low or "no password supplied" in low: return "auth"
    if "role" in low and "does not exist" in low: return "no-role"
    if "could not connect" in low or "connection refused" in low or "timeout" in low or "could not translate" in low or "no route" in low or "network is unreachable" in low: return "unreachable"
    if "no pg_hba.conf entry" in low: return "hba"
    return "unknown"

action = cfg.get("action", "test")
try:
    if action == "create":
        conn = connect("postgres")
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg["database"],))
        if cur.fetchone() is None:
            cur.execute('CREATE DATABASE "%s"' % cfg["database"].replace('"', '""'))
        conn.close()
    conn = connect(cfg["database"])
    cur = conn.cursor()
    cur.execute("SELECT version(), current_user")
    version, user = cur.fetchone()
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
    tables = cur.fetchone()[0]
    conn.close()
    print(json.dumps({"ok": True, "version": version.split(",")[0], "user": user, "tables": tables}))
except psycopg2.Error as e:
    msg = (str(e) or e.__class__.__name__).strip()
    print(json.dumps({"ok": False, "code": classify(msg), "error": msg}))
except Exception as e:
    print(json.dumps({"ok": False, "code": "unknown", "error": str(e)}))
`;

export interface PgCheckResult {
	ok: boolean;
	code?: 'no-database' | 'auth' | 'no-role' | 'unreachable' | 'hba' | 'runtime' | 'unknown';
	error?: string;
	version?: string;
	user?: string;
	tables?: number;
}

export function checkPostgres(runtime: WorkerRuntime, config: PostgresConfig, action: 'test' | 'create' = 'test'): PgCheckResult {
	const result = spawnSync(runtime.python, ['-I', '-c', PG_CHECK_SCRIPT], {
		input: JSON.stringify({ ...config, action }),
		encoding: 'utf8',
		timeout: 20_000,
		env: { ...process.env, PYTHONNOUSERSITE: '1', PYTHONIOENCODING: 'utf-8' },
		windowsHide: true
	});
	if (result.error) return { ok: false, code: 'runtime', error: `Could not run the worker's Python: ${result.error.message}` };
	const line = (result.stdout || '').trim().split('\n').pop() ?? '';
	try {
		return JSON.parse(line) as PgCheckResult;
	} catch {
		return { ok: false, code: 'runtime', error: (result.stderr || result.stdout || 'The database check produced no result.').trim().slice(-600) };
	}
}

export function isPortFree(port: number, host = '127.0.0.1'): Promise<boolean> {
	return new Promise((resolve) => {
		const server = net.createServer();
		server.once('error', () => resolve(false));
		server.listen({ port, host, exclusive: true }, () => {
			server.close(() => resolve(true));
		});
	});
}

export interface HealthReply {
	ok: boolean;
	service?: string;
	version?: string;
	status?: number;
}

export function fetchHealth(port: number, pathname = '/health', timeoutMs = 2500): Promise<HealthReply> {
	return new Promise((resolve) => {
		const req = http.get({ host: '127.0.0.1', port, path: pathname, timeout: timeoutMs, headers: { accept: 'application/json' } }, (res) => {
			let body = '';
			res.setEncoding('utf8');
			res.on('data', (chunk) => (body += chunk));
			res.on('end', () => {
				if (res.statusCode !== 200) return resolve({ ok: false, status: res.statusCode });
				try {
					const json = JSON.parse(body) as { service?: string; version?: string };
					resolve({ ok: true, service: json.service, version: json.version, status: 200 });
				} catch {
					resolve({ ok: false, status: res.statusCode });
				}
			});
		});
		req.on('timeout', () => {
			req.destroy();
			resolve({ ok: false });
		});
		req.on('error', () => resolve({ ok: false }));
	});
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export interface SupervisorOptions {
	runtime: WorkerRuntime;
	logDir: string;
	envFile: string;
	postgres: () => PostgresConfig | null;
	readyTimeoutMs?: number;
}

export class WorkerSupervisor extends EventEmitter {
	private child: ChildProcess | null = null;
	private logStream: WriteStream | null = null;
	private _state: WorkerState;
	private stopping = false;
	private recentLog: string[] = [];
	private crashRestarts: number[] = [];

	constructor(private readonly opts: SupervisorOptions) {
		super();
		this._state = {
			phase: 'idle',
			port: DEFAULT_PORT,
			url: `http://127.0.0.1:${DEFAULT_PORT}`,
			pid: null,
			external: false,
			error: null,
			exitCode: null,
			startedAt: null,
			logPath: path.join(opts.logDir, 'worker.log')
		};
	}

	get state(): WorkerState {
		return { ...this._state };
	}

	get runtime(): WorkerRuntime {
		return this.opts.runtime;
	}

	/** The last few hundred log lines, for the status page. */
	tail(lines = 120): string[] {
		return this.recentLog.slice(-lines);
	}

	private set(patch: Partial<WorkerState>): void {
		this._state = { ...this._state, ...patch };
		this.emit('state', this.state);
	}

	async start(): Promise<WorkerState> {
		if (this._state.phase === 'starting' || this._state.phase === 'ready') return this.state;
		this.stopping = false;
		this.set({ phase: 'starting', error: null, exitCode: null, pid: null, external: false });

		// A worker already answering on the default port: attach rather than fight over 8001.
		const health = await fetchHealth(DEFAULT_PORT);
		if (health.ok && health.service === 'ai-shuffle') {
			this.set({ phase: 'ready', external: true, port: DEFAULT_PORT, url: `http://127.0.0.1:${DEFAULT_PORT}`, startedAt: new Date().toISOString() });
			return this.state;
		}
		for (const port of [DEFAULT_PORT, PROTECTED_PORT]) {
			if (!(await isPortFree(port))) {
				this.set({ phase: 'failed', error: `Port ${port} is in use by another program. Free it and restart the worker.` });
				return this.state;
			}
		}
		const postgres = this.opts.postgres();
		if (!postgres) {
			this.set({ phase: 'failed', error: 'The database connection is not set up.' });
			return this.state;
		}

		await fs.mkdir(this.opts.logDir, { recursive: true });
		await this.writeEnvFile(postgres);
		await this.rotateLog();
		const env = buildWorkerEnv({ runtime: this.opts.runtime, postgres, port: DEFAULT_PORT, envFile: this.opts.envFile });
		const child = spawn(this.opts.runtime.python, ['-m', 'app.main'], {
			cwd: this.opts.runtime.serverDir,
			env,
			stdio: ['ignore', 'pipe', 'pipe'],
			windowsHide: true,
			detached: process.platform !== 'win32'
		});
		this.child = child;
		this.recentLog = [];
		this.log(`--- worker start ${new Date().toISOString()} (${this.opts.runtime.mode} runtime at ${this.opts.runtime.root}) ---`);
		child.stdout?.on('data', (chunk: Buffer) => this.log(chunk.toString('utf8')));
		child.stderr?.on('data', (chunk: Buffer) => this.log(chunk.toString('utf8')));
		const exited = new Promise<number | null>((resolve) => {
			child.once('exit', (code, signal) => resolve(code ?? (signal ? -1 : null)));
			child.once('error', (error) => {
				this.log(`spawn error: ${error.message}`);
				resolve(-2);
			});
		});
		void exited.then((code) => this.onExit(code));
		this.set({ pid: child.pid ?? null, port: DEFAULT_PORT, url: `http://127.0.0.1:${DEFAULT_PORT}`, startedAt: new Date().toISOString() });

		const deadline = Date.now() + (this.opts.readyTimeoutMs ?? 180_000);
		while (Date.now() < deadline) {
			if (this.child !== child || this.state.phase !== 'starting') return this.state;
			const ready = await fetchHealth(DEFAULT_PORT, '/health/ready', 2000);
			if (ready.ok) {
				this.set({ phase: 'ready' });
				return this.state;
			}
			await sleep(700);
		}
		if (this.child === child && this.state.phase === 'starting') {
			this.log('worker did not become ready in time; stopping it');
			await this.stop();
			this.set({ phase: 'failed', error: 'The worker did not become ready in time. See the log for details.' });
		}
		return this.state;
	}

	private onExit(code: number | null): void {
		const child = this.child;
		this.child = null;
		this.closeLog(`--- worker ${this.stopping ? 'stopped by the app' : `exited (${code ?? 'signal'})`} ${new Date().toISOString()} ---`);
		if (this.stopping) {
			this.set({ phase: 'stopped', pid: null, exitCode: code });
			return;
		}
		if (this._state.phase === 'starting') {
			this.set({ phase: 'failed', pid: null, exitCode: code, error: this.describeExit(code, 'The worker stopped while starting.') });
			return;
		}
		// Crash after it was ready: restart a couple of times, then give up loudly.
		const now = Date.now();
		this.crashRestarts = this.crashRestarts.filter((t) => now - t < 5 * 60_000);
		if (child && this.crashRestarts.length < 2) {
			this.crashRestarts.push(now);
			this.set({ phase: 'starting', pid: null, exitCode: code, error: this.describeExit(code, 'The worker stopped unexpectedly; restarting it.') });
			setTimeout(() => void this.start(), 1500);
			return;
		}
		this.set({ phase: 'failed', pid: null, exitCode: code, error: this.describeExit(code, 'The worker keeps stopping unexpectedly.') });
	}

	private describeExit(code: number | null, lead: string): string {
		const last = this.recentLog.filter((l) => l.trim()).slice(-3).join('\n');
		return `${lead}${code !== null ? ` (exit code ${code})` : ''}${last ? `\n${last}` : ''}`;
	}

	async stop(): Promise<void> {
		const child = this.child;
		if (!child || child.exitCode !== null) {
			if (this._state.phase !== 'idle') this.set({ phase: 'stopped', pid: null });
			return;
		}
		this.stopping = true;
		this.set({ phase: 'stopping' });
		const exited = new Promise<void>((resolve) => child.once('exit', () => resolve()));
		if (process.platform === 'win32') {
			spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
		} else {
			try {
				process.kill(-(child.pid as number), 'SIGTERM');
			} catch {
				child.kill('SIGTERM');
			}
		}
		const timeout = sleep(10_000).then(() => 'timeout' as const);
		if ((await Promise.race([exited.then(() => 'exited' as const), timeout])) === 'timeout') {
			try {
				if (process.platform !== 'win32') process.kill(-(child.pid as number), 'SIGKILL');
				else child.kill('SIGKILL');
			} catch {
				/* already gone */
			}
			await Promise.race([exited, sleep(3000)]);
		}
	}

	async restart(): Promise<WorkerState> {
		await this.stop();
		this.set({ phase: 'idle' });
		return this.start();
	}

	private async writeEnvFile(postgres: PostgresConfig): Promise<void> {
		// The server also reads .env for anything the settings UI persists there;
		// keep a copy of the connection so command-line tools run against the
		// same database. Written with owner-only permissions.
		const lines = [
			'# Written by AI Shuffle Desktop. Edit the connection from the app, not here.',
			'LOCAL_MODE=true',
			`POSTGRES_HOST=${postgres.host}`,
			`POSTGRES_PORT=${postgres.port}`,
			`POSTGRES_USER=${postgres.user}`,
			`POSTGRES_PASSWORD=${postgres.password}`,
			`POSTGRES_DB=${postgres.database}`,
			''
		];
		await fs.mkdir(path.dirname(this.opts.envFile), { recursive: true });
		await fs.writeFile(this.opts.envFile, lines.join('\n'), { encoding: 'utf8', mode: 0o600 });
	}

	private async rotateLog(): Promise<void> {
		const current = this._state.logPath;
		const previous = path.join(this.opts.logDir, 'worker.prev.log');
		try {
			await fs.rename(current, previous);
		} catch {
			/* first run */
		}
		this.logStream = createWriteStream(current, { flags: 'a' });
	}

	private log(text: string): void {
		const lines = text.replace(/\r\n/g, '\n').split('\n');
		for (const line of lines) {
			if (!line) continue;
			this.recentLog.push(line);
			this.emit('log', line);
		}
		if (this.recentLog.length > 600) this.recentLog.splice(0, this.recentLog.length - 600);
		this.logStream?.write(text.endsWith('\n') ? text : `${text}\n`);
	}

	private closeLog(finalLine: string): void {
		this.log(finalLine);
		this.logStream?.end();
		this.logStream = null;
	}
}
