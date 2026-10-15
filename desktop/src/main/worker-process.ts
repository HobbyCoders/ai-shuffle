/**
 * Recognising, probing and stopping a worker this app started earlier.
 *
 * The installed launcher (installed-entry.ts) needs quiesceRecordedWorker
 * before a database restore, and code it imports is frozen in every installed
 * app. Keeping these helpers out of worker.ts keeps the full worker supervisor
 * out of the launcher, so worker.ts changes still reach installed apps through
 * source updates (tests/test_update_reach.py checks the launcher closure).
 */
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { existsSync, promises as fs } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { assertProcessIdentity, isProcessIdentity, execWithIdentity, signalVerifiedProcess, type ProcessIdentity } from './process-identity';
import { isListenerToken } from './listener';

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
	/** Captured from the spawned process, not from its HTTP responder. Legacy records have no proof. */
	identity?: ProcessIdentity;
}

export async function readWorkerProcessRecord(file: string): Promise<WorkerProcessRecord | null> {
	try {
		const record = JSON.parse(await fs.readFile(file, 'utf8')) as Partial<WorkerProcessRecord>;
		if (record.version !== 1 || typeof record.token !== 'string' || !/^[a-f0-9]{64}$/.test(record.token)) return null;
		const pid = Number.isSafeInteger(record.pid) && (record.pid as number) > 0 ? (record.pid as number) : null;
		const port = Number.isSafeInteger(record.port) ? (record.port as number) : 0;
		return { version: 1, pid, token: record.token, port, ...(isListenerToken(record.listener) ? { listener: record.listener } : {}),
			...(isProcessIdentity(record.identity) && record.identity.pid === pid ? { identity: record.identity } : {}) };
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
export async function terminateProcessTree(pid: number, timeoutMs = 10_000, identity?: ProcessIdentity): Promise<boolean> {
	if (identity) await assertProcessIdentity(identity);
	if (process.platform === 'win32') {
		if (identity) await execWithIdentity(identity, 'taskkill.exe', ['/PID', String(pid), '/T', '/F']);
		else spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
	} else {
		if (identity) await signalVerifiedProcess(identity, 'SIGTERM', true);
		else try { process.kill(-pid, 'SIGTERM'); } catch { try { process.kill(pid, 'SIGTERM'); } catch { /* already gone */ } }
	}
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (!isProcessAlive(pid)) return true;
		await sleep(100);
	}
	if (process.platform !== 'win32') {
		if (identity) await signalVerifiedProcess(identity, 'SIGKILL', true);
		else try { process.kill(-pid, 'SIGKILL'); } catch { try { process.kill(pid, 'SIGKILL'); } catch { /* already gone */ } }
		await sleep(200);
	}
	return !isProcessAlive(pid);
}

/** Before touching a restore point, reap only an authenticated worker from this profile. */
export async function quiesceRecordedWorker(stateDir: string): Promise<void> {
	const file = path.join(stateDir, 'worker-process.json');
	const previous = await readWorkerProcessRecord(file);
	if (!previous) {
		if (existsSync(file)) throw new Error('The worker process record is unreadable; database recovery is blocked');
		return;
	}
	if (!previous.pid) {
		if (!previous.port || !await isPortFree(previous.port)) throw new Error('Cannot prove the previous worker is stopped; database recovery is blocked');
		return;
	}
	if (!isProcessAlive(previous.pid)) return;
	const health = await fetchHealth(previous.port);
	if (!health.ok || health.service !== 'ai-shuffle' || health.attach !== attachTokenHash(previous.token)) throw new Error('Cannot verify the previous worker is stopped; database restore is blocked');
	if (!previous.identity) throw new Error('The previous worker has no verified OS process identity; close it normally before database recovery');
	if (!await terminateProcessTree(previous.pid, 10_000, previous.identity)) throw new Error('The previous worker could not be reaped; database restore is blocked');
}
