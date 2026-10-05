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
 *
 * Each spawn also gets a fresh listener token (SHUFFLE_DESKTOP_LISTENER_TOKEN,
 * see listener.ts). Only this app's window sends it, so the worker can tell
 * the app apart from any other local process that reaches its loopback port.
 */
import { EventEmitter } from 'node:events';
import { createHash, randomBytes } from 'node:crypto';
import { spawn, spawnSync, type ChildProcess } from 'node:child_process';
import { createWriteStream, existsSync, renameSync, type WriteStream } from 'node:fs';
import { promises as fs } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { isManagedConnection, type PostgresConfig } from './settings';
import { writePrivateFile } from './private-file';
import { isListenerToken, LISTENER_HEADER, LISTENER_TOKEN_ENV, newListenerToken } from './listener';

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
	/** Set when the worker's PATH has no `claude`: how to install it. */
	setupNotice?: string | null;
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
		if (/^(POSTGRES_|DATABASE_|PG|PYTHONHOME$|PYTHONPATH$|VIRTUAL_ENV$|SHUFFLE_DESKTOP_ISOLATED$|SHUFFLE_PROTECTED_PORT$|SHUFFLE_DESKTOP_PROTECTED_PORT$|SHUFFLE_DESKTOP_ATTACH_TOKEN$|SHUFFLE_DESKTOP_LISTENER_TOKEN$)/i.test(key)) delete base[key];
	}
	let profile: NodeJS.ProcessEnv = {};
	if (opts.profileDir) {
		// The Claude CLI is the user's own install and runs on the user's own
		// `claude` login, so its config dir stays the host one: an inherited
		// CLAUDE_CONFIG_DIR, else ~/.claude of the real home. Everything else
		// (Fluid settings, app data, other CLIs) stays in the private profile.
		const inheritedKey = (name: string) => Object.keys(base).find((k) => k.toUpperCase() === name);
		const configKey = inheritedKey('CLAUDE_CONFIG_DIR');
		const homeKey = inheritedKey(platform === 'win32' ? 'USERPROFILE' : 'HOME');
		const userClaudeDir = (configKey && base[configKey]) || path.join((homeKey && base[homeKey]) || os.homedir(), '.claude');
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
			CLAUDE_CONFIG_DIR: userClaudeDir,
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

/** How to install the Claude Code CLI with Anthropic's installer on this platform. */
export function claudeCliSetupMessage(platform: NodeJS.Platform = process.platform): string {
	const install = platform === 'win32'
		? 'irm https://claude.ai/install.ps1 | iex   (in PowerShell)'
		: 'curl -fsSL https://claude.ai/install.sh | bash';
	return `Claude Code CLI not found. Only a Claude subscription needs it. To use one, install it with Anthropic's installer, then sign in with \`claude\`:\n  ${install}\nRestart AI Shuffle afterwards.`;
}

/**
 * The `claude` the worker would run: the first match on the worker's PATH, or
 * null. Copies inside a Python package dir (the Agent SDK's vendored binary)
 * do not count; the worker never runs those.
 */
export function findClaudeOnPath(env: NodeJS.ProcessEnv, platform: NodeJS.Platform = process.platform, exists: (file: string) => boolean = existsSync): string | null {
	const pathKey = platform === 'win32' ? Object.keys(env).find((k) => k.toLowerCase() === 'path') ?? 'Path' : 'PATH';
	const names = platform === 'win32' ? ['claude.exe', 'claude.cmd', 'claude.bat'] : ['claude'];
	const join = platform === 'win32' ? path.win32.join : path.posix.join;
	for (const dir of (env[pathKey] ?? '').split(platform === 'win32' ? ';' : ':')) {
		if (!dir || /[\\/](site|dist)-packages([\\/]|$)/i.test(dir)) continue;
		for (const name of names) {
			const candidate = join(dir, name);
			if (exists(candidate)) return candidate;
		}
	}
	return null;
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
	/** Why a probe that got no usable reply failed, when the probe itself gave up. */
	reason?: string;
}

/** Longest a whole health probe may take; the http `timeout` only bounds idle time. */
export const HEALTH_TOTAL_TIMEOUT_MS = 5000;
/** Largest health reply read; the real /health and /health/ready bodies are a few hundred bytes. */
export const HEALTH_MAX_BYTES = 65_536;

export function fetchHealth(port: number, pathname = '/health', timeoutMs = 2500, totalMs = HEALTH_TOTAL_TIMEOUT_MS): Promise<HealthReply> {
	return new Promise((resolve) => {
		let timer: NodeJS.Timeout | null = null;
		const settle = (reply: HealthReply) => {
			if (timer) clearTimeout(timer);
			timer = null;
			resolve(reply);
		};
		const req = http.get({ host: '127.0.0.1', port, path: pathname, timeout: timeoutMs, headers: { accept: 'application/json' } }, (res) => {
			let body = '';
			res.setEncoding('utf8');
			// A reply cut off mid-body (the worker crashed while answering) never
			// ends; without this the probe, and the start or stop awaiting it, hangs.
			res.on('error', () => settle({ ok: false, status: res.statusCode }));
			res.on('data', (chunk) => {
				body += chunk;
				// Whatever answers on the port may stream forever; a health reply is small.
				if (body.length > HEALTH_MAX_BYTES) {
					settle({ ok: false, status: res.statusCode, reason: 'The health reply was too large' });
					req.destroy(new Error('Health response too large'));
				}
			});
			res.on('end', () => {
				if (res.statusCode !== 200) return settle({ ok: false, status: res.statusCode });
				try {
					const json = JSON.parse(body) as { service?: string; version?: string; attach?: string };
					settle({ ok: true, service: json.service, version: json.version, attach: typeof json.attach === 'string' ? json.attach : undefined, status: 200 });
				} catch {
					settle({ ok: false, status: res.statusCode });
				}
			});
		});
		// A responder that drips bytes never goes idle and stays under the size
		// bound; the overall deadline ends the probe regardless.
		timer = setTimeout(() => {
			timer = null;
			settle({ ok: false, reason: `The health check did not finish within ${Math.round(totalMs / 1000)} s` });
			req.destroy(new Error('Health check timed out'));
		}, totalMs);
		req.on('timeout', () => {
			settle({ ok: false });
			req.destroy();
		});
		req.on('error', () => settle({ ok: false }));
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
	/** The listener token the worker was started with; absent in records from older app versions. */
	listener?: string;
}

export async function readWorkerProcessRecord(file: string): Promise<WorkerProcessRecord | null> {
	try {
		const record = JSON.parse(await fs.readFile(file, 'utf8')) as Partial<WorkerProcessRecord>;
		if (record.version !== 1 || typeof record.token !== 'string' || !/^[a-f0-9]{64}$/.test(record.token)) return null;
		const pid = Number.isSafeInteger(record.pid) && (record.pid as number) > 0 ? (record.pid as number) : null;
		const port = Number.isSafeInteger(record.port) ? (record.port as number) : 0;
		return { version: 1, pid, token: record.token, port, ...(isListenerToken(record.listener) ? { listener: record.listener } : {}) };
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
export function syncBeforeOpen(port: number, listener: string | null = null): Promise<{ status: string }> {
	return new Promise((resolve, reject) => {
		const req = http.request({
			host: '127.0.0.1', port, path: '/api/v1/network/startup-sync', method: 'POST',
			timeout: 65_000, headers: { accept: 'application/json', 'content-length': '0', ...listenerHeaders(listener) }
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


function listenerHeaders(listener: string | null): Record<string, string> {
	return isListenerToken(listener) ? { [LISTENER_HEADER]: listener } : {};
}

export const HANDOFF_PATH = '/api/v1/owner/handoff';

/**
 * "Open in Browser": the default browser has no listener token, so the
 * worker hands it a session through a one-time URL instead. Only this app can
 * ask for one (the request carries the listener token); the URL works once,
 * for a minute, from this computer only. Resolves null when the worker has no
 * handoff (started without a listener token), where the plain URL still works.
 */
export function requestBrowserHandoff(port: number, listener: string | null): Promise<string | null> {
	if (!isListenerToken(listener)) return Promise.resolve(null);
	return new Promise((resolve, reject) => {
		const req = http.request({
			host: '127.0.0.1', port, path: HANDOFF_PATH, method: 'POST',
			timeout: 10_000, headers: { accept: 'application/json', 'content-length': '0', ...listenerHeaders(listener) }
		}, (res) => {
			let body = '';
			res.setEncoding('utf8');
			res.on('error', reject);
			res.on('data', (chunk) => {
				body += chunk;
				if (body.length > 4096) req.destroy(new Error('Browser handoff response too large'));
			});
			res.on('end', () => {
				if (res.statusCode === 404) return resolve(null);
				if (res.statusCode !== 200) return reject(new Error(`The worker did not open a browser session (${res.statusCode}).`));
				try {
					const reply = JSON.parse(body);
					if (typeof reply.path !== 'string' || !reply.path.startsWith(HANDOFF_PATH + '?token=')) throw new Error('Invalid browser handoff response');
					resolve(`http://127.0.0.1:${port}${reply.path}`);
				} catch (error) { reject(error); }
			});
		});
		req.on('timeout', () => req.destroy(new Error('The worker did not answer in time.')));
		req.on('error', reject);
		req.end();
	});
}

/** Authenticated, loopback-only control of the child we launched. Never a renderer API. */
export type LifecycleAction = 'active-sessions' | 'stop-sessions' | 'update-policy' | 'update-bridge' | 'source-lease';
const LIFECYCLE_POSTS: ReadonlySet<LifecycleAction> = new Set(['stop-sessions', 'update-bridge']);
export function requestLifecycle(port: number, token: string, action: LifecycleAction, state?: object, listener: string | null = null): Promise<Record<string, unknown>> {
	// A lease envelope is up to 4 KiB on its own (the hub's header limit).
	const limit = action === 'source-lease' ? 16_384 : 4096;
	return new Promise((resolve, reject) => {
		const req = http.request({
			host: '127.0.0.1', port, path: `/api/v1/internal/desktop/${action}`,
			method: LIFECYCLE_POSTS.has(action) ? 'POST' : 'GET',
			timeout: action === 'active-sessions' ? 3000 : 30_000,
			headers: { authorization: `Bearer ${token}`, accept: 'application/json', 'content-type': 'application/json', ...listenerHeaders(listener) }
		}, (res) => {
			let body = '';
			res.setEncoding('utf8');
			res.on('error', reject);
			res.on('data', (chunk) => {
				body += chunk;
				if (body.length > limit) req.destroy(new Error('Worker response too large'));
			});
			res.on('end', () => {
				if (res.statusCode === 409) {
					// A refused update action says why (e.g. still waiting on the last install).
					let detail = '';
					try { detail = String(JSON.parse(body).detail ?? ''); } catch { /* Fall through to the status. */ }
					return reject(new Error(detail || 'Worker refused the request (409)'));
				}
				if (res.statusCode !== 200) return reject(new Error(`Worker lifecycle request failed (${res.statusCode})`));
				try { resolve(JSON.parse(body)); } catch { reject(new Error('Invalid worker response')); }
			});
		});
		req.on('timeout', () => req.destroy(new Error('Worker did not respond in time')));
		req.on('error', reject);
		req.end(state ? JSON.stringify(state) : undefined);
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
	/** Overall limit of one health probe; defaults to HEALTH_TOTAL_TIMEOUT_MS. */
	healthTotalMs?: number;
	/** worker.log size at which it rotates to worker.prev.log during a run; defaults to WORKER_LOG_MAX_BYTES. */
	logMaxBytes?: number;
}

export const ATTACH_ANY_ENV = 'SHUFFLE_DESKTOP_ATTACH_ANY';
/** worker.log rotates to worker.prev.log at each start and whenever it grows past this during a run. */
export const WORKER_LOG_MAX_BYTES = 20 * 1024 ** 2;

export class WorkerSupervisor extends EventEmitter {
	private child: ChildProcess | null = null;
	private starting: Promise<WorkerState> | null = null;
	private logStream: WriteStream | null = null;
	/** Bytes in the current worker.log, as far as this run has written them. */
	private logBytes = 0;
	private _state: WorkerState;
	private stopping = false;
	/** Other workers running subagents for this worker's chats, from the last activity check. */
	private _remoteWorkers: string[] = [];
	private readonly lifecycleToken = randomBytes(32).toString('hex');
	private recentLog: string[] = [];
	private crashRestarts: number[] = [];
	/** Set by a post-ready crash so the scheduled start() proceeds although the phase already says 'starting'. */
	private restartPending = false;
	private restartTimer: NodeJS.Timeout | null = null;
	private readonly processFile: string;
	/** The listener token of the worker this app talks to; null when it does not know one. */
	private listener: string | null = null;

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

	/** For the window's session and IPC only; never part of the state sent to pages. */
	get listenerToken(): string | null {
		return this.listener;
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
		this.starting = this.doStart()
			.catch((error: unknown) => {
				// An unexpected failure before the spawn (for example a log directory
				// that cannot be created) ends this start instead of leaving it 'starting'.
				this.set({ phase: 'failed', startupStep: undefined, error: error instanceof Error ? error.message : String(error) });
				return this.state;
			})
			.finally(() => { this.starting = null; });
		return this.starting;
	}

	private async doStart(): Promise<WorkerState> {
		if (this._state.phase === 'ready' || (this._state.phase === 'starting' && !this.restartPending)) return this.state;
		this.restartPending = false;
		this.stopping = false;
		this.set({ phase: 'starting', startupStep: undefined, syncStatus: undefined, error: null, exitCode: null, pid: null, external: false });
		this.listener = null;

		const port = this.opts.port ?? DEFAULT_PORT;
		const protectedPort = this.opts.protectedPort ?? PROTECTED_PORT;
		const previous = await readWorkerProcessRecord(this.processFile);
		const orphanAlive = !!previous?.pid && isProcessAlive(previous.pid);
		// Preview NEVER attaches to an existing worker, even on its own port, and
		// does not probe an unrelated one; it only looks for its own orphan.
		const health: HealthReply = this.opts.allowAttach !== false || orphanAlive ? await fetchHealth(port, '/health', 2500, this.opts.healthTotalMs) : { ok: false };
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
				// A worker we started keeps the listener token it was started with.
				this.listener = ours ? previous!.listener ?? null : null;
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
				const why = candidate === port && health.reason ? ` ${health.reason}.` : '';
				this.set({ phase: 'failed', error: `Port ${candidate} is in use by another program.${why} Free it and restart the worker.` });
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
			const profile = this.opts.profileDir;
			// Windows known-folder lookup rejects nonexistent redirected AppData.
			// Chrome then treats even an explicit profile as the default and refuses CDP.
			for (const dir of [this.opts.workspaceDir, profile,
				...(profile ? [path.join(profile, 'AppData', 'Roaming'), path.join(profile, 'AppData', 'Local')] : [])]) {
				if (dir) await fs.mkdir(dir, { recursive: true, mode: 0o700 });
			}
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
		const listener = newListenerToken();
		env[LISTENER_TOKEN_ENV] = listener;
		this.listener = listener;
		// The preview job owns descendants even after an intermediate parent exits.
		// Normal installed workers retain their existing lifecycle.
		const entry = process.platform === 'win32' && this.opts.runtime.mode === 'repo'
			&& this.opts.profileDir && env.AI_SHUFFLE_PREVIEW === '1'
			? 'app.preview_process' : 'app.main';
		const child = spawn(this.opts.runtime.python, ['-m', entry], {
			cwd: this.opts.runtime.serverDir,
			env,
			stdio: ['ignore', 'pipe', 'pipe'],
			windowsHide: true,
			detached: process.platform !== 'win32'
		});
		this.child = child;
		this.recentLog = [];
		this.log(`--- worker start ${new Date().toISOString()} (${this.opts.runtime.mode} runtime at ${this.opts.runtime.root}) ---`);
		// Claude sessions run the user's own `claude` from PATH, never a bundled copy.
		const claudeCli = findClaudeOnPath(env);
		const setupNotice = claudeCli ? null : claudeCliSetupMessage();
		this.log(claudeCli ? `Claude Code CLI: ${claudeCli}` : setupNotice!);
		this.set({ setupNotice });
		child.stdout?.on('data', (chunk: Buffer) => this.log(chunk.toString('utf8')));
		child.stderr?.on('data', (chunk: Buffer) => this.log(chunk.toString('utf8')));
		const exited = new Promise<number | null>((resolve) => {
			child.once('exit', (code, signal) => resolve(code ?? (signal ? -1 : null)));
			child.once('error', (error) => {
				this.log(`spawn error: ${error.message}`);
				resolve(-2);
			});
		});
		void exited.then((code) => this.onExit(code, child));
		this.set({ pid: child.pid ?? null, port, url: `http://127.0.0.1:${port}`, startedAt: new Date().toISOString() });
		try {
			const record: WorkerProcessRecord = { version: 1, pid: child.pid ?? null, token: attachToken, port, listener };
			await writePrivateFile(this.processFile, JSON.stringify(record));
		} catch (error) {
			this.log(`could not record the worker process: ${(error as Error).message}`);
		}

		const deadline = Date.now() + (this.opts.readyTimeoutMs ?? 180_000);
		while (Date.now() < deadline) {
			if (this.child !== child || this.state.phase !== 'starting') return this.state;
			const ready = await fetchHealth(port, '/health/ready', 2000, this.opts.healthTotalMs);
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

	private onExit(code: number | null, exited: ChildProcess): void {
		// A child that outlived its stop (it ignored even SIGKILL for a while) and
		// exits after a newer one was spawned must not end the newer one's state,
		// log or process record, nor drop the handle a later stop needs.
		if (this.child !== null && this.child !== exited) return;
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
		// The worker's own last words; not the "--- worker exited" marker logged just before.
		const last = this.recentLog.filter((l) => l.trim() && !l.startsWith('--- worker ')).slice(-3).join('\n');
		return `${lead}${code !== null ? ` (exit code ${code})` : ''}${last ? `\n${last}` : ''}`;
	}

	/** Only work owned by this app will be stopped; attached workers stay up. */
	async exchangeUpdateState(state: object): Promise<Record<string, unknown>> {
		if (this._state.external || this._state.phase !== 'ready') return {};
		return this.lifecycleRequest('update-bridge', state);
	}

	async updatesAllowed(): Promise<boolean> {
		if (this._state.external || this._state.phase !== 'ready') return false;
		const reply = await this.lifecycleRequest('update-policy');
		return reply.allow_updates === true;
	}

	/**
	 * The worker's license lease for the hub's gated source feed, as the
	 * X-Shuffle-Lease header value, or null: not our worker, not ready, no
	 * lease, beta mode, or a worker too old to have the endpoint (feature check).
	 */
	async sourceLease(): Promise<string | null> {
		if (this._state.external || this._state.phase !== 'ready') return null;
		try {
			const reply = await this.lifecycleRequest('source-lease');
			return typeof reply.lease === 'string' && reply.lease ? reply.lease : null;
		} catch {
			return null;
		}
	}

	async activeSessionCount(): Promise<number> {
		this._remoteWorkers = [];
		if (this._state.external || this._state.phase !== 'ready') return 0;
		const reply = await this.lifecycleRequest('active-sessions');
		if (!Number.isSafeInteger(reply.active_sessions) || (reply.active_sessions as number) < 0) {
			throw new Error('Invalid active-session response');
		}
		// Older workers send no list; quitting leaves these running (#1808).
		const names = Array.isArray(reply.remote_workers) ? reply.remote_workers : [];
		this._remoteWorkers = names.filter((name): name is string => typeof name === 'string' && name.trim() !== '').map(name => name.trim().slice(0, 80)).slice(0, 8);
		return reply.active_sessions as number;
	}

	/** Workers that keep running this worker's subagents after it quits, as of the last {@link activeSessionCount}. */
	get remoteWorkers(): string[] { return [...this._remoteWorkers]; }

	private async finishStartup(port: number): Promise<void> {
		this.set({ startupStep: 'syncing' });
		try {
			const result = await syncBeforeOpen(port, this.listener);
			if (this.stopping || this._state.phase !== 'starting') return;
			this.log(`startup sync: ${result.status}`);
			this.set({ phase: 'ready', startupStep: undefined, syncStatus: result.status });
		} catch (error) {
			if (this.stopping || this._state.phase !== 'starting') return;
			this.set({ phase: 'failed', startupStep: undefined, error: error instanceof Error ? error.message : String(error) });
		}
	}

	/** A one-time URL that signs the default browser in, or null when the plain URL works. */
	browserHandoffUrl(): Promise<string | null> {
		if (this._state.phase !== 'ready') return Promise.resolve(null);
		return requestBrowserHandoff(this._state.port, this.listener);
	}

	private lifecycleRequest(action: LifecycleAction, state?: object): Promise<Record<string, unknown>> {
		return requestLifecycle(this._state.port, this.lifecycleToken, action, state, this.listener);
	}

	/**
	 * Stop the worker we launched. `force` skips asking it to stop its sessions
	 * first; only the quit path uses it, after that request already failed or
	 * hung, so a wedged worker cannot keep the app open.
	 */
	async stop({ force = false }: { force?: boolean } = {}): Promise<void> {
		// Windows taskkill does not run Python's lifespan cleanup. Persist Stop
		// BEFORE terminating the process tree, while PostgreSQL is still alive.
		if (!force && this.child && this.child.exitCode === null && this._state.phase === 'ready' && !this._state.external) {
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
		try { this.logBytes = (await fs.stat(current)).size; } catch { this.logBytes = 0; }
		this.openLog(current);
	}

	private openLog(file: string): void {
		const stream = createWriteStream(file, { flags: 'a' });
		// A log file that cannot be opened must not take the app down; the status page keeps the tail.
		stream.on('error', () => { if (this.logStream === stream) this.logStream = null; });
		this.logStream = stream;
	}

	/**
	 * A long run rotates on size too. The old stream is renamed while still
	 * open, so writes it has queued land in worker.prev.log, then ended; new
	 * output goes to a fresh worker.log. A rename that fails (the file is held
	 * open elsewhere) keeps the current file and tries again after another cap.
	 */
	private rotateDuringRun(): void {
		const old = this.logStream;
		// Not open yet: renaming now would let it create a second worker.log.
		if (!old || old.pending) return;
		const current = this._state.logPath;
		this.logBytes = 0;
		try {
			renameSync(current, path.join(this.opts.logDir, 'worker.prev.log'));
		} catch {
			return;
		}
		old.end();
		this.openLog(current);
		this.log(`--- worker.log passed ${this.opts.logMaxBytes ?? WORKER_LOG_MAX_BYTES} bytes; earlier output of this run is in worker.prev.log ---`);
	}

	private log(text: string): void {
		const lines = text.replace(/\r\n/g, '\n').split('\n');
		for (const line of lines) {
			if (!line) continue;
			this.recentLog.push(line);
			this.emit('log', line);
		}
		if (this.recentLog.length > 600) this.recentLog.splice(0, this.recentLog.length - 600);
		if (!this.logStream) return;
		const out = text.endsWith('\n') ? text : `${text}\n`;
		this.logStream.write(out);
		this.logBytes += Buffer.byteLength(out);
		if (this.logBytes > (this.opts.logMaxBytes ?? WORKER_LOG_MAX_BYTES)) this.rotateDuringRun();
	}

	private closeLog(finalLine: string): void {
		this.log(finalLine);
		this.logStream?.end();
		this.logStream = null;
	}
}
