/**
 * Ordering for a chat's effort level (#1609).
 *
 * The session row counts the changes of its effort (`effort_rev`). The level
 * reaches a client from several places that can arrive out of order: the row
 * it opened, `/live`, a `session_updated` push and the answer to its own pick.
 * Each one names the revision its level belongs to, and a client adopts a
 * level only when that revision is newer than the one it holds. It sends the
 * revision it holds with each message, so the worker can tell a level this
 * client was never told is outdated.
 */

/** A revision as the server sends it, or undefined (a server too old to send one). */
export function effortRevOf(value: unknown): number | undefined {
	return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : undefined;
}

/**
 * Whether a level told with revision `rev` is news to a holder of `held`.
 *
 * A level with no revision comes from a server that has none; it is adopted
 * as it always was. A holder that knows no revision adopts any.
 */
export function effortIsNewer(rev: number | undefined, held: number | undefined): boolean {
	if (rev === undefined || held === undefined) return true;
	return rev > held;
}

/** The level and the revision it was last acknowledged at, as a client holds them. */
type HeldEffort = { effortLevelOverride: string | null; effortRev?: number };
/** The same pair as a session row carries it. */
type RowEffort = { effort?: string | null; effort_rev?: number };

/**
 * The level and revision a chat opens with: the copy cached when this client
 * left it, or the session row when the row's revision is newer (a pick made
 * while the chat was not open here).
 *
 * A cached copy with a revision is ordered by it, Auto included: a cached
 * Auto at a newer revision than the row is a choice, not missing state. A
 * cached copy with no revision (cached before revisions, or by a page that
 * never learned one) loses to a row that names one, since it cannot show it
 * is newer. Only when neither has a revision does the old rule hold: a cached
 * level wins, a cached Auto falls back to the row.
 */
export function restoredEffort(cached: HeldEffort | null | undefined, row: RowEffort | undefined): HeldEffort {
	const rowRev = effortRevOf(row?.effort_rev);
	const fromRow = { effortLevelOverride: row?.effort ?? null, effortRev: rowRev };
	if (!cached) return fromRow;
	const cachedRev = effortRevOf(cached.effortRev);
	const held = { effortLevelOverride: cached.effortLevelOverride ?? null, effortRev: cachedRev };
	if (cachedRev !== undefined) return rowRev !== undefined && rowRev > cachedRev ? fromRow : held;
	if (rowRev !== undefined || held.effortLevelOverride == null) return fromRow;
	return held;
}

/**
 * The level a refreshed session row brings to the chat that is open, or null
 * when the row is no news.
 *
 * A level saved on the worker that runs the chat reaches a page on another
 * worker only as the replicated row, and a page that missed a push (a socket
 * gap) only as the reloaded row. The row list is a reload and can be older
 * than what the page holds, so only a revision above the held one counts. A
 * new compose starts at 0; an unversioned restored level loses to any revision.
 */
export function effortFromRow(state: HeldEffort, row: RowEffort | undefined): HeldEffort | null {
	const rowRev = effortRevOf(row?.effort_rev);
	if (!row || rowRev === undefined || (state.effortRev !== undefined && rowRev <= state.effortRev)) return null;
	return { effortLevelOverride: row.effort ?? null, effortRev: rowRev };
}

/**
 * The revision a send carries, or undefined to send none (the worker then
 * runs the level, but saves it only on a row whose effort never changed).
 *
 * A chat created in this tab was never told a revision. Its sidebar row names
 * one, and that is the composer's as long as the row names the same level.
 */
export function effortRevToSend(state: HeldEffort, row: RowEffort | undefined): number | undefined {
	if (state.effortRev !== undefined) return state.effortRev;
	const rowRev = effortRevOf(row?.effort_rev);
	if (rowRev === undefined) return undefined;
	return (row?.effort ?? null) === (state.effortLevelOverride ?? null) ? rowRev : undefined;
}
