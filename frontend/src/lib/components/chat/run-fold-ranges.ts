/**
 * Open live runs as row ranges (transcript-organization spec §6).
 *
 * A live run the reader opened is stored as `<first row>..<last row>`: the
 * row it starts at and the row it ended at when it was last rendered. Runs
 * merge and split as prose finishes, so a run is open when its rows overlap
 * any stored range, which is decided from the store and the current runs
 * alone: no memory of earlier renders, so a merge or split that happened
 * while the group was unmounted, or before a reload, reads the same way.
 * A bare `<row>` (stored before ranges) is the range of that one row.
 *
 * `reconcileRanges` rewrites the stored ranges to the open runs' current
 * extents, so the store converges on one range per open run and only
 * changes when a run's extent changes.
 */

export const RANGE_SEP = '..';

/** A run's extent: its key (first row id) and positions in the turn. */
export interface RunSpan {
	key: string;
	/** Sequence position of the run's first row. */
	start: number;
	/** Sequence position of the run's last row. */
	end: number;
	/** Id of the run's last row. */
	through: string;
}

export interface Range { start: number; end: number }

export function rangeId(span: RunSpan): string {
	return `${span.key}${RANGE_SEP}${span.through}`;
}

/**
 * Positions a stored id covers in this turn, or null when it names no row
 * of this turn (another session's turn under the same key, a row that left
 * the window): such ids are never touched while the turn is live. A range
 * resolves by any endpoint that still names a row: an optimistic row acked
 * and then reloaded from server history keeps only its server id, so a
 * range stored under the optimistic id covers its surviving end's row, and
 * the next reconcile rewrites it to the run's real extent.
 */
export function parseRange(id: string, position: ReadonlyMap<string, number>): Range | null {
	const single = position.get(id);
	if (single !== undefined) return { start: single, end: single };
	let partial: Range | null = null;
	for (let at = id.indexOf(RANGE_SEP); at > 0; at = id.indexOf(RANGE_SEP, at + 1)) {
		const start = position.get(id.slice(0, at));
		const end = position.get(id.slice(at + RANGE_SEP.length));
		if (start !== undefined && end !== undefined) return end < start ? { start: end, end: start } : { start, end };
		const one = start ?? end;
		if (one !== undefined && !partial) partial = { start: one, end: one };
	}
	return partial;
}

const overlaps = (a: Range, b: Range) => a.start <= b.end && b.start <= a.end;

/** Keys of the runs whose rows overlap a stored open range. */
export function openRunKeys(
	ids: readonly string[],
	spans: readonly RunSpan[],
	position: ReadonlyMap<string, number>
): Set<string> {
	const ranges = ids.map((id) => parseRange(id, position)).filter((range): range is Range => range !== null);
	const open = new Set<string>();
	if (ranges.length === 0) return open;
	for (const span of spans) if (ranges.some((range) => overlaps(range, span))) open.add(span.key);
	return open;
}

/** Stored ids whose range overlaps `span` (closing a run drops them all). */
export function rangesOver(
	ids: readonly string[],
	span: Range,
	position: ReadonlyMap<string, number>
): string[] {
	return ids.filter((id) => {
		const range = parseRange(id, position);
		return range !== null && overlaps(range, span);
	});
}

/**
 * The writes that make the store hold exactly one range per open run, at
 * its current extent: a merged run's ranges become one, a split run's range
 * becomes one per part, a run whose end moved rewrites its range. A stored
 * range that overlaps no current run is left as it is (its work is in the
 * turn's current run, and settling reads it as an open log).
 */
export function reconcileRanges(
	ids: readonly string[],
	spans: readonly RunSpan[],
	position: ReadonlyMap<string, number>
): { add: string[]; remove: string[] } {
	const open = openRunKeys(ids, spans, position);
	const wanted = new Set(spans.filter((span) => open.has(span.key)).map(rangeId));
	const have = new Set(ids);
	const add = [...wanted].filter((id) => !have.has(id));
	const remove = ids.filter((id) => {
		if (wanted.has(id)) return false;
		const range = parseRange(id, position);
		return range !== null && spans.some((span) => overlaps(range, span));
	});
	return { add, remove };
}
