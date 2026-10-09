/**
 * In-app terminals (#1945, phase 2): tabs per chat, the capability probe and
 * requests from command blocks.
 *
 * Runtime only. Nothing here reaches localStorage, a preference or a log:
 * terminals live in the worker process, and after a reload the chat's tabs
 * come back from `GET /terminals?session_id=`. Terminal input and output never
 * enter this store; each tab's socket feeds its emulator directly.
 */
import { terminalsApi, TerminalApiError, type TerminalInfo, type TerminalTargets } from '$lib/api/terminals';
import { sidePanel, sessionPanelKey } from './side-panel.svelte';
import type { ProbeState } from '$lib/terminal/gating';
import type { CommandBlock } from '$lib/terminal/commands';

export type ConnStatus = 'connecting' | 'live' | 'reconnecting' | 'gone' | 'auth' | 'error';

interface SessionTabs { ids: string[]; active: string | null; loaded: boolean; error: string | null }

/** What a command block asked the panel to do once it is open. */
export interface TerminalRequest { seq: number; action: 'open' | 'paste'; block: CommandBlock }

const PROBE_TTL = 60_000;
/** A command block's request that the panel has not acted on by now is dropped, never replayed later. */
export const REQUEST_TTL = 15_000;
export const REQUEST_EXPIRED = "The terminal didn't open in time, so nothing was pasted. Click the button again.";
const emptyTabs: SessionTabs = { ids: [], active: null, loaded: false, error: null };

/**
 * The scope of terminals that belong to no chat: the Terminal page, opened
 * from the navigation or from Settings before any chat exists (signing in to
 * a CLI, other setup). Chat ids are UUIDs, so this key never collides with
 * one. Its terminals are created with no `session_id`, open in the worker's
 * workspace (else home) folder, and are the worker's terminals whose
 * `session_id` is null.
 */
export const WORKER_TERMINALS = ':worker';

/** The chat id to send for a scope: null for the worker's own terminals. */
const chatFor = (scope: string | null): string | null => (scope === WORKER_TERMINALS ? null : scope);

let capability = $state<ProbeState>({ status: 'idle' });
let probes = $state<Record<string, ProbeState>>({});
let tabs = $state<Record<string, SessionTabs>>({});
let terms = $state<Record<string, TerminalInfo>>({});
let conn = $state<Record<string, { status: ConnStatus; message: string | null }>>({});
let notices = $state<Record<string, string | null>>({});
let requests = $state<Record<string, TerminalRequest | null>>({});
let counts = $state<{ open: number; running: number; limit: number }>({ open: 0, running: 0, limit: 8 });

const probedAt = new Map<string, number>();
const inflight = new Map<string, Promise<ProbeState>>();
let capabilityPromise: Promise<ProbeState> | null = null;
let requestSeq = 0;
const requestTimers = new Map<string, ReturnType<typeof setTimeout>>();
/** The button each pending request came from: the panel may move focus off it. Never reactive state. */
const requestOrigins = new Map<string, Element | null>();

function clearRequestTimer(sessionId: string) {
	const timer = requestTimers.get(sessionId);
	if (timer) clearTimeout(timer);
	requestTimers.delete(sessionId);
}

/** Map a targets answer (or refusal) to the gate's input. */
export function probeFromTargets(result: TerminalTargets): ProbeState {
	return {
		status: 'ready',
		supported: result.supported,
		reason: result.unsupported_reason,
		worker: result.worker?.name || 'this worker',
		shells: result.shells.map((shell) => shell.name),
		defaultShell: result.default_shell,
		folder: result.default_folder?.path ?? null,
		folderSource: result.default_folder?.source ?? null,
		open: result.open,
		limit: result.limit,
	};
}

export function probeFromError(error: unknown): ProbeState {
	if (error instanceof TerminalApiError) {
		if (error.status === 401 || error.status === 403) return { status: 'denied' };
		return { status: 'error', code: error.code, message: error.message };
	}
	return { status: 'error', code: 'unknown', message: 'Terminals are unavailable right now.' };
}

async function probe(sessionId: string | null): Promise<ProbeState> {
	try {
		return probeFromTargets(await terminalsApi.targets(chatFor(sessionId)));
	} catch (error) {
		return probeFromError(error);
	}
}

function setTabs(sessionId: string, next: Partial<SessionTabs>) {
	tabs[sessionId] = { ...(tabs[sessionId] ?? emptyTabs), ...next };
}

function message(error: unknown): string {
	return error instanceof Error ? error.message : 'Something went wrong. Try again.';
}

export const terminals = {
	/** Worker-wide gate for the composer toggle and the phone sheet cell. */
	get capability(): ProbeState { return capability; },
	/** Worker-wide counts from the last list call. */
	get counts() { return counts; },

	/** Probe once per page load. A denial is final until reload. */
	ensureCapability(): Promise<ProbeState> {
		if (capability.status === 'ready' || capability.status === 'denied') return Promise.resolve(capability);
		capabilityPromise ??= (async () => {
			capability = { status: 'loading' };
			capability = await probe(null);
			capabilityPromise = null;
			return capability;
		})();
		return capabilityPromise;
	},

	/** The chat's own probe: default folder, shells, and whether it runs here. */
	probeFor(sessionId: string | null | undefined): ProbeState {
		if (!sessionId) return capability;
		return probes[sessionId] ?? { status: 'idle' };
	},

	ensureProbe(sessionId: string, force = false): Promise<ProbeState> {
		// A denied viewer is never probed per chat: the answer is the same.
		if (capability.status === 'denied') return Promise.resolve(capability);
		const fresh = Date.now() - (probedAt.get(sessionId) ?? 0) < PROBE_TTL;
		const known = probes[sessionId];
		if (!force && known && known.status !== 'idle' && fresh) return Promise.resolve(known);
		const pending = inflight.get(sessionId);
		if (pending) return pending;
		const run = (async () => {
			if (!known) probes[sessionId] = { status: 'loading' };
			const result = await probe(sessionId);
			probes[sessionId] = result;
			probedAt.set(sessionId, Date.now());
			if (result.status === 'denied') capability = result;
			if (result.status === 'ready' && capability.status !== 'ready') capability = result;
			// The probe carries the worker-wide count, so the cap stays honest
			// when terminals open or close outside this chat.
			if (result.status === 'ready') counts = { ...counts, open: result.open, limit: result.limit };
			inflight.delete(sessionId);
			return result;
		})();
		inflight.set(sessionId, run);
		return run;
	},

	tabsFor(sessionId: string): SessionTabs { return tabs[sessionId] ?? emptyTabs; },
	terminal(id: string): TerminalInfo | null { return terms[id] ?? null; },
	connection(id: string) { return conn[id] ?? { status: 'connecting' as ConnStatus, message: null }; },
	notice(sessionId: string): string | null { return notices[sessionId] ?? null; },
	setNotice(sessionId: string, text: string | null) { notices[sessionId] = text; },

	/** List the chat's terminals: this is how tabs come back after a reload. */
	async load(sessionId: string): Promise<void> {
		try {
			const chat = chatFor(sessionId);
			const result = await terminalsApi.list(chat);
			counts = { open: result.open, running: result.running, limit: result.limit };
			// Unfiltered, the list holds every chat's terminals too: keep the chatless ones.
			const own = chat ? result.terminals : result.terminals.filter((t) => t.session_id === null);
			for (const info of own) terms[info.id] = info;
			const current = tabs[sessionId] ?? emptyTabs;
			const listed = own.map((t) => t.id);
			// Keep the owner's tab order; append terminals opened elsewhere.
			const ids = [...current.ids.filter((id) => listed.includes(id)), ...listed.filter((id) => !current.ids.includes(id))];
			const active = current.active && ids.includes(current.active) ? current.active : ids[ids.length - 1] ?? null;
			setTabs(sessionId, { ids, active, loaded: true, error: null });
		} catch (error) {
			setTabs(sessionId, { loaded: true, error: message(error) });
		}
	},

	select(sessionId: string, id: string) {
		if (this.tabsFor(sessionId).ids.includes(id)) setTabs(sessionId, { active: id });
	},

	/** Open a terminal for the chat. Returns null and sets a notice on refusal. */
	async create(sessionId: string, options: { cwd?: string | null; shell?: string | null; cols?: number; rows?: number } = {}): Promise<TerminalInfo | null> {
		notices[sessionId] = null;
		try {
			const info = await terminalsApi.create({ session_id: chatFor(sessionId), ...options });
			terms[info.id] = info;
			const current = this.tabsFor(sessionId);
			setTabs(sessionId, { ids: [...current.ids.filter((id) => id !== info.id), info.id], active: info.id, loaded: true });
			counts = { ...counts, open: counts.open + 1 };
			return info;
		} catch (error) {
			notices[sessionId] = message(error);
			if (error instanceof TerminalApiError && error.code === 'limit_reached') counts = { ...counts, open: counts.limit };
			return null;
		}
	},

	/** Remove a tab locally (its terminal is gone or was closed). */
	drop(id: string) {
		for (const [sessionId, entry] of Object.entries(tabs)) {
			if (!entry.ids.includes(id)) continue;
			const index = entry.ids.indexOf(id);
			const ids = entry.ids.filter((other) => other !== id);
			const active = entry.active === id ? ids[Math.min(index, ids.length - 1)] ?? null : entry.active;
			setTabs(sessionId, { ids, active });
		}
		delete terms[id];
		delete conn[id];
	},

	async close(sessionId: string, id: string): Promise<void> {
		try {
			await terminalsApi.close(id);
			counts = { ...counts, open: Math.max(0, counts.open - 1) };
		} catch (error) {
			if (!(error instanceof TerminalApiError && error.code === 'not_found')) {
				notices[sessionId] = message(error);
				return;
			}
		}
		this.drop(id);
	},

	async restart(sessionId: string, id: string): Promise<void> {
		notices[sessionId] = null;
		try {
			terms[id] = await terminalsApi.restart(id);
		} catch (error) {
			notices[sessionId] = message(error);
		}
	},

	/** Put text at the prompt. The server cleans it; nothing runs without Enter. */
	async paste(sessionId: string, id: string, text: string): Promise<'bracketed' | 'plain' | null> {
		notices[sessionId] = null;
		try {
			const result = await terminalsApi.paste(id, text);
			terms[id] = result.terminal;
			return result.mode;
		} catch (error) {
			notices[sessionId] = message(error);
			return null;
		}
	},

	update(info: TerminalInfo) { if (terms[info.id] || this.owns(info.id)) terms[info.id] = info; },
	owns(id: string): boolean { return Object.values(tabs).some((entry) => entry.ids.includes(id)); },
	setConnection(id: string, status: ConnStatus, text: string | null = null) {
		if (!this.owns(id)) return;
		const current = conn[id];
		if (current?.status === status && current.message === text) return;
		conn[id] = { status, message: text };
	},

	/**
	 * A command block's button: open the panel, then let it act on the
	 * request. The request lives for REQUEST_TTL at most; it is dropped when
	 * the panel closes or can't act, so an old click never replays later.
	 */
	request(sessionId: string, action: 'open' | 'paste', block: CommandBlock, origin: Element | null = null) {
		requestSeq += 1;
		const seq = requestSeq;
		requests[sessionId] = { seq, action, block };
		requestOrigins.set(sessionId, origin);
		clearRequestTimer(sessionId);
		requestTimers.set(sessionId, setTimeout(() => {
			if (requests[sessionId]?.seq === seq) terminals.dropRequest(sessionId, REQUEST_EXPIRED);
		}, REQUEST_TTL));
		sidePanel.open(sessionPanelKey(sessionId), 'terminal');
	},
	pendingRequest(sessionId: string): TerminalRequest | null { return requests[sessionId] ?? null; },
	/** The element the request's click came from, if any. */
	requestOrigin(sessionId: string): Element | null { return requestOrigins.get(sessionId) ?? null; },
	takeRequest(sessionId: string, seq: number): TerminalRequest | null {
		const pending = requests[sessionId];
		if (!pending || pending.seq !== seq) return null;
		requests[sessionId] = null;
		clearRequestTimer(sessionId);
		return pending;
	},
	/** Forget a waiting request. With a notice, tell the owner why nothing happened. */
	dropRequest(sessionId: string, notice: string | null = null) {
		clearRequestTimer(sessionId);
		requestOrigins.delete(sessionId);
		if (!requests[sessionId]) return;
		requests[sessionId] = null;
		if (notice) notices[sessionId] = notice;
	},

	/** Open or close the panel for a chat (composer toggle, phone sheet). */
	toggle(sessionId: string | null) {
		if (!sessionId) return;
		const key = sessionPanelKey(sessionId);
		if (sidePanel.forScope(key).active === 'terminal') {
			terminals.dropRequest(sessionId);
			sidePanel.close(key, 'terminal');
		} else sidePanel.open(key, 'terminal');
	},
	isOpen(sessionId: string | null): boolean {
		return !!sessionId && sidePanel.forScope(sessionPanelKey(sessionId)).active === 'terminal';
	},

	/** Test hook. */
	reset() {
		capability = { status: 'idle' };
		probes = {}; tabs = {}; terms = {}; conn = {}; notices = {}; requests = {};
		counts = { open: 0, running: 0, limit: 8 };
		probedAt.clear(); inflight.clear(); capabilityPromise = null;
		for (const timer of requestTimers.values()) clearTimeout(timer);
		requestTimers.clear(); requestOrigins.clear();
	},
};
