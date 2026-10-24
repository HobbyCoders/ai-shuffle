/**
 * The bundled PostgreSQL: where its binaries live, how its cluster is
 * created, started, watched and stopped.
 *
 * Packaged builds carry a PostgreSQL 16 distribution under resources/postgres
 * (see scripts/build-worker.mjs). Development runs point
 * `SHUFFLE_DESKTOP_POSTGRES_DIR` at any directory with bin/initdb, bin/pg_ctl
 * and bin/postgres.
 *
 * The cluster lives in `<userData>/postgres/data`. The first start runs
 * initdb with a generated password for the `ai_shuffle` superuser; every
 * start picks a free loopback port and hands it to `pg_ctl start`, then makes
 * sure the `ai_shuffle` database exists. The connection is written back
 * through the settings store with `managed: true`, which is what the worker
 * reads. Everything that touches the filesystem or a process goes through a
 * small `PostgresRuntime` so the decisions can be tested with fakes.
 */
import { EventEmitter } from 'node:events';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { promises as fs } from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import type { PostgresConfig } from './settings';
import { privateDirectory } from './private-file';
import { isPortFree, isProcessAlive } from './worker-process';
import { ColdClusterCopy, durableJson } from './database-restore';
import { readProcessIdentity, sameProcess, isProcessIdentity, execWithIdentity, signalVerifiedProcess, type ProcessIdentity } from './process-identity';

export const POSTGRES_HOST = '127.0.0.1';
export const POSTGRES_USER = 'ai_shuffle';
export const POSTGRES_DATABASE = 'ai_shuffle';
/** The database initdb always creates; the one client tools connect to before ours exists. */
const MAINTENANCE_DATABASE = 'postgres';
/** Unix socket paths are limited to about 100 bytes; beyond that the socket goes to the temp dir. */
const MAX_SOCKET_DIR_LENGTH = 80;

export interface PostgresBinaries {
	/** Directory the binaries were resolved from, for diagnostics. */
	root: string;
	initdb: string;
	pgCtl: string;
	postgres: string;
	createdb: string;
	psql: string;
	pgIsReady: string;
}

export interface ResolvePostgresOptions {
	packaged: boolean;
	resourcesPath: string;
	platform?: NodeJS.Platform;
	env?: NodeJS.ProcessEnv;
	existsSync?: (file: string) => boolean;
}

export function resolvePostgresBinaries(opts: ResolvePostgresOptions): PostgresBinaries | { error: string } {
	const platform = opts.platform ?? process.platform;
	const env = opts.env ?? process.env;
	const exists = opts.existsSync ?? existsSync;
	const root = opts.packaged ? path.join(opts.resourcesPath, 'postgres') : env.SHUFFLE_DESKTOP_POSTGRES_DIR || null;
	if (!root) {
		return {
			error: 'Development run: set SHUFFLE_DESKTOP_POSTGRES_DIR to a PostgreSQL 16 installation (a directory with bin/initdb).'
		};
	}
	const exe = platform === 'win32' ? '.exe' : '';
	const bin = (name: string) => path.join(root, 'bin', `${name}${exe}`);
	for (const name of ['initdb', 'pg_ctl', 'postgres']) {
		if (!exists(bin(name))) {
			return {
				error: opts.packaged
					? `The bundled PostgreSQL is missing (${bin(name)}). Reinstall the app.`
					: `No PostgreSQL ${name} at ${bin(name)} (SHUFFLE_DESKTOP_POSTGRES_DIR).`
			};
		}
	}
	return { root, initdb: bin('initdb'), pgCtl: bin('pg_ctl'), postgres: bin('postgres'), createdb: bin('createdb'), psql: bin('psql'), pgIsReady: bin('pg_isready') };
}

export type PostgresPhase = 'idle' | 'starting' | 'ready' | 'stopped' | 'failed';

export interface PostgresState {
	phase: PostgresPhase;
	port: number | null;
	pid: number | null;
	error: string | null;
	dataDir: string;
	logPath: string;
	startedAt: string | null;
}

// ---- Pure decisions ---------------------------------------------------------

export interface InitdbOptions {
	dataDir: string;
	pwfile: string;
	user?: string;
}

export function initdbArgs(opts: InitdbOptions): string[] {
	return ['-D', opts.dataDir, '-U', opts.user ?? POSTGRES_USER, `--pwfile=${opts.pwfile}`, '--auth=scram-sha-256', '-E', 'UTF8', '--locale=C'];
}

export interface PgCtlStartOptions {
	dataDir: string;
	logPath: string;
	port: number;
	/** Directory for the Unix socket; null on Windows, where there is none. */
	socketDir: string | null;
	timeoutSec?: number;
	/** Defaults to pglz, which every PostgreSQL build accepts. */
	walCompression?: WalCompression;
}

export type WalCompression = 'lz4' | 'pglz';

/**
 * The Windows bundle is built with lz4 (`pg_config --configure` lists
 * --with-lz4). The other desktop bundles are not checked, and an unknown
 * wal_compression value stops the server from starting, so they use pglz.
 */
export function walCompressionFor(platform: NodeJS.Platform): WalCompression {
	return platform === 'win32' ? 'lz4' : 'pglz';
}

/**
 * Checkpoint, WAL and session timeout settings, passed on every start so existing clusters pick
 * them up without editing postgresql.conf. The 1 GB / 5 minute defaults forced
 * a checkpoint every few seconds under load, and each checkpoint makes the next
 * write to every page log a full 8 KB page image; a large trigram GIN index
 * turned that into most of the WAL (#436). Keep in step with
 * docker/container_runtime.py WAL_SETTINGS.
 */
export function walSettingArgs(walCompression: WalCompression): string[] {
	return [
		'max_wal_size=4GB', 'checkpoint_timeout=15min', 'checkpoint_completion_target=0.9', `wal_compression=${walCompression}`,
		// Bound long queries, lock waits and abandoned transactions that hold pooled
		// connections (#580, #591). Maintenance may raise statement_timeout itself.
		'statement_timeout=30s', 'lock_timeout=5s', 'idle_in_transaction_session_timeout=30s',
	].map((s) => `-c ${s}`);
}

/**
 * `pg_ctl start` with the per-start settings passed as postgres options: the
 * port changes between runs, and the server must never listen beyond
 * loopback. `-o` is one argument that pg_ctl re-splits through the shell, so
 * the socket directory (which can contain spaces) is quoted inside it.
 */
export function pgCtlStartArgs(opts: PgCtlStartOptions): string[] {
	const options = [`-p ${opts.port}`, `-c listen_addresses=${POSTGRES_HOST}`, ...walSettingArgs(opts.walCompression ?? 'pglz')];
	if (opts.socketDir !== null) options.push(`-k "${opts.socketDir}"`);
	return ['-D', opts.dataDir, '-l', opts.logPath, '-w', '-t', String(opts.timeoutSec ?? 60), '-o', options.join(' '), 'start'];
}

export function pgCtlStopArgs(dataDir: string, timeoutSec = 30): string[] {
	return ['-D', dataDir, '-m', 'fast', '-w', '-t', String(timeoutSec), 'stop'];
}

export function pgCtlStatusArgs(dataDir: string): string[] {
	return ['-D', dataDir, 'status'];
}

/** Where the Unix socket goes: next to the data directory when the path is short enough, else the temp dir. */
export function chooseSocketDir(platform: NodeJS.Platform, preferred: string, tmpDir: string): string | null {
	if (platform === 'win32') return null;
	return Buffer.byteLength(preferred, 'utf8') <= MAX_SOCKET_DIR_LENGTH ? preferred : tmpDir;
}

export interface PidFileInfo {
	pid: number;
	dataDir: string | null;
	startTime: number | null;
	port: number | null;
}

/** postmaster.pid: pid, data directory, start time, port, socket directory, listen address, shared memory key, status. */
export function parsePidFile(text: string): PidFileInfo | null {
	const lines = text.split(/\r?\n/);
	const pid = Number(lines[0]?.trim());
	if (!Number.isInteger(pid) || pid <= 0) return null;
	const port = Number(lines[3]?.trim());
	const startTime = Number(lines[2]?.trim());
	return { pid, dataDir: lines[1]?.trim() || null, startTime: Number.isSafeInteger(startTime) && startTime > 0 ? startTime : null, port: Number.isInteger(port) && port > 0 ? port : null };
}

export type PidFileAction = 'none' | 'remove' | 'stop' | 'fail';

/**
 * Preliminary plan only. A protocol response is NOT PID ownership. Before a
 * live PID can be signalled, stopVerified also checks the OS identity captured
 * at launch, executable, cluster directory and postmaster start timestamp.
 */
export function planPidFile(info: PidFileInfo | null, pidAlive: boolean, serverAnswers: boolean): PidFileAction {
	if (info === null) return 'remove';
	if (!pidAlive) return 'remove';
	return serverAnswers ? 'stop' : 'fail';
}

/** Whether a start needs initdb, a password reset in single-user mode, or nothing but the stored connection. */
export function planCluster(clusterExists: boolean, stored: PostgresConfig | null): 'initdb' | 'reset-password' | 'reuse' {
	if (!clusterExists) return 'initdb';
	return stored?.password ? 'reuse' : 'reset-password';
}

export function sameConnection(a: PostgresConfig | null, b: PostgresConfig): boolean {
	return !!a && a.host === b.host && a.port === b.port && a.user === b.user && a.database === b.database && a.password === b.password && a.managed === true;
}

// ---- Runtime ----------------------------------------------------------------

export interface ExecOptions {
	identity?: ProcessIdentity;
	env?: NodeJS.ProcessEnv;
	input?: string;
	timeoutMs?: number;
}

export interface ExecResult {
	code: number | null;
	stdout: string;
	stderr: string;
}

/** Every side effect the supervisor performs, replaceable for tests. */
export interface PostgresRuntime {
	platform: NodeJS.Platform;
	env: NodeJS.ProcessEnv;
	tmpDir: string;
	existsSync(file: string): boolean;
	readFile(file: string): Promise<string>;
	writeFile(file: string, text: string, mode: number): Promise<void>;
	mkdir(dir: string): Promise<void>;
	/** Removes a file or a whole directory; missing paths are fine. */
	remove(target: string): Promise<void>;
	exec(file: string, args: string[], opts?: ExecOptions): Promise<ExecResult>;
	pidAlive(pid: number): boolean;
	processIdentity(pid: number): Promise<ProcessIdentity>;
	signal(identity: ProcessIdentity, signal: NodeJS.Signals, group: boolean, log: (message: string) => void): Promise<void>;
	realpath(file: string): Promise<string>;
	saveIdentity(file: string, value: unknown): Promise<void>;
	/** A free loopback port: the preferred one when it is still free, otherwise an ephemeral one. */
	freePort(preferred: number | null): Promise<number>;
	randomPassword(): string;
}

/** Runs a program to completion with its output captured. Never throws; a spawn failure is reported as code -2. */
export function execCapture(file: string, args: string[], opts: ExecOptions = {}): Promise<ExecResult> {
	if (opts.identity) return execWithIdentity(opts.identity, file, args, opts);
	return new Promise((resolve) => {
		let stdout = '';
		let stderr = '';
		let done = false;
		const finish = (code: number | null, extra = '') => {
			if (done) return;
			done = true;
			clearTimeout(timer);
			resolve({ code, stdout, stderr: `${stderr}${extra}` });
		};
		const child = spawn(file, args, { env: opts.env ?? process.env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
		const timer = setTimeout(() => {
			child.kill('SIGKILL');
			finish(-3, `\n${path.basename(file)} did not finish within ${Math.round((opts.timeoutMs ?? 120_000) / 1000)}s`);
		}, opts.timeoutMs ?? 120_000);
		child.stdout?.setEncoding('utf8').on('data', (chunk: string) => (stdout += chunk));
		child.stderr?.setEncoding('utf8').on('data', (chunk: string) => (stderr += chunk));
		child.once('error', (error) => finish(-2, error.message));
		child.once('exit', (code, signal) => finish(code ?? (signal ? -1 : null)));
		if (opts.input !== undefined) child.stdin?.end(opts.input);
		else child.stdin?.end();
	});
}

export async function pickPort(preferred: number | null): Promise<number> {
	if (preferred && (await isPortFree(preferred))) return preferred;
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.once('error', reject);
		server.listen({ port: 0, host: POSTGRES_HOST, exclusive: true }, () => {
			const { port } = server.address() as net.AddressInfo;
			server.close(() => resolve(port));
		});
	});
}

export const nodeRuntime: PostgresRuntime = {
	platform: process.platform,
	env: process.env,
	tmpDir: os.tmpdir(),
	existsSync,
	readFile: (file) => fs.readFile(file, 'utf8'),
	writeFile: (file, text, mode) => fs.writeFile(file, text, { encoding: 'utf8', mode }),
	mkdir: privateDirectory,
	remove: (target) => fs.rm(target, { recursive: true, force: true }),
	exec: execCapture,
	pidAlive: isProcessAlive,
	processIdentity: readProcessIdentity,
	signal: signalVerifiedProcess,
	realpath: file => fs.realpath(file),
	saveIdentity: durableJson,
	freePort: pickPort,
	// Hex keeps the password safe in the managed descriptor and in a connection URL.
	randomPassword: () => randomBytes(20).toString('hex')
};

// ---- Supervisor -------------------------------------------------------------

export interface PostgresCredentials {
	/** The managed connection stored earlier, or null when there is none or it cannot be read. */
	load(): PostgresConfig | null;
	save(config: PostgresConfig): Promise<void>;
}

export interface PostgresSupervisorOptions {
	binaries: PostgresBinaries;
	/** `<userData>/postgres`: holds `data/` and, on Unix, the socket. */
	root: string;
	logDir: string;
	credentials: PostgresCredentials;
	runtime?: Partial<PostgresRuntime>;
	startTimeoutSec?: number;
}

export class PostgresSupervisor extends EventEmitter {
	private _state: PostgresState;
	private recentLog: string[] = [];
	private conn: PostgresConfig | null = null;
	private queue: Promise<unknown> = Promise.resolve();
	private readonly rt: PostgresRuntime;
	/** Set once this supervisor has run `pg_ctl start`; only then is the pid file ours to stop. */
	private launched = false;

	constructor(private readonly opts: PostgresSupervisorOptions) {
		super();
		this.rt = { ...nodeRuntime, ...opts.runtime };
		this._state = {
			phase: 'idle',
			port: null,
			pid: null,
			error: null,
			dataDir: path.join(opts.root, 'data'),
			logPath: path.join(opts.logDir, 'postgres.log'),
			startedAt: null
		};
	}

	get state(): PostgresState {
		return { ...this._state };
	}

	get dataDir(): string {
		return this._state.dataDir;
	}

	/** The connection the worker should use; null until the server is ready. */
	connection(): PostgresConfig | null {
		return this.conn ? { ...this.conn } : null;
	}

	/** The last few hundred supervisor lines (initdb, pg_ctl and createdb output), for the status page. */
	tail(lines = 120): string[] {
		return this.recentLog.slice(-lines);
	}

	private set(patch: Partial<PostgresState>): void {
		this._state = { ...this._state, ...patch };
		this.emit('state', this.state);
	}

	/** Start and stop never overlap: a stop requested during a start waits for it. */
	private serialize<T>(fn: () => Promise<T>): Promise<T> {
		const next = this.queue.then(fn, fn);
		this.queue = next.catch(() => undefined);
		return next;
	}

	start(): Promise<PostgresState> {
		return this.serialize(() => this.doStart());
	}

	stop(): Promise<void> {
		return this.serialize(() => this.doStop());
	}

	/**
	 * Used only while the desktop single-instance lock is held and the worker is reaped.
	 * `before` runs ahead of the stop for a copy or restore, never for cleanup.
	 */
	updateRestorePoint(before?: () => Promise<void>): ColdClusterCopy {
		return new ColdClusterCopy(this.opts.root, async snapshot => { await before?.(); await this.quiesce(snapshot); });
	}

	quiesce(requireClean = false): Promise<void> {
		return this.serialize(async () => {
			await this.doStop();
			await this.clearPidFile();
			const status = await this.exec(this.opts.binaries.pgCtl, pgCtlStatusArgs(this.dataDir));
			// pg_ctl 3 = not running, 4 = no data directory (interrupted restore rename).
			if (![3, 4].includes(status.code ?? -1) || this.rt.existsSync(this.pidFile)) throw new Error('PostgreSQL could not be stopped for the update restore point');
			if (requireClean && this.rt.existsSync(path.join(this.dataDir, 'PG_VERSION'))) {
				const control = path.join(path.dirname(this.opts.binaries.pgCtl), this.rt.platform === 'win32' ? 'pg_controldata.exe' : 'pg_controldata');
				const result = await this.exec(control, [this.dataDir], { env: { LC_ALL: 'C', LANG: 'C' } });
				if (result.code !== 0 || !/^Database cluster state:\s+shut down\s*$/m.test(result.stdout)) throw new Error('PostgreSQL is not cleanly shut down; the update restore point was not changed');
			}
		});
	}

	private async doStart(): Promise<PostgresState> {
		if (this._state.phase === 'ready' && (await this.isRunning())) return this.state;
		this.conn = null;
		this.set({ phase: 'starting', error: null, pid: null, port: null, startedAt: null });
		try {
			await this.rt.mkdir(this.opts.root);
			await this.rt.mkdir(this.opts.logDir);
			const stored = this.opts.credentials.load();
			const plan = planCluster(this.rt.existsSync(path.join(this.dataDir, 'PG_VERSION')), stored);
			let password: string;
			if (plan === 'initdb') {
				password = await this.initCluster();
			} else {
				await this.clearPidFile();
				password = plan === 'reuse' ? (stored as PostgresConfig).password : await this.resetPassword();
			}
			const port = await this.rt.freePort(stored?.port ?? null);
			await this.launch(port);
			this.set({ port, pid: await this.readPid(), startedAt: new Date().toISOString() });
			await this.ensureDatabase(port, password);
			const conn: PostgresConfig = { host: POSTGRES_HOST, port, user: POSTGRES_USER, database: POSTGRES_DATABASE, password, managed: true };
			if (!sameConnection(stored, conn)) await this.opts.credentials.save(conn);
			this.conn = conn;
			this.set({ phase: 'ready' });
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			this.log(message);
			this.set({ phase: 'failed', error: message });
		}
		return this.state;
	}

	private async doStop(): Promise<void> {
		await this.assertPidFileMatchesRecordedProcess();
		if (this._state.phase === 'idle' || this._state.phase === 'stopped') return;
		this.conn = null;
		// A start that failed before launching (for example over a pid file held
		// by an unrelated process) has nothing of ours to stop; pg_ctl would
		// signal whatever pid that file names.
		if (this.launched && this.rt.existsSync(this.pidFile)) {
			const info = parsePidFile(await this.rt.readFile(this.pidFile));
			if (!info) throw new Error('Invalid PostgreSQL process record; no process was signalled');
			if (this.rt.pidAlive(info.pid)) await this.stopVerified(info);
			else await this.rt.remove(this.pidFile);
		}
		this.launched = false;
		this.set({ phase: 'stopped', pid: null });
	}

	private get pidFile(): string {
		return path.join(this.dataDir, 'postmaster.pid');
	}

	private async isRunning(): Promise<boolean> {
		return (await this.exec(this.opts.binaries.pgCtl, pgCtlStatusArgs(this.dataDir), { timeoutMs: 15_000 })).code === 0;
	}

	/** A fresh cluster with a generated password; existing data is never auto-cleaned. */
	private async initCluster(): Promise<string> {
		if (this.rt.existsSync(this.dataDir)) {
			throw new Error(`The existing database directory at ${this.dataDir} has no PG_VERSION. It was preserved; inspect or back it up before retrying.`);
		}
		this.log(`creating the database cluster in ${this.dataDir}`);
		const password = this.rt.randomPassword();
		const pwfile = path.join(this.opts.root, `.initdb-${process.pid}.pw`);
		await this.rt.writeFile(pwfile, `${password}\n`, 0o600);
		try {
			const result = await this.exec(this.opts.binaries.initdb, initdbArgs({ dataDir: this.dataDir, pwfile }), { timeoutMs: 300_000 });
			if (result.code !== 0) throw new Error(`initdb could not create the database cluster (exit code ${result.code}).\n${lastLines(result)}`);
		} finally {
			await this.rt.remove(pwfile);
		}
		return password;
	}

	/**
	 * The cluster exists but its password is unknown: the settings file was
	 * lost or the keyring changed. Single-user mode sets a new password
	 * without deleting or importing data.
	 */
	private async resetPassword(): Promise<string> {
		this.log('no stored password for the bundled server; setting a new one');
		const password = this.rt.randomPassword();
		const sql = `ALTER ROLE "${POSTGRES_USER}" WITH PASSWORD '${password.replace(/'/g, "''")}';\n`;
		const result = await this.exec(this.opts.binaries.postgres, ['--single', '-D', this.dataDir, MAINTENANCE_DATABASE], { input: sql });
		if (result.code !== 0) {
			throw new Error(`Could not set a new password for the bundled server (exit code ${result.code}).\n${lastLines(result)}`);
		}
		return password;
	}

	private get identityFile(): string { return path.join(this.opts.root, 'postmaster-identity.json'); }
	private async captureIdentity(): Promise<void> {
		const info = parsePidFile(await this.rt.readFile(this.pidFile));
		if (!info?.startTime || !info.dataDir || await this.rt.realpath(info.dataDir) !== await this.rt.realpath(this.dataDir)) throw new Error('PostgreSQL did not record the expected cluster identity');
		const identity = await this.rt.processIdentity(info.pid);
		const executable = await this.rt.realpath(this.opts.binaries.postgres);
		if (!isProcessIdentity(identity) || identity.pid !== info.pid || !this.sameExecutable(identity.executable, executable) || Math.abs(identity.startedAt - info.startTime) > 2) throw new Error('PostgreSQL OS process identity does not match its cluster start');
		await this.rt.saveIdentity(this.identityFile, { schemaVersion: 1, identity, dataDir: await this.rt.realpath(this.dataDir), startTime: info.startTime });
	}
	private sameExecutable(a: string, b: string): boolean {
		return this.rt.platform === 'win32' ? a.toLowerCase() === b.toLowerCase() : a === b;
	}
	private async stopVerified(info: PidFileInfo): Promise<void> {
		// A port response can belong to another server. Require the kernel identity
		// captured at launch AND this cluster's exact postmaster.pid provenance.
		if (!this.rt.existsSync(this.identityFile)) throw new Error('PostgreSQL has no recorded OS process identity; close the original server normally before recovery. No process was signalled.');
		const saved = JSON.parse(await this.rt.readFile(this.identityFile));
		if (saved.schemaVersion !== 1 || !isProcessIdentity(saved.identity) || saved.identity.pid !== info.pid || !info.startTime ||
			saved.startTime !== info.startTime || !info.dataDir ||
			saved.dataDir !== await this.rt.realpath(this.dataDir) || saved.dataDir !== await this.rt.realpath(info.dataDir) ||
			!sameProcess(saved.identity, await this.rt.processIdentity(info.pid))) {
			throw new Error('PostgreSQL process identity or cluster mismatch; no process was signalled');
		}
		if (this.rt.platform === 'win32') {
			// Hold the verified Windows process handle throughout pg_ctl.
			const result = await this.exec(this.opts.binaries.pgCtl, pgCtlStopArgs(this.dataDir), { identity: saved.identity });
			if (result.code !== 0) throw new Error(`Verified PostgreSQL did not stop (exit ${result.code})`);
		} else {
			// SIGINT is PostgreSQL fast shutdown (the same mode as pg_ctl -m fast).
			// No intermediate process or second pid-file read between identity check
			// and signal. The remaining POSIX reuse window is documented in the helper.
			await this.rt.signal(saved.identity, 'SIGINT', false, message => this.log(message));
			const deadline = Date.now() + 30_000;
			while (this.rt.existsSync(this.pidFile)) {
				if (Date.now() >= deadline) throw new Error('Verified PostgreSQL did not finish fast shutdown in 30 seconds');
				await new Promise(resolve => setTimeout(resolve, 100));
			}
		}
		if (!this.rt.existsSync(this.pidFile)) await this.rt.remove(this.identityFile);
	}

	private async assertPidFileMatchesRecordedProcess(): Promise<void> {
		if (!this.rt.existsSync(this.identityFile)) return;
		const saved = JSON.parse(await this.rt.readFile(this.identityFile));
		if (!isProcessIdentity(saved.identity)) throw new Error('Invalid PostgreSQL OS identity record; recovery is blocked');
		if (this.rt.pidAlive(saved.identity.pid)) {
			const info = this.rt.existsSync(this.pidFile) ? parsePidFile(await this.rt.readFile(this.pidFile)) : null;
			if (!info || info.pid !== saved.identity.pid) throw new Error('PostgreSQL PID file disagrees with its recorded live process; no process was signalled');
		}
	}
	private async clearPidFile(): Promise<void> {
		await this.assertPidFileMatchesRecordedProcess();
		if (!this.rt.existsSync(this.pidFile)) return;
		const info = parsePidFile(await this.rt.readFile(this.pidFile));
		const alive = info !== null && this.rt.pidAlive(info.pid);
		const answers = alive && info?.port ? await this.serverAnswers(info.port) : false;
		switch (planPidFile(info, alive, answers)) {
			case 'remove':
				this.log(`removing a stale postmaster.pid${info ? ` (process ${info.pid} is gone)` : ''}`);
				await this.rt.remove(this.pidFile);
				return;
			case 'stop': {
				this.log(`stopping a PostgreSQL an earlier run left behind (process ${info?.pid})`);
				await this.stopVerified(info!);
				if (!this.rt.existsSync(this.pidFile)) return;
				if (info && !this.rt.pidAlive(info.pid)) {
					await this.rt.remove(this.pidFile);
					return;
				}
				throw new Error(`A PostgreSQL from an earlier run (process ${info?.pid}) is still running and did not stop. Quit it, then restart the worker.`);
			}
			case 'fail':
				throw new Error(
					`Another process (${info?.pid}) holds ${this.pidFile}. If no PostgreSQL is running from that directory, delete the file and restart the worker.`
				);
			default:
				return;
		}
	}

	/** Whether something on the port speaks the PostgreSQL protocol: pg_isready exits 0 (accepting) or 1 (rejecting, still starting). */
	private async serverAnswers(port: number): Promise<boolean> {
		if (!this.rt.existsSync(this.opts.binaries.pgIsReady)) return false;
		const result = await this.exec(this.opts.binaries.pgIsReady, ['-h', POSTGRES_HOST, '-p', String(port), '-t', '5'], { timeoutMs: 15_000 });
		return result.code === 0 || result.code === 1;
	}

	private async launch(port: number): Promise<void> {
		const socketDir = chooseSocketDir(this.rt.platform, this.opts.root, this.rt.tmpDir);
		const timeoutSec = this.opts.startTimeoutSec ?? 60;
		this.log(`starting PostgreSQL on ${POSTGRES_HOST}:${port}`);
		const args = pgCtlStartArgs({ dataDir: this.dataDir, logPath: this._state.logPath, port, socketDir, timeoutSec, walCompression: walCompressionFor(this.rt.platform) });
		this.launched = true;
		const result = await this.exec(this.opts.binaries.pgCtl, args, { timeoutMs: (timeoutSec + 30) * 1000 });
		if (result.code !== 0) {
			throw new Error(`PostgreSQL did not start (pg_ctl exit code ${result.code}).\n${lastLines(result)}${await this.serverLogTail()}`);
		}
		await this.captureIdentity();
	}

	private async readPid(): Promise<number | null> {
		try {
			return parsePidFile(await this.rt.readFile(this.pidFile))?.pid ?? null;
		} catch {
			return null;
		}
	}

	private async ensureDatabase(port: number, password: string): Promise<void> {
		const env = { PGPASSWORD: password };
		const b = this.opts.binaries;
		const result = this.rt.existsSync(b.createdb)
			? await this.exec(b.createdb, ['-h', POSTGRES_HOST, '-p', String(port), '-U', POSTGRES_USER, POSTGRES_DATABASE], { env })
			: await this.exec(b.psql, ['-h', POSTGRES_HOST, '-p', String(port), '-U', POSTGRES_USER, '-d', MAINTENANCE_DATABASE, '-v', 'ON_ERROR_STOP=1', '-c', `CREATE DATABASE "${POSTGRES_DATABASE}"`], { env });
		if (result.code === 0) {
			this.log(`created the ${POSTGRES_DATABASE} database`);
			return;
		}
		if (/already exists/i.test(result.stderr)) return;
		throw new Error(`Could not create the ${POSTGRES_DATABASE} database (exit code ${result.code}).\n${lastLines(result)}`);
	}

	/** Runs a bundled binary with the parent's PG* variables stripped, so a developer's shell cannot redirect it. */
	private async exec(file: string, args: string[], opts: ExecOptions = {}): Promise<ExecResult> {
		const env: NodeJS.ProcessEnv = {};
		for (const [key, value] of Object.entries(this.rt.env)) {
			if (!/^(PG|DATABASE_|POSTGRES_)/i.test(key)) env[key] = value;
		}
		const result = await this.rt.exec(file, args, { ...opts, env: { ...env, ...opts.env } });
		for (const text of [result.stdout, result.stderr]) {
			if (text.trim()) this.log(text);
		}
		return result;
	}

	private async serverLogTail(): Promise<string> {
		try {
			const lines = (await this.rt.readFile(this._state.logPath)).trim().split('\n');
			return lines.length ? `\n${lines.slice(-8).join('\n')}` : '';
		} catch {
			return '';
		}
	}

	private log(text: string): void {
		for (const line of text.replace(/\r\n/g, '\n').split('\n')) {
			if (!line.trim()) continue;
			this.recentLog.push(line);
			this.emit('log', line);
		}
		if (this.recentLog.length > 400) this.recentLog.splice(0, this.recentLog.length - 400);
	}
}

function lastLines(result: ExecResult, count = 6): string {
	return `${result.stderr}\n${result.stdout}`
		.split('\n')
		.map((l) => l.trimEnd())
		.filter((l) => l.trim())
		.slice(-count)
		.join('\n');
}
