/**
 * Background-task chips of the open chat.
 *
 * The chip list on the root chat is the only source: a chip raised by a
 * subagent lives on the root chat, so nothing here reads a transcript. The
 * `task_chips_changed` frame carries no content and only says "re-read"; frames
 * can be missed, so the list is also read on switching in and after a reconnect.
 *
 * Which started chips stay in the tray is decided here (`visibleTaskChips`):
 * a started chip stays until this browser has opened its chat, the owner clears
 * it, or {@link STARTED_CHIP_TTL_MS} has passed since it started. The worker has
 * no record of that (it refuses to dismiss a started chip), so it is per browser,
 * which keeps a chip started on another device findable here.
 */
import { writable, type Readable } from 'svelte/store';
import {
	dismissTaskChip,
	listTaskChips,
	startTaskChip,
	type TaskChip,
	type TaskChipList,
	type TaskChipSource
} from '$lib/api/sessions';
import { isApiError } from '$lib/api/client';
import { sessionOwnerBoundary } from '$lib/stores/session-owner';

export const STARTED_CHIP_TTL_MS = 24 * 60 * 60 * 1000;
export const STARTING_RECHECK_MS = 4000;
const SEEN_KEY = 'ai-shuffle-task-chips-seen';
const SEEN_LIMIT = 300;
const CACHE_LIMIT = 32;

export type TaskChipAction = 'start' | 'dismiss';

export interface TaskChipsView {
	sessionId: string | null;
	/** What the tray shows, in the worker's order (oldest first). */
	chips: TaskChip[];
	source: TaskChipSource | null;
	/** A click on its way to the worker. */
	busy: Record<string, TaskChipAction>;
	/** Why the last click did not reach the worker; the chip stays actionable. */
	errors: Record<string, string>;
	/** The list has been read for this chat at least once. */
	loaded: boolean;
}

export interface TaskChipDeps {
	list: (sessionId: string) => Promise<TaskChipList>;
	start: (sessionId: string, chipId: string) => Promise<TaskChip>;
	dismiss: (sessionId: string, chipId: string) => Promise<TaskChip>;
	storage?: Pick<Storage, 'getItem' | 'setItem'> | null;
	now?: () => number;
}

export function visibleTaskChips(chips: readonly TaskChip[], seen: ReadonlySet<string>, now: number): TaskChip[] {
	return chips.filter((chip) => {
		if (chip.state === 'dismissed') return false;
		if (chip.state !== 'started') return true;
		const startedAt = Date.parse(chip.updated_at);
		return !seen.has(chip.id) && !(startedAt && now - startedAt > STARTED_CHIP_TTL_MS);
	});
}

/** The model the owner's click approves, in the words the chip shows. */
export function taskChipModel(chip: Pick<TaskChip, 'model' | 'model_label' | 'effort'>): string {
	const label = chip.model_label || chip.model;
	return chip.effort ? `${label} · ${chip.effort}` : label;
}

function clickError(error: unknown, action: TaskChipAction): string {
	if (isApiError(error)) {
		if (error.status === 503) {
			return action === 'start'
				? 'The worker running this chat is offline, so nothing started. Try again when it is back.'
				: 'The worker running this chat is offline. Try again when it is back.';
		}
		if (error.detail) return error.detail;
	}
	return action === 'start'
		? 'Could not reach the app to start this. Check your connection and try again.'
		: 'Could not reach the app to dismiss this. Check your connection and try again.';
}

interface Entry {
	sessionId: string;
	chips: TaskChip[];
	source: TaskChipSource | null;
	busy: Record<string, TaskChipAction>;
	errors: Record<string, string>;
	loaded: boolean;
	/** Bumped by a click's answer, so an older read in flight cannot undo it. */
	revision: number;
	reading: boolean;
	readAgain: boolean;
}

export function createTaskChips(deps: TaskChipDeps) {
	const empty = (sessionId: string | null): TaskChipsView => ({ sessionId, chips: [], source: null, busy: {}, errors: {}, loaded: false });
	const value = writable<TaskChipsView>(empty(null));
	const entries = new Map<string, Entry>();
	const now = deps.now ?? Date.now;
	let current: string | null = null;
	let recheck: ReturnType<typeof setTimeout> | null = null;

	function readSeen(): string[] {
		try {
			const parsed = JSON.parse(deps.storage?.getItem(SEEN_KEY) ?? '[]');
			return Array.isArray(parsed) ? parsed.filter((id) => typeof id === 'string') : [];
		} catch {
			return [];
		}
	}
	let seen = readSeen();

	function publish() {
		const entry = current ? entries.get(current) : undefined;
		value.set(entry ? {
			sessionId: entry.sessionId,
			chips: visibleTaskChips(entry.chips, new Set(seen), now()),
			source: entry.source,
			busy: entry.busy,
			errors: entry.errors,
			loaded: entry.loaded
		} : empty(current));
		if (recheck !== null) clearTimeout(recheck);
		recheck = null;
		// The start's own answer settles a chip this tab clicked. One started
		// elsewhere relies on a frame, and must not sit on "Starting" if it is lost.
		if (entry?.chips.some((chip) => chip.state === 'starting' && !entry.busy[chip.id])) {
			recheck = setTimeout(() => { recheck = null; if (current === entry.sessionId) void read(entry); }, STARTING_RECHECK_MS);
		}
	}

	function markSeen(chipId: string) {
		if (seen.includes(chipId)) return;
		seen = [...readSeen().filter((id) => id !== chipId), chipId].slice(-SEEN_LIMIT);
		try { deps.storage?.setItem(SEEN_KEY, JSON.stringify(seen)); } catch { /* still hidden in this tab */ }
	}

	function entryFor(sessionId: string): Entry {
		let entry = entries.get(sessionId);
		if (!entry) {
			entry = { sessionId, chips: [], source: null, busy: {}, errors: {}, loaded: false, revision: 0, reading: false, readAgain: false };
			entries.set(sessionId, entry);
			for (const [id, other] of entries) {
				if (entries.size <= CACHE_LIMIT) break;
				if (id !== current && id !== sessionId && !Object.keys(other.busy).length) entries.delete(id);
			}
		}
		return entry;
	}

	function pruneErrors(entry: Entry) {
		const open = new Set(entry.chips.filter((chip) => chip.state === 'pending' || chip.state === 'failed').map((chip) => chip.id));
		const kept = Object.entries(entry.errors).filter(([id]) => open.has(id));
		if (kept.length !== Object.keys(entry.errors).length) entry.errors = Object.fromEntries(kept);
	}

	async function read(entry: Entry): Promise<void> {
		if (entry.reading) { entry.readAgain = true; return; }
		entry.reading = true;
		entry.readAgain = false;
		const revision = entry.revision;
		try {
			const list = await deps.list(entry.sessionId);
			if (entries.get(entry.sessionId) !== entry) return;
			if (revision !== entry.revision) {
				entry.readAgain = true;
			} else {
				entry.chips = list.chips ?? [];
				entry.source = list.source ?? null;
				entry.loaded = true;
				// Opening a chat a chip started is what clears that chip, by any route.
				if (entry.source) markSeen(entry.source.chip_id);
				pruneErrors(entry);
				if (current === entry.sessionId) publish();
			}
		} catch {
			// What shows stays; the next frame, switch or reconnect reads again.
		} finally {
			entry.reading = false;
			if (entry.readAgain && current === entry.sessionId) void read(entry);
		}
	}

	function settle(entry: Entry, chipId: string, chip?: TaskChip, error?: string) {
		const { [chipId]: _done, ...busy } = entry.busy;
		entry.busy = busy;
		if (chip) {
			entry.revision++;
			entry.chips = entry.chips.map((row) => (row.id === chip.id ? chip : row));
		}
		const { [chipId]: _old, ...errors } = entry.errors;
		entry.errors = error ? { ...errors, [chipId]: error } : errors;
		pruneErrors(entry);
		if (current === entry.sessionId) publish();
	}

	async function act(action: TaskChipAction, chipId: string) {
		if (!current) return;
		const entry = entryFor(current);
		if (entry.busy[chipId]) return;
		entry.busy = { ...entry.busy, [chipId]: action };
		const { [chipId]: _old, ...errors } = entry.errors;
		entry.errors = errors;
		publish();
		try {
			const chip = await (action === 'start' ? deps.start : deps.dismiss)(entry.sessionId, chipId);
			if (entries.get(entry.sessionId) === entry) settle(entry, chipId, chip);
		} catch (error) {
			if (entries.get(entry.sessionId) !== entry) return;
			settle(entry, chipId, undefined, clickError(error, action));
			// The answer may be lost, not the action, and a 404 or 409 means the
			// list here is out of date. The read shows what really happened.
			void read(entry);
		}
	}

	return {
		subscribe: value.subscribe as Readable<TaskChipsView>['subscribe'],
		/** Follow the open chat. Publishes that chat's last known chips straight away, never another's. */
		observe(sessionId: string | null) {
			if (sessionId === current) return;
			current = sessionId;
			if (sessionId) {
				const entry = entryFor(sessionId);
				publish();
				void read(entry);
			} else {
				publish();
			}
		},
		/** A `task_chips_changed` frame. */
		receive(sessionId: unknown) {
			if (sessionId && sessionId === current) void read(entryFor(current));
		},
		/** Frames may have been missed while the socket was down. */
		refresh() {
			if (current) void read(entryFor(current));
		},
		start: (chipId: string) => act('start', chipId),
		dismiss: (chipId: string) => act('dismiss', chipId),
		/** The owner opened a started chip's chat, or cleared it. Started chips cannot be dismissed on the worker. */
		clear(chipId: string) {
			markSeen(chipId);
			publish();
		},
		/** Another tab of this browser opened or cleared a started chip. */
		syncSeen() {
			seen = readSeen();
			publish();
		},
		/** A different signed-in owner: nothing cached may carry over. */
		reset() {
			entries.clear();
			seen = readSeen();
			if (current) {
				const entry = entryFor(current);
				publish();
				void read(entry);
			} else {
				publish();
			}
		}
	};
}

export const taskChips = createTaskChips({
	list: (sessionId) => listTaskChips(sessionId),
	start: (sessionId, chipId) => startTaskChip(sessionId, chipId),
	dismiss: (sessionId, chipId) => dismissTaskChip(sessionId, chipId),
	storage: typeof localStorage === 'undefined' ? null : localStorage
});

if (typeof window !== 'undefined') {
	window.addEventListener('storage', (event) => {
		if (event.key === SEEN_KEY) taskChips.syncSeen();
	});
}

let ownerBound = false;
sessionOwnerBoundary.subscribe(() => {
	// subscribe() calls back at once; only a later change is a new owner.
	if (ownerBound) taskChips.reset();
	ownerBound = true;
});
