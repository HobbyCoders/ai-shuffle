/**
 * Owner-approved peer terminals (#196), both ends.
 *
 * Source (the worker the owner is using): one request flow per peer worker.
 * Request, show the confirmation code while the other worker's owner
 * decides, then hold the approval until the panel opens its single shell.
 *
 * Recipient (the worker that would run the shell): the requests waiting here
 * and the approvals still alive, polled only while something shows them.
 *
 * Runtime only. Authorization ids are opaque in-memory leases: they never
 * reach localStorage, a URL other than the owner API routes, or a log.
 */
import { terminalsApi, TerminalApiError, type PeerAuthorization, type PeerRequest } from '$lib/api/terminals';
import { requestErrorMessage } from '$lib/terminal/peer';

export const SOURCE_POLL_MS = 2_000;
export const RECIPIENT_POLL_MS = 3_000;

export type SourceFlow =
	| { phase: 'requesting'; workerId: string; name: string }
	| { phase: 'pending'; workerId: string; name: string; authorizationId: string; code: string; expiresAt: number; unreachable: boolean }
	| { phase: 'approved'; workerId: string; name: string; authorizationId: string; code: string; expiresAt: number }
	/** The request did not lead to a shell. `retry` is true when asking again can help. */
	| { phase: 'ended'; workerId: string; name: string; title: string; message: string; retry: boolean };

let flows = $state<Record<string, SourceFlow>>({});
let now = $state(Date.now());
let requests = $state<PeerRequest[]>([]);
let fetchedAt = $state(0);
let recipientLoaded = $state(false);
let recipientError = $state<string | null>(null);
let decisions = $state<Record<string, 'approve' | 'deny' | 'revoke'>>({});
let notices = $state<Record<string, string>>({});

let ticker: ReturnType<typeof setInterval> | null = null;
let sourceTimer: ReturnType<typeof setInterval> | null = null;
let recipientTimer: ReturnType<typeof setInterval> | null = null;
let watchers = 0;

const live = (flow: SourceFlow | undefined): flow is Extract<SourceFlow, { phase: 'pending' | 'approved' }> =>
	!!flow && (flow.phase === 'pending' || flow.phase === 'approved');

function syncTimers() {
	const sourceActive = Object.values(flows).some(live);
	const anyActive = sourceActive || watchers > 0;
	if (anyActive && !ticker) ticker = setInterval(() => { now = Date.now(); }, 1_000);
	if (!anyActive && ticker) { clearInterval(ticker); ticker = null; }
	if (sourceActive && !sourceTimer) sourceTimer = setInterval(() => void pollSources(), SOURCE_POLL_MS);
	if (!sourceActive && sourceTimer) { clearInterval(sourceTimer); sourceTimer = null; }
	if (watchers > 0 && !recipientTimer) recipientTimer = setInterval(() => {
		if (typeof document === 'undefined' || document.visibilityState !== 'hidden') void peerTerminals.refreshRequests();
	}, RECIPIENT_POLL_MS);
	if (watchers === 0 && recipientTimer) { clearInterval(recipientTimer); recipientTimer = null; }
}

function setFlow(workerId: string, flow: SourceFlow | null) {
	now = Date.now();
	if (flow) flows[workerId] = flow;
	else delete flows[workerId];
	syncTimers();
}

const EXPIRED = (name: string) => `No approval from ${name}`;

function ended(flow: { workerId: string; name: string }, message: string, retry = true, title = EXPIRED(flow.name)): SourceFlow {
	return { phase: 'ended', workerId: flow.workerId, name: flow.name, title, message, retry };
}

/** Fold one status answer into the flow it belongs to. */
function apply(workerId: string, result: PeerAuthorization) {
	const flow = flows[workerId];
	if (!live(flow) || flow.authorizationId !== result.authorization_id) return;
	const expiresAt = typeof result.expires_in === 'number' ? Date.now() + result.expires_in * 1000 : flow.expiresAt;
	const code = result.confirmation_code || flow.code;
	if (result.status === 'pending') {
		setFlow(workerId, { phase: 'pending', workerId, name: flow.name, authorizationId: flow.authorizationId, code, expiresAt, unreachable: false });
	} else if (result.status === 'approved' && !result.used) {
		setFlow(workerId, { phase: 'approved', workerId, name: flow.name, authorizationId: flow.authorizationId, code, expiresAt });
	} else if (result.status === 'approved') {
		setFlow(workerId, ended(flow, 'That approval was already used. Each approval opens one shell.'));
	} else {
		// The recipient answers `expired` for both a timed-out request and one
		// its owner denied or revoked: it does not say which.
		setFlow(workerId, ended(flow, `The request expired, or it was denied or revoked on ${flow.name}. Request again to get a new code.`));
	}
}

async function poll(workerId: string) {
	const flow = flows[workerId];
	if (!live(flow)) return;
	try {
		apply(workerId, await terminalsApi.peerAuthorization(flow.authorizationId));
	} catch (error) {
		const current = flows[workerId];
		if (!live(current) || current.authorizationId !== flow.authorizationId) return;
		if (error instanceof TerminalApiError && (error.status === 403 || error.status === 404)) {
			setFlow(workerId, ended(current, `The request is no longer valid here. Request again to get a new code.`));
		} else if (current.phase === 'pending') {
			// Keep waiting: the recipient may answer on the next poll.
			setFlow(workerId, { ...current, unreachable: true });
		}
	}
}

async function pollSources() {
	const at = Date.now();
	for (const flow of Object.values(flows)) {
		if (!live(flow)) continue;
		// The recipient is the authority on expiry; this is the backstop when it stops answering.
		if (at >= flow.expiresAt + SOURCE_POLL_MS * 2) {
			setFlow(flow.workerId, flow.phase === 'pending'
				? ended(flow, `${flow.name} didn't approve in time. Request again to get a new code.`)
				: ended(flow, `The approval on ${flow.name} ran out before a shell opened.`));
			continue;
		}
		void poll(flow.workerId);
	}
}

export const peerTerminals = {
	/** Wall clock for countdowns, ticking once a second while anything counts down. */
	get now(): number { return now; },

	// ── Source ────────────────────────────────────────────────────────────
	flow(workerId: string | undefined): SourceFlow | null { return workerId ? flows[workerId] ?? null : null; },

	/** Ask `workerId` for one shell. Its owner approves it on that worker. */
	async request(workerId: string, name: string): Promise<void> {
		const current = flows[workerId];
		if (current?.phase === 'requesting' || live(current)) return;
		setFlow(workerId, { phase: 'requesting', workerId, name });
		try {
			const result = await terminalsApi.requestPeer(workerId);
			if (flows[workerId]?.phase !== 'requesting') {
				// Cancelled while the request was in flight: withdraw it.
				void terminalsApi.revokePeerAuthorization(result.authorization_id).catch(() => {});
				return;
			}
			if (result.status === 'unavailable' || result.available === false) {
				setFlow(workerId, ended({ workerId, name }, result.reason || `${name} can't host a terminal right now.`, false, `${name} is unavailable`));
				return;
			}
			const expiresAt = Date.now() + (result.expires_in ?? 120) * 1000;
			const code = result.confirmation_code ?? '';
			const base = { workerId, name, authorizationId: result.authorization_id, code, expiresAt };
			setFlow(workerId, result.status === 'approved' && !result.used
				? { phase: 'approved', ...base }
				: { phase: 'pending', ...base, unreachable: false });
		} catch (error) {
			if (flows[workerId]?.phase !== 'requesting') return;
			setFlow(workerId, ended({ workerId, name }, requestErrorMessage(error, name), true, `Couldn't request a shell on ${name}`));
		}
	},

	/** Withdraw a pending request or an unused approval. Local authority ends even if the peer is unreachable. */
	async cancel(workerId: string): Promise<void> {
		const flow = flows[workerId];
		setFlow(workerId, null);
		if (!live(flow)) return;
		try {
			await terminalsApi.revokePeerAuthorization(flow.authorizationId);
		} catch {
			// The source forgot it before calling out; the recipient expires its side by itself.
		}
	},

	/** Forget a flow without contacting anyone: the approval was used (or its error shown). */
	clear(workerId: string) { setFlow(workerId, null); },

	/** Record why the single shell did not open; the approval may be spent. */
	fail(workerId: string, name: string, title: string, message: string) {
		setFlow(workerId, ended({ workerId, name }, message, true, title));
	},

	/** Test hook and sign-out. */
	poll: (workerId: string) => poll(workerId),

	// ── Recipient ─────────────────────────────────────────────────────────
	get requests(): PeerRequest[] {
		const elapsed = (now - fetchedAt) / 1000;
		return requests.filter((r) => r.expires_in - elapsed > 0);
	},
	get pendingCount(): number { return this.requests.filter((r) => r.status === 'pending').length; },
	get loaded(): boolean { return recipientLoaded; },
	get error(): string | null { return recipientError; },
	/** Seconds left and age for one request, from the last answer plus local time. */
	timing(request: PeerRequest): { left: number; age: number } {
		const elapsed = Math.max(0, (now - fetchedAt) / 1000);
		return { left: Math.max(0, request.expires_in - elapsed), age: request.age_seconds + elapsed };
	},
	decision(requestId: string): 'approve' | 'deny' | 'revoke' | null { return decisions[requestId] ?? null; },
	/** A per-request failure, shown on its card. */
	notice(requestId: string): string | null { return notices[requestId] ?? null; },

	async refreshRequests(): Promise<void> {
		try {
			const result = await terminalsApi.peerRequests();
			requests = result.requests ?? [];
			fetchedAt = Date.now();
			now = fetchedAt;
			recipientError = null;
			for (const id of Object.keys(notices)) if (!requests.some((r) => r.request_id === id)) delete notices[id];
		} catch (error) {
			// 401/403: not this worker's owner. Show nothing rather than an error.
			if (error instanceof TerminalApiError && (error.status === 401 || error.status === 403)) requests = [];
			else recipientError = error instanceof Error ? error.message : 'Terminal requests are unavailable right now.';
		} finally {
			recipientLoaded = true;
		}
	},

	/** Poll the recipient list while a surface shows it. Returns the stop function. */
	watch(): () => void {
		watchers += 1;
		syncTimers();
		if (watchers === 1) void this.refreshRequests();
		let stopped = false;
		return () => {
			if (stopped) return;
			stopped = true;
			watchers = Math.max(0, watchers - 1);
			syncTimers();
		};
	},

	async approve(requestId: string): Promise<void> { await decide(requestId, 'approve'); },
	/** Deny a pending request, or revoke an approval and close its shell. */
	async revoke(requestId: string, kind: 'deny' | 'revoke' = 'revoke'): Promise<void> { await decide(requestId, kind); },

	/** Test hook. */
	reset() {
		flows = {}; requests = []; fetchedAt = 0; recipientLoaded = false; recipientError = null; decisions = {}; notices = {};
		watchers = 0; now = Date.now();
		syncTimers();
	},
};

async function decide(requestId: string, kind: 'approve' | 'deny' | 'revoke') {
	if (decisions[requestId]) return;
	decisions[requestId] = kind;
	delete notices[requestId];
	try {
		if (kind === 'approve') {
			const approved = await terminalsApi.approvePeerRequest(requestId);
			requests = requests.map((r) => (r.request_id === requestId ? { ...r, ...approved, expires_in: approved.expires_in + (Date.now() - fetchedAt) / 1000 } : r));
		} else {
			await terminalsApi.revokePeerRequest(requestId);
			requests = requests.filter((r) => r.request_id !== requestId);
		}
	} catch (error) {
		notices[requestId] = kind === 'approve'
			? (error instanceof TerminalApiError && error.status === 403
				? 'This request expired, was already approved, or peer terminals were turned off. Ask for a new one.'
				: error instanceof Error ? error.message : "Couldn't approve. Try again.")
			: error instanceof TerminalApiError && error.status === 403
				? 'This request already ended.'
				: error instanceof Error ? error.message : "Couldn't revoke. Try again.";
	} finally {
		delete decisions[requestId];
		void peerTerminals.refreshRequests();
	}
}
