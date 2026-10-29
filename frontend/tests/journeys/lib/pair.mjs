// Shared steps for two-worker journeys (`pair: true`, against
// `python -m scripts.devloop up --pair`): worker A's UI is the page under
// test; worker B is a second real worker in the same network. Ownership is
// asserted on BOTH workers' own APIs, never only through the UI.
//
// Guide: tests/journeys/README.md ("Two workers").
import { execFile } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';

const run = promisify(execFile);
const CHECKOUT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');

/** Seeded chats of the pair: A's are the default seed's, B's are `pair-b`'s. */
export const PAIR_SEEDED = {
	bLoose: 'Worker B chat (seeded)',
	bProject: 'Worker B project chat (seeded)'
};
export const B_PROJECT = 'Worker B project';

/**
 * Anomalies a two-worker journey tolerates because the pair itself causes
 * them (beside DEV_INSTANCE_NOISE). Each has a reason a reviewer can check.
 */
export const PAIR_NOISE = [];

/** The seeded id of `key` on worker `member` (status.seed.chats). */
export function pairChatId(member, key) {
	const id = member.status.seed?.chats?.[key];
	if (!id) throw new Error(`worker ${member.role.toUpperCase()} has no seeded chat "${key}"`);
	return id;
}

const helperPages = new WeakMap();

/**
 * A background page on `member`'s own origin (its access cookie rides the
 * context), for reading its API: the preview cookie is same-origin only.
 */
async function originPage(context, member) {
	let pages = helperPages.get(context);
	if (!pages) helperPages.set(context, (pages = new Map()));
	let page = pages.get(member.role);
	if (!page || page.isClosed()) {
		page = await context.newPage();
		await page.goto(new URL('/api/v1/auth/status', member.status.urls.ui).toString());
		pages.set(member.role, page);
	}
	return page;
}

/** Call worker `member`'s API (GET for assertions; unsafe methods only to stage). */
export async function memberApi(context, member, method, path, body) {
	const page = await originPage(context, member);
	const result = await page.evaluate(
		async ({ method, path, body }) => {
			const response = await fetch(`/api/v1${path}`, {
				method,
				credentials: 'include',
				headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
				body: body === undefined ? undefined : JSON.stringify(body)
			});
			return { status: response.status, text: await response.text() };
		},
		{ method, path, body }
	);
	if (result.status >= 400) {
		const error = new Error(`worker ${member.role.toUpperCase()}: ${method} ${path} → HTTP ${result.status}: ${result.text.slice(0, 300)}`);
		error.status = result.status;
		throw error;
	}
	return result.text ? JSON.parse(result.text) : null;
}

/** Close the helper pages of a context (before the runner closes it). */
export async function closeHelperPages(context) {
	const pages = helperPages.get(context);
	if (!pages) return;
	for (const page of pages.values()) await page.close().catch(() => {});
	helperPages.delete(context);
}

/** One chat as each worker sees it: `{ a, b }` session rows. */
export async function chatOnBoth({ context, pair }, chatId) {
	const [a, b] = await Promise.all([
		memberApi(context, pair.a, 'GET', `/sessions/${chatId}`),
		memberApi(context, pair.b, 'GET', `/sessions/${chatId}`)
	]);
	return { a, b };
}

/** Which device each worker says owns `row`. */
function ownerOf(row) {
	return row?.owner_device_id || null;
}

/**
 * Both workers agree `chatId` is owned by worker `role`: its owner_device_id
 * on both, `is_remote` false on the owner and true on the other.
 */
export async function expectOwner(ctx, chatId, role, { timeout = 60_000 } = {}) {
	const { pair } = ctx;
	const owner = pair[role];
	const other = pair[role === 'a' ? 'b' : 'a'];
	const deadline = Date.now() + timeout;
	let last = null;
	for (;;) {
		const rows = await chatOnBoth(ctx, chatId).catch((error) => ({ error: error.message }));
		if (!rows.error) {
			const seen = { a: ownerOf(rows.a), b: ownerOf(rows.b) };
			const remote = { [owner.role]: rows[owner.role].is_remote, [other.role]: rows[other.role].is_remote };
			last = { owner: seen, is_remote: remote };
			if (seen.a === owner.deviceId && seen.b === owner.deviceId && remote[owner.role] === false && remote[other.role] === true) return rows;
		} else {
			last = rows;
		}
		if (Date.now() >= deadline) {
			throw new Error(
				`expectation failed: chat ${chatId.slice(0, 8)} never owned by worker ${role.toUpperCase()} (${owner.deviceId}) on both workers within ${timeout}ms; last: ${JSON.stringify(last)}`
			);
		}
		await new Promise((r) => setTimeout(r, 500));
	}
}

/**
 * Watch the owner a worker reports for `chatId` until stopped; returns a
 * function resolving every distinct owner in order ([A, B] means it switched
 * exactly once). Polls each worker's API every 250 ms.
 */
export function watchOwners(ctx, chatId) {
	const seen = { a: [], b: [] };
	let running = true;
	let failures = 0;
	const loop = (async () => {
		// A case that failed before stopping it leaves a closed context behind:
		// give up after 10 s of failed reads so the journey process can exit.
		while (running && failures < 40) {
			const rows = await chatOnBoth(ctx, chatId).catch(() => null);
			failures = rows ? 0 : failures + 1;
			if (rows) {
				for (const role of ['a', 'b']) {
					const owner = ownerOf(rows[role]);
					const list = seen[role];
					if (owner && list[list.length - 1] !== owner) list.push(owner);
				}
			}
			await new Promise((r) => setTimeout(r, 250));
		}
	})();
	return async () => {
		running = false;
		await loop;
		return seen;
	};
}

/** The transcript a worker serves for a chat (GET /sessions/{id}): [{ id, role, text }]. */
export async function transcriptOn(ctx, member, chatId) {
	const body = await memberApi(ctx.context, member, 'GET', `/sessions/${chatId}`);
	return (body?.messages || []).map((m) => ({ id: String(m.id), role: m.role, type: m.type, text: String(m.content ?? '') }));
}

/** How many user messages of a transcript contain `needle`. */
export function userMessagesWith(transcript, needle) {
	return transcript.filter((m) => m.role === 'user' && m.text.includes(needle)).length;
}

/** Transcript row ids that appear more than once (a duplicated row). */
export function duplicateIds(transcript) {
	const seen = new Set();
	const dupes = new Set();
	for (const m of transcript) (seen.has(m.id) ? dupes : seen).add(m.id);
	return [...dupes];
}

/** `python -m scripts.devloop pair offline|online a|b` against this journey's pair. */
export async function setWorker(ctx, role, action) {
	const python = process.env.DEVLOOP_PYTHON || 'python3';
	const { stdout, stderr } = await run(python, ['-m', 'scripts.devloop', 'pair', action, role, '--status', ctx.pair.path], {
		cwd: CHECKOUT,
		timeout: 120_000
	}).catch((error) => {
		throw new Error(`pair ${action} ${role} failed: ${error.stderr || error.message}`);
	});
	return (stdout + stderr).trim();
}

/** Wait until worker `viewer` reports worker `role` online (true) or offline (false). */
export async function waitPeerOnline(ctx, viewer, role, online, { timeout = 90_000 } = {}) {
	const target = ctx.pair[role].deviceId;
	const deadline = Date.now() + timeout;
	let last;
	for (;;) {
		const body = await memberApi(ctx.context, ctx.pair[viewer], 'GET', '/network/members').catch((e) => ({ error: e.message }));
		const member = (body.members || []).find((m) => m.device_id === target);
		last = member ? member.online : body.error || 'not listed';
		if (member && member.online === online) return;
		if (Date.now() >= deadline) {
			throw new Error(`worker ${viewer.toUpperCase()} never saw worker ${role.toUpperCase()} ${online ? 'online' : 'offline'} within ${timeout}ms (last: ${last})`);
		}
		await new Promise((r) => setTimeout(r, 500));
	}
}
