/**
 * Which transcript folds the reader opened (transcript-organization spec §6).
 *
 * A fold key is `<turn key>:run-<first row>..<last row>` for a live run and
 * `<turn key>:turn` for the settled turn's group, where the turn key is the
 * SERVER id of the row that opened the turn and the range is the open run's
 * extent when it was last rendered: one entry per open run. Runs merge and
 * split as prose finishes; a run is open when its rows overlap a stored range,
 * and WorkLogGroup rewrites the ranges to the runs' new extents in one write
 * (see run-fold-ranges.ts). A key without `..` (stored before ranges) covers
 * its one row.
 * When the turn settles its runs become one fold: `promoteRuns` opens that
 * fold if the reader had opened any run, so settling never closes what they
 * were reading. An optimistic send has no server id until the
 * reducer reconciles it; until then its fold is keyed by the optimistic id and
 * `remap` moves it once the server id is known, so the open state follows the
 * turn through a reload.
 *
 * Only open folds are stored (closed is the default). Device-local: the cache
 * survives a reload and a session switch, and is never synced to the account
 * (it is reading position, not a preference). Capped so a long-lived browser
 * does not grow it without bound; the oldest entries go first.
 */
import { createPersistedStore, type PersistedSync } from '../persisted';

export const FOLD_STATE_LIMIT = 400;

type FoldRecord = Record<string, true>;

/** `local` stores never touch the sync engine. */
const NO_SYNC: PersistedSync = { register() {}, save() {} };

function normalize(raw: unknown): FoldRecord | undefined {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
	return Object.fromEntries(
		Object.entries(raw).filter(([, open]) => open === true)
	) as FoldRecord;
}

function capped(record: FoldRecord): FoldRecord {
	const keys = Object.keys(record);
	if (keys.length <= FOLD_STATE_LIMIT) return record;
	return Object.fromEntries(keys.slice(-FOLD_STATE_LIMIT).map((key) => [key, true])) as FoldRecord;
}

export function createFoldState(storage?: Storage) {
	const store = createPersistedStore<FoldRecord>(
		{ key: 'transcript_fold_open', ui: true, initial: {}, normalize, local: true },
		NO_SYNC,
		storage
	);

	function setOpen(key: string, open: boolean) {
		const current = store.current;
		if (!!current[key] === open) return;
		const next = { ...current };
		// Re-insert so the most recently touched fold is the last to be evicted.
		delete next[key];
		if (open) next[key] = true;
		store.set(capped(next));
	}

	/**
	 * Close the folds in `close` and open the folds in `open`, in one write;
	 * nothing is written when that changes nothing.
	 */
	function update(close: readonly string[], open: readonly string[]) {
		const current = store.current;
		const closing = close.filter((key) => current[key]);
		const opening = open.filter((key) => !current[key]);
		if (closing.length === 0 && opening.length === 0) return;
		const next = { ...current };
		for (const key of closing) delete next[key];
		for (const key of opening) next[key] = true;
		store.set(capped(next));
	}

	/**
	 * Move every fold of turn `from` onto turn `to` (optimistic id → server id).
	 * A fold already recorded under `to` wins; nothing changes when `from` has none.
	 */
	function remap(from: string, to: string) {
		if (!from || !to || from === to) return;
		const prefix = `${from}:`;
		const current = store.current;
		const moved = Object.keys(current).filter((key) => key.startsWith(prefix));
		if (moved.length === 0) return;
		const next: FoldRecord = {};
		for (const [key, open] of Object.entries(current)) {
			next[key.startsWith(prefix) ? `${to}:${key.slice(prefix.length)}` : key] = open;
		}
		store.set(next);
	}

	/**
	 * Settle turn `turn`: its live runs merge into the one turn fold. If any run
	 * was open, the turn fold opens (and is stored, so a reload keeps it); the
	 * run entries are dropped either way, since a settled turn has no runs.
	 */
	function promoteRuns(turn: string) {
		if (!turn) return;
		const prefix = `${turn}:run-`;
		const current = store.current;
		const runKeys = Object.keys(current).filter((key) => key.startsWith(prefix));
		if (runKeys.length === 0) return;
		const next: FoldRecord = { ...current };
		for (const key of runKeys) delete next[key];
		delete next[`${turn}:turn`];
		next[`${turn}:turn`] = true;
		store.set(capped(next));
	}

	return {
		subscribe: store.subscribe,
		get current(): Readonly<FoldRecord> { return store.current; },
		isOpen: (key: string) => store.current[key] === true,
		setOpen,
		update,
		remap,
		promoteRuns
	};
}

export type FoldState = ReturnType<typeof createFoldState>;

let storage: Storage | undefined;
try { storage = globalThis.localStorage; } catch { /* SSR or blocked storage. */ }
export const foldState = createFoldState(storage);
