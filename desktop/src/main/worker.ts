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
 * only one worker can run per machine. A worker already answering on the
 * default port is adopted only when it proves it is ours: every spawn gets a
 * random attach token (SHUFFLE_DESKTOP_ATTACH_TOKEN) whose SHA-256 the
 * worker reports in /health, and the token plus pid are kept in a private
 * file so the next launch can tell its own orphan from another user's worker
 * or a Docker container. `SHUFFLE_DESKTOP_ATTACH_ANY=1` restores unverified
 * attaching for developers running a manual worker.
 */
import { EventEmitter } from 'node:events';
import { createHash, randomBytes } from 'node:crypto';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { createWriteStream, existsSync, type WriteStream } from 'node:fs';
import { promises as fs } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { isManagedConnection, type PostgresConfig } from './settings';
import { writePrivateFile } from './private-file';

export const DEFAULT_PORT = 8000;
/** Default protected listener; isolated previews may choose another loopback port. */
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
	startupStep?: 'syncing';
	syncStatus?: string;
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
	/** Verified application package selected by the desktop launcher, never an environment override. */
	applicationDir?: string;
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
		const serverDir = opts.applicationDir ?? path.join(bundledRoot, 'server');
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
	/** Explicit isolated source profile; never inferred from inherited environment. */
	isolated?: boolean;
	runtime: WorkerRuntime;
	port: number;
	protectedPort?: number;
	envFile: string;
	dataDir: string;
	workspaceDir?: string;
	/** Only set for an isolated preview; isolates inherited CLI credential/config paths. */
	profileDir?: string;
	baseEnv?: NodeJS.ProcessEnv;
	platform?: NodeJS.Platform;
}

/** The worker gets a private descriptor, never database environment variables. */
export function buildWorkerEnv(opts: EnvOptions): NodeJS.ProcessEnv {
	const platform = opts.platform ?? process.platform;
	const base = { ...(opts.baseEnv ?? process.env) };
	const sep = platform === 'win32' ? ';' : ':';
	const pathKey = platform === 'win32' ? Object.keys(base).find((k) => k.toLowerCase() === 'path') ?? 'Path' : 'PATH';
	const pathParts = [path.dirname(opts.runtime.python)];
	if (opts.runtime.nodeBinDir) pathParts.push(opts.runtime.nodeBinDir);
	if (base[pathKey]) pathParts.push(base[pathKey] as string);
	for (const key of Object.keys(base)) {
		if (/^(POSTGRES_|DATABASE_|PG|PYTHONHOME$|PYTHONPATH$|VIRTUAL_ENV$|SHUFFLE_DESKTOP_ISOLATED$|SHUFFLE_PROTECTED_PORT$|SHUFFLE_DESKTOP_PROTECTED_PORT$|SHUFFLE_DESKTOP_ATTACH_TOKEN$)/i.test(key)) delete base[key];
	}
	let profile: NodeJS.ProcessEnv = {};
	if (opts.profileDir) {
		// Case-insensitive removal matters on Windows: do not leave HOME/home
		// aliases or inherited config overrides that point at the stable profile.
		for (const key of Object.keys(base)) {
			if (/^(HOME|USERPROFILE|APPDATA|LOCALAPPDATA|XDG_.*|CLAUDE_CONFIG_DIR|CLAUDE_PROJECTS_DIR|FLUID_PROJECTS_DIR|FLUID_SKILLS_DIR|CODEX_HOME|GH_CONFIG_DIR|INSTANCE_ID|CLUSTER_SECRET|AI_SHUFFLE_ENV_FILE|DATA_DIR|WORKSPACE_DIR|SHUFFLE_.*DIR|.*(?:API_KEY|AUTH_TOKEN|ACCESS_TOKEN)|ADMIN_PASSWORD|SECRET_KEY)$/i.test(key)) delete base[key];
		}
		profile = {
			HOME: opts.profileDir, USERPROFILE: opts.profileDir, CHROME_CDP_PORT: '19221',
			APPDATA: path.join(opts.profileDir, 'AppData', 'Roaming'),
			LOCALAPPDATA: path.join(opts.profileDir, 'AppData', 'Local'),
			XDG_CONFIG_HOME: path.join(opts.profileDir, '.config'),
			XDG_DATA_HOME: path.join(opts.profileDir, '.local', 'share'),
			XDG_CACHE_HOME: path.join(opts.profileDir, '.cache'),
			CLAUDE_CONFIG_DIR: path.join(opts.profileDir, '.claude'),
			CODEX_HOME: path.join(opts.profileDir, '.codex'),
			GH_CONFIG_DIR: path.join(opts.profileDir, '.config', 'gh')
		};
	}
	return {
		...base, ...profile,
		[pathKey]: pathParts.join(sep),
		LOCAL_MODE: 'true', HOST: '127.0.0.1', PORT: String(opts.port),
		SHUFFLE_DESKTOP_ISOLATED: opts.isolated ? '1' : '0',
		SHUFFLE_PROTECTED_PORT: String(opts.protectedPort ?? PROTECTED_PORT),
		SHUFFLE_DESKTOP_PROTECTED_PORT: String(opts.protectedPort ?? PROTECTED_PORT),
		DATA_DIR: opts.dataDir,
		...(opts.workspaceDir ? { WORKSPACE_DIR: opts.workspaceDir } : {}),
		AI_SHUFFLE_ENV_FILE: opts.envFile,
		TOOLS_DIR: opts.runtime.toolsDir, BROWSER_MCP_ENTRY: opts.runtime.browserMcpEntry,
		PYTHONPATH: opts.runtime.mode === 'bundled' ? opts.runtime.serverDir : path.join(opts.runtime.serverDir, 'fluid'),
		PYTHONNOUSERSITE: '1', PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8', SHUFFLE_DESKTOP: '1'
	};
}

/** Private, atomic handoff to the backend; called before spawning it. */
export async function writeManagedPostgres(dataDir: string, config: PostgresConfig): Promise<void> {
	if (!isManagedConnection(config)) throw new Error('The embedded PostgreSQL endpoint is invalid.');
	const { host, port, user, password, database } = config;
	await writePrivateFile(path.join(dataDir, 'managed-postgres.json'), JSON.stringify({ version: 1, host, port, user, password, database }));
}

/** Retain non-database application settings; remove obsolete database entries. */
export async function prepareWorkerConfig(file: string): Promise<void> {
	let content = '';
	try { content = await fs.readFile(file, 'utf8'); }
	catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
	const clean = content.split(/\r?\n/).filter((line) => !/^\s*(?:export\s+)?(?:DATABASE_|POSTGRES_)[A-Z0-9_]*\s*=/i.test(line)).join('\n');
	await writePrivateFile(file, clean);
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

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
	/** SHA-256 hex of the attach token the worker was started with, when it was started by a desktop app. */
	attach?: string;
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
					const json = JSON.parse(body) as { service?: string; version?: string; attach?: string };
					resolve({ ok: true, service: json.service, version: json.version, attach: typeof json.attach === 'string' ? json.attach : undefined, status: 200 });
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

/** What the worker reports in /health for a given attach token. */
export function attachTokenHash(token: string): string {
	return createHash('sha256').update(token, 'utf8').digest('hex');
}

/** The spawned worker's identity, kept privately so the next launch can recognise it. */
export interface WorkerProcessRecord {
	version: 1;
	pid: number | null;
	token: string;
	port: number;
}

export async function readWorkerProcessRecord(file: string): Promise<WorkerProcessRecord | null> {
	try {
		const record = JSON.parse(await fs.readFile(file, 'utf8')) as Partial<WorkerProcessRecord>;
		if (record.version !== 1 || typeof record.token !== 'string' || !/^[a-f0-9]{64}$/.test(record.token)) return null;
		const pid = Number.isSafeInteger(record.pid) && (record.pid as number) > 0 ? (record.pid as number) : null;
		const port = Number.isSafeInteger(record.port) ? (record.port as number) : 0;
		return { version: 1, pid, token: record.token, port };
	} catch {
		return null;
	}
}

export function isProcessAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code === 'EPERM';
	}
}

/** Stop a worker we started earlier and its children; waits until the pid is gone. */
export async function terminateProcessTree(pid: number, timeoutMs = 10_000): Promise<boolean> {
	if (process.platform === 'win32') {
		spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
	} else {
		try { process.kill(-pid, 'SIGTERM'); } catch { try { process.kill(pid, 'SIGTERM'); } catch { /* already gone */ } }
	}
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (!isProcessAlive(pid)) return true;
		await sleep(100);
	}
	if (process.platform !== 'win32') {
		try { process.kill(-pid, 'SIGKILL'); } catch { try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ } }
		await sleep(200);
	}
	return !isProcessAlive(pid);
}

/** Owner-only launch barrier, also used when attaching to a running local worker. */
export function syncBeforeOpen(port: number): Promise<{ status: string }> {
	return new Promise((resolve, reject) => {
		const req = http.request({
			host: '127.0.0.1', port, path: '/api/v1/network/startup-sync', method: 'POST',
			timeout: 65_000, headers: { accept: 'application/json', 'content-length': '0' }
		}, (res) => {
			let body = '';
			res.setEncoding('utf8');
			res.on('error', reject);
			res.on('data', (chunk) => {
				body += chunk;
				if (body.length > 4096) req.destroy(new Error('Startup sync response too large'));
			});
			res.on('end', () => {
				if (res.statusCode !== 200) return reject(new Error(`Startup sync failed (${res.statusCode}). Update the worker and try again.`));
				try {
					const reply = JSON.parse(body);
					if (!['synced', 'offline', 'partial', 'not_enrolled'].includes(reply.status)) throw new Error('Invalid startup sync response');
					resolve(reply);
				} catch (error) { reject(error); }
			});
		});
		req.on('timeout', () => req.destroy(new Error('Startup sync did not respond in time. Try again.')));
		req.on('error', reject);
		req.end();
	});
}


/** Authenticated, loopback-only control of the child we launched. Never a renderer API. */
export function requestLifecycle(port: number, token: string, action: 'active-sessions' | 'stop-sessions'): Promise<Record<string, unknown>> {
	return new Promise((resolve, reject) => {
		const req = http.request({
			host: '127.0.0.1', port, path: `/api/v1/internal/desktop/${action}`,
			method: action === 'active-sessions' ? 'GET' : 'POST',
			timeout: action === 'active-sessions' ? 3000 : 30_000,
			headers: { authorization: `Bearer ${token}`, accept: 'application/json' }
		}, (res) => {
			let body = '';
			res.setEncoding('utf8');
			res.on('error', reject);
			res.on('data', (chunk) => {
				body += chunk;
				if (body.length > 4096) req.destroy(new Error('Worker response too large'));
			});
			res.on('end', () => {
				if (res.statusCode !== 200) return reject(new Error(`Worker lifecycle request failed (${res.statusCode})`));
				try { resolve(JSON.parse(body)); } catch { reject(new Error('Invalid worker response')); }
			});
		});
		req.on('timeout', () => req.destroy(new Error('Worker did not respond in time')));
		req.on('error', reject);
		req.end();
	});
}

export interface SupervisorOptions {
	isolated?: boolean;
	runtime: WorkerRuntime;
	logDir: string;
	envFile: string;
	dataDir: string;
	workspaceDir?: string;
	profileDir?: string;
	port?: number;
	protectedPort?: number;
	allowAttach?: boolean;
	/** Private directory for the spawned worker's pid and attach token; defaults to the env file's directory. */
	stateDir?: string;
	baseEnv?: NodeJS.ProcessEnv;
	postgres: () => PostgresConfig | null;
	readyTimeoutMs?: number;
}

export const ATTACH_ANY_ENV = 'SHUFFLE_DESKTOP_ATTACH_ANY';

export class WorkerSupervisor extends EventEmitter {
	private child: ChildProcess | null = null;
	private starting: Promise<WorkerState> | null = null;
	private logStream: WriteStream | null = null;
	private _state: WorkerState;
	private stopping = false;
	private readonly lifecycleToken = randomBytes(32).toString('hex');
	private recentLog: string[] = [];
	private crashRestarts: number[] = [];
	/** Set by a post-ready crash so the scheduled start() proceeds although the phase already says 'starting'. */
	private restartPending = false;
	private restartTimer: NodeJS.Timeout | null = null;
	private readonly processFile: string;

	constructor(private readonly opts: SupervisorOptions) {
		super();
		this.processFile = path.join(opts.stateDir ?? path.dirname(opts.envFile), 'worker-process.json');
		this._state = {
			phase: 'idle',
			port: opts.port ?? DEFAULT_PORT,
			url: `http://127.0.0.1:${opts.port ?? DEFAULT_PORT}`,
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

	start(): Promise<WorkerState> {
		if (this.starting) return this.starting;
		this.starting = this.doStart().finally(() => { this.starting = null; });
		return this.starting;
	}

	private async doStart(): Promise<WorkerState> {
		if (this._state.phase === 'ready' || (this._state.phase === 'starting' && !this.restartPending)) return this.state;
		this.restartPending = false;
		this.stopping = false;
		this.set({ phase: 'starting', startupStep: undefined, syncStatus: undefined, error: null, exitCode: null, pid: null, external: false });

		const port = this.opts.port ?? DEFAULT_PORT;
		const protectedPort = this.opts.protectedPort ?? PROTECTED_PORT;
		const previous = await readWorkerProcessRecord(this.processFile);
		const orphanAlive = !!previous?.pid && isProcessAlive(previous.pid);
		// Preview NEVER attaches to an existing worker, even on its own port, and
		// does not probe an unrelated one; it only looks for its own orphan.
		const health: HealthReply = this.opts.allowAttach !== false || orphanAlive ? await fetchHealth(port) : { ok: false };
		if (this.stopping) return this.state;
		if (health.ok && health.service === 'ai-shuffle') {
			const ours = !!previous && health.attach === attachTokenHash(previous.token);
			if (ours && orphanAlive) {
				// Our own orphan from a run that did not shut down: stop it and start
				// fresh on the current database rather than adopting it as external.
				this.log(`stopping worker ${previous!.pid} left behind by a previous run`);
				if (!(await terminateProcessTree(previous!.pid!)) || !(await this.waitForFreePorts([port, protectedPort]))) {
					this.set({ phase: 'failed', error: `A worker from a previous run (pid ${previous!.pid}) is still holding port ${port} and could not be stopped.` });
					return this.state;
				}
				if (this.stopping) return this.state;
			} else if (this.opts.allowAttach !== false && (ours || (this.opts.baseEnv ?? process.env)[ATTACH_ANY_ENV] === '1')) {
				this.set({ external: true, port, url: `http://127.0.0.1:${port}`, startedAt: new Date().toISOString() });
				await this.finishStartup(port);
				return this.state;
			} else if (this.opts.allowAttach !== false) {
				this.set({ phase: 'failed', error: `Port ${port} is in use by another AI Shuffle worker that this app did not start (another user's app, a Docker container or a manual run). Stop it and try again, or set ${ATTACH_ANY_ENV}=1 to attach to it anyway.` });
				return this.state;
			}
		}
		for (const candidate of [port, protectedPort]) {
			if (!(await isPortFree(candidate))) {
				this.set({ phase: 'failed', error: `Port ${candidate} is in use by another program. Free it and restart the worker.` });
				return this.state;
			}
		}
		if (this.stopping) return this.state;
		const postgres = this.opts.postgres();
		if (!postgres) {
			this.set({ phase: 'failed', error: 'The embedded PostgreSQL server is not ready.' });
			return this.state;
		}

		await fs.mkdir(this.opts.logDir, { recursive: true });
		try {
			await writeManagedPostgres(this.opts.dataDir, postgres);
			await prepareWorkerConfig(this.opts.envFile);
			for (const dir of [this.opts.workspaceDir, this.opts.profileDir]) if (dir) await fs.mkdir(dir, { recursive: true, mode: 0o700 });
		} catch {
			this.set({ phase: 'failed', error: 'Could not prepare the private embedded database connection.' });
			return this.state;
		}
		await this.rotateLog();
		if (this.stopping) {
			this.closeLog('--- worker start cancelled by app shutdown ---');
			return this.state;
		}
		const env = buildWorkerEnv({ ...this.opts, port });
		env.SHUFFLE_DESKTOP_LIFECYCLE_TOKEN = this.lifecycleToken;
		const attachToken = randomBytes(32).toString('hex');
		env.SHUFFLE_DESKTOP_ATTACH_TOKEN = attachToken;
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
		this.set({ pid: child.pid ?? null, port, url: `http://127.0.0.1:${port}`, startedAt: new Date().toISOString() });
		try {
			const record: WorkerProcessRecord = { version: 1, pid: child.pid ?? null, token: attachToken, port };
			await writePrivateFile(this.processFile, JSON.stringify(record));
		} catch (error) {
			this.log(`could not record the worker process: ${(error as Error).message}`);
		}

		const deadline = Date.now() + (this.opts.readyTimeoutMs ?? 180_000);
		while (Date.now() < deadline) {
			if (this.child !== child || this.state.phase !== 'starting') return this.state;
			const ready = await fetchHealth(port, '/health/ready', 2000);
			if (ready.ok) {
				await this.finishStartup(port);
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
		void fs.rm(this.processFile, { force: true }).catch(() => {});
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
			this.restartPending = true;
			this.set({ phase: 'starting', pid: null, exitCode: code, error: this.describeExit(code, 'The worker stopped unexpectedly; restarting it.') });
			this.restartTimer = setTimeout(() => {
				this.restartTimer = null;
				if (this.restartPending) void this.start();
			}, 1500);
			return;
		}
		this.set({ phase: 'failed', pid: null, exitCode: code, error: this.describeExit(code, 'The worker keeps stopping unexpectedly.') });
	}

	private describeExit(code: number | null, lead: string): string {
		const last = this.recentLog.filter((l) => l.trim()).slice(-3).join('\n');
		return `${lead}${code !== null ? ` (exit code ${code})` : ''}${last ? `\n${last}` : ''}`;
	}

	/** Only work owned by this app will be stopped; attached workers stay up. */
	async activeSessionCount(): Promise<number> {
		if (this._state.external || this._state.phase !== 'ready') return 0;
		const reply = await this.lifecycleRequest('active-sessions');
		if (!Number.isSafeInteger(reply.active_sessions) || (reply.active_sessions as number) < 0) {
			throw new Error('Invalid active-session response');
		}
		return reply.active_sessions as number;
	}

	private async finishStartup(port: number): Promise<void> {
		this.set({ startupStep: 'syncing' });
		try {
			const result = await syncBeforeOpen(port);
			if (this.stopping || this._state.phase !== 'starting') return;
			this.log(`startup sync: ${result.status}`);
			this.set({ phase: 'ready', startupStep: undefined, syncStatus: result.status });
		} catch (error) {
			if (this.stopping || this._state.phase !== 'starting') return;
			this.set({ phase: 'failed', startupStep: undefined, error: error instanceof Error ? error.message : String(error) });
		}
	}

	private lifecycleRequest(action: 'active-sessions' | 'stop-sessions'): Promise<Record<string, unknown>> {
		return requestLifecycle(this._state.port, this.lifecycleToken, action);
	}

	async stop(): Promise<void> {
		// Windows taskkill does not run Python's lifespan cleanup. Persist Stop
		// BEFORE terminating the process tree, while PostgreSQL is still alive.
		if (this.child && this.child.exitCode === null && this._state.phase === 'ready' && !this._state.external) {
			const reply = await this.lifecycleRequest('stop-sessions');
			if (reply.stopped !== true) throw new Error('Worker did not finish stopping sessions');
		}
		this.stopping = true;
		this.restartPending = false;
		if (this.restartTimer) {
			clearTimeout(this.restartTimer);
			this.restartTimer = null;
		}
		const child = this.child;
		if (!child || child.exitCode !== null) {
			if (this.starting) await this.starting;
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

	/** Called only after the worker and database have stopped on app exit. */
	async clearConnectionFile(): Promise<void> {
		await fs.rm(path.join(this.opts.dataDir, 'managed-postgres.json'), { force: true });
	}

	async restart(): Promise<WorkerState> {
		await this.stop();
		this.set({ phase: 'idle' });
		return this.start();
	}

	private async waitForFreePorts(ports: number[], timeoutMs = 10_000): Promise<boolean> {
		const deadline = Date.now() + timeoutMs;
		while (Date.now() < deadline) {
			let free = true;
			for (const port of ports) if (!(await isPortFree(port))) free = false;
			if (free) return true;
			await sleep(250);
		}
		return false;
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
