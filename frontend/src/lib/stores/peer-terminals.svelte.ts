/**
 * Terminals on another worker (#196, #674), both ends.
 *
 * This device: sign one open for a peer worker, linking the device first if
 * that worker doesn't know it yet. The code shown while linking is worked out
 * here from the device's own key and is never sent; the owner types it on the
 * receiving worker.
 *
 * The receiving worker: the devices waiting to link here and the ones linked,
 * polled only while something shows them.
 *
 * Runtime only. The private key stays in the browser's key store and never
 * passes through here.
 */
import { terminalsApi, TerminalApiError, type DeviceProof, type LinkedDevice, type PeerLinkRequest, type ProofTarget } from '$lib/api/terminals';
import { deviceKey, deviceLabel, type DeviceKey } from '$lib/terminal/device-key';
import { LINK_SECONDS, PROOF_PURPOSE, requestErrorMessage, type PeerAction } from '$lib/terminal/peer';

export const LINK_POLL_MS = 2_500;
export const DEVICES_POLL_MS = 3_000;

export type LinkFlow =
	| { phase: 'linking'; workerId: string; name: string; code: string; expiresAt: number; unreachable: boolean }
	/** The link did not finish. `retry` is true when asking again can help. */
	| { phase: 'ended'; workerId: string; name: string; title: string; message: string; retry: boolean };

let flows = $state<Record<string, LinkFlow>>({});
let now = $state(Date.now());
let requests = $state<PeerLinkRequest[]>([]);
let links = $state<LinkedDevice[]>([]);
let fetchedAt = $state(0);
let loaded = $state(false);
let loadError = $state<string | null>(null);
let working = $state<Record<string, 'decline' | 'revoke'>>({});
let notices = $state<Record<string, string>>({});
let approving = $state(false);

/** Each worker's current attempt: a newer attempt or a cancel ends the older one. */
const attempts = new Map<string, number>();
let attemptSeq = 0;
let listSeq = 0;
let listInflight = 0;
let ticker: ReturnType<typeof setInterval> | null = null;
let listTimer: ReturnType<typeof setInterval> | null = null;
let watchers = 0;
/**
 * Workers with a link card counting down. Plain, not read from `flows`, because
 * syncTimers also runs from an effect teardown, where Svelte hands back a key
 * added in the same flush as undefined.
 */
const linking = new Set<string>();

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function syncTimers() {
	const counting = linking.size > 0 || watchers > 0;
	if (counting && !ticker) ticker = setInterval(() => { now = Date.now(); }, 1_000);
	if (!counting && ticker) { clearInterval(ticker); ticker = null; }
	if (watchers > 0 && !listTimer) listTimer = setInterval(() => {
		// A slow list must not be overtaken by the next tick, or each answer is dropped as stale.
		if (listInflight > 0) return;
		if (typeof document === 'undefined' || document.visibilityState !== 'hidden') void peerTerminals.refreshDevices();
	}, DEVICES_POLL_MS);
	if (watchers === 0 && listTimer) { clearInterval(listTimer); listTimer = null; }
}

function setFlow(workerId: string, flow: LinkFlow | null) {
	now = Date.now();
	if (flow) flows[workerId] = flow;
	else delete flows[workerId];
	if (flow?.phase === 'linking') linking.add(workerId);
	else linking.delete(workerId);
	syncTimers();
}

function end(workerId: string, name: string, title: string, message: string, retry = true) {
	setFlow(workerId, { phase: 'ended', workerId, name, title, message, retry });
}

const isOutage = (error: unknown) => !(error instanceof TerminalApiError) || error.status === 0 || error.status >= 500;

export const peerTerminals = {
	/** Wall clock for countdowns, ticking once a second while anything counts down. */
	get now(): number { return now; },

	// ── This device ───────────────────────────────────────────────────────
	flow(workerId: string | undefined): LinkFlow | null { return workerId ? flows[workerId] ?? null : null; },

	/**
	 * A signed proof for one open on `workerId`, or null when it did not happen
	 * (cancelled, or the reason is in the flow). If the device isn't linked yet
	 * this keeps waiting while the link card shows the code, then signs.
	 */
	async authorize(workerId: string, name: string, action: PeerAction, target: ProofTarget): Promise<DeviceProof | null> {
		attemptSeq += 1;
		const attempt = attemptSeq;
		attempts.set(workerId, attempt);
		const current = () => attempts.get(workerId) === attempt;
		setFlow(workerId, null);
		let key: DeviceKey;
		try {
			key = await deviceKey();
		} catch (error) {
			if (current()) end(workerId, name, "This device can't sign", error instanceof Error ? error.message : "This device can't hold a terminal key.", false);
			return null;
		}
		let asked = false;
		while (current()) {
			let answer;
			try {
				answer = await terminalsApi.peerChallenge(workerId, key.keyId);
			} catch (error) {
				if (!current()) return null;
				const flow = flows[workerId];
				if (flow?.phase === 'linking' && isOutage(error) && Date.now() < flow.expiresAt) {
					setFlow(workerId, { ...flow, unreachable: true });
					await sleep(LINK_POLL_MS);
					continue;
				}
				end(workerId, name, `Couldn't reach ${name}`, requestErrorMessage(error, name));
				return null;
			}
			if (!current()) return null;
			if (answer.status === 'linked' && answer.challenge) {
				setFlow(workerId, null);
				const payload = JSON.stringify({ purpose: PROOF_PURPOSE, receiver: answer.receiver, via: answer.via, challenge: answer.challenge, action, target });
				const signature = await key.sign(payload);
				return current() ? { key_id: key.keyId, payload, signature } : null;
			}
			if (answer.status === 'pending') {
				const flow = flows[workerId];
				if (flow?.phase === 'linking' && flow.unreachable) setFlow(workerId, { ...flow, unreachable: false });
			}
			if (answer.status === 'unlinked') {
				if (asked) {
					end(workerId, name, `The link to ${name} ended`, `The request ran out or was declined on ${name}. Ask again to get it there.`);
					return null;
				}
				asked = true;
				try {
					const link = await terminalsApi.requestPeerLink(workerId, key.publicKey, deviceLabel());
					if (!current()) return null;
					// Already linked (another tab finished first): the next round signs.
					if (link.status !== 'linked') {
						setFlow(workerId, { phase: 'linking', workerId, name, code: key.code, expiresAt: Date.now() + (link.expires_in ?? LINK_SECONDS) * 1000, unreachable: false });
					}
				} catch (error) {
					if (current()) end(workerId, name, `Couldn't start linking ${name}`, requestErrorMessage(error, name));
					return null;
				}
				continue;
			}
			if (answer.status === 'pending' && !flows[workerId]) {
				// A request from an earlier page is still waiting: show its code.
				asked = true;
				setFlow(workerId, { phase: 'linking', workerId, name, code: key.code, expiresAt: Date.now() + (answer.expires_in ?? LINK_SECONDS) * 1000, unreachable: false });
			}
			await sleep(LINK_POLL_MS);
		}
		return null;
	},

	/** Stop an attempt and forget its card. The request on the other worker expires by itself. */
	cancel(workerId: string) {
		attempts.delete(workerId);
		setFlow(workerId, null);
	},

	/** Forget a finished flow without contacting anyone. */
	clear(workerId: string) { setFlow(workerId, null); },

	/** Record why the shell did not open. */
	fail(workerId: string, name: string, title: string, message: string) { end(workerId, name, title, message); },

	// ── The receiving worker ──────────────────────────────────────────────
	get requests(): PeerLinkRequest[] {
		const elapsed = (now - fetchedAt) / 1000;
		return requests.filter((r) => r.expires_in - elapsed > 0);
	},
	get links(): LinkedDevice[] { return links; },
	get loaded(): boolean { return loaded; },
	get error(): string | null { return loadError; },
	get approving(): boolean { return approving; },
	/** Seconds left and age for one request, from the last answer plus local time. */
	timing(request: PeerLinkRequest): { left: number; age: number } {
		const elapsed = Math.max(0, (now - fetchedAt) / 1000);
		return { left: Math.max(0, request.expires_in - elapsed), age: request.age_seconds + elapsed };
	},
	working(id: string): 'decline' | 'revoke' | null { return working[id] ?? null; },
	notice(id: string): string | null { return notices[id] ?? null; },

	async refreshDevices(): Promise<void> {
		const seq = ++listSeq;
		listInflight += 1;
		try {
			const result = await terminalsApi.deviceLinks();
			if (seq !== listSeq) return; // A newer refresh or decision landed meanwhile.
			requests = result.requests ?? [];
			links = result.links ?? [];
			fetchedAt = Date.now();
			now = fetchedAt;
			loadError = null;
		} catch (error) {
			if (seq !== listSeq) return;
			// 401/403: not this worker's owner. Show nothing rather than an error.
			if (error instanceof TerminalApiError && (error.status === 401 || error.status === 403)) { requests = []; links = []; }
			else loadError = error instanceof Error ? error.message : 'Linked devices are unavailable right now.';
		} finally {
			listInflight = Math.max(0, listInflight - 1);
			loaded = true;
		}
	},

	/** Poll the device list while a surface shows it. Returns the stop function. */
	watch(): () => void {
		watchers += 1;
		syncTimers();
		if (watchers === 1) void this.refreshDevices();
		let stopped = false;
		return () => {
			if (stopped) return;
			stopped = true;
			watchers = Math.max(0, watchers - 1);
			syncTimers();
		};
	},

	/** Link the device whose code the owner typed here. Returns why not, or null when linked. */
	async approve(code: string): Promise<string | null> {
		if (approving) return null;
		approving = true;
		try {
			await terminalsApi.approveDeviceLink(code);
			listSeq += 1;
			await this.refreshDevices();
			return null;
		} catch (error) {
			if (error instanceof TerminalApiError && error.status === 404) return "No device is waiting with that code. Check it on the device and try again.";
			return error instanceof Error ? error.message : "Couldn't link the device. Try again.";
		} finally {
			approving = false;
		}
	},

	async decline(requestId: string): Promise<void> { await act(requestId, 'decline', () => terminalsApi.declineDeviceLink(requestId)); },
	/** Unlink a device. Any shell it has open here closes straight away. */
	async revoke(keyId: string): Promise<void> { await act(keyId, 'revoke', () => terminalsApi.revokeDeviceLink(keyId)); },

	/** Test hook. */
	reset() {
		flows = {}; requests = []; links = []; fetchedAt = 0; loaded = false; loadError = null; working = {}; notices = {}; approving = false;
		attempts.clear(); linking.clear(); listSeq = 0; listInflight = 0; watchers = 0; now = Date.now();
		syncTimers();
	},
};

async function act(id: string, kind: 'decline' | 'revoke', run: () => Promise<unknown>) {
	if (working[id]) return;
	working[id] = kind;
	delete notices[id];
	try {
		await run();
		listSeq += 1;
		if (kind === 'decline') requests = requests.filter((r) => r.request_id !== id);
		else links = links.filter((d) => d.key_id !== id);
	} catch (error) {
		if (error instanceof TerminalApiError && error.status === 404) { /* Already gone: the refresh shows it. */ }
		else notices[id] = error instanceof Error ? error.message : "That didn't work. Try again.";
	} finally {
		delete working[id];
		void peerTerminals.refreshDevices();
	}
}
