// Counting separate movements in per-frame positions. A movement is an unbroken
// run of frames changing in one direction: a still frame between two changes, or
// a change of direction, starts another one.

/** The 2px the journeys already accept as "did not move" (the rail's bound). */
const STILL = 2;

function countRuns(steps) {
	let count = 0;
	let direction = 0;
	for (const step of steps) {
		const next = Math.sign(step);
		if (next !== 0 && next !== direction) count++;
		direction = next;
	}
	return count;
}

const beyondStill = (shift, tolerance) => (Math.abs(shift) > tolerance ? shift : 0);

/** How many separate movements a list of positions contains. */
export function movements(positions, tolerance = STILL) {
	return countRuns(positions.slice(1).map((position, i) => beyondStill(position - positions[i], tolerance)));
}

/** How many frames the row may land after the composer or content that moved it. */
const CATCH_UP_FRAMES = 3;

/** How far a return may differ from a named correction's size and still be that correction. */
const CORRECTION_MARGIN = 1;

/** The row stopped here: the send began or ended, it held still, or it reversed. */
function stoppedAt(rows, i, tolerance) {
	if (i === 0 || i === rows.length - 1) return true;
	const into = beyondStill(rows[i] - rows[i - 1], tolerance);
	const out = beyondStill(rows[i + 1] - rows[i], tolerance);
	return Math.sign(into) * Math.sign(out) <= 0;
}

/**
 * Each bounce as `{ from, to }`. A bounce is the row reversing and stopping back
 * at a place it stopped at earlier: within the tolerance, or within a quarter of
 * how far it went. Going on past that place without stopping there is not one.
 * Each frame belongs to one bounce, so a place reached by one can start the next.
 */
function findReturns(rows, tolerance) {
	const returns = [];
	let since = 0;
	for (let at = 1; at < rows.length; at++) {
		if (!stoppedAt(rows, at, tolerance)) continue;
		let low = Infinity;
		let high = -Infinity;
		for (let from = at - 1; from >= since; from--) {
			const far = Math.max(high - rows[from], rows[from] - low, 0);
			const off = Math.abs(rows[at] - rows[from]);
			if (stoppedAt(rows, from, tolerance) && far - off > tolerance && off <= Math.max(tolerance, far / 4)) {
				returns.push({ from, to: at });
				since = at;
				break;
			}
			low = Math.min(low, rows[from]);
			high = Math.max(high, rows[from]);
		}
	}
	return returns;
}

/**
 * The frames with the named correction taken out of row, composer and content,
 * as if it never happened. Doing this first means forgiving it cannot change
 * what any bounce is measured from, or leave its height to explain a later step.
 * Only a correction that ends where it began is one: removing it then leaves
 * every other stop in place, because the step it flattens is too small to count.
 */
function withoutCorrection(frames, size, tolerance) {
	const matches = (change) => Math.abs(change - size) <= CORRECTION_MARGIN;
	for (const { from, to } of findReturns(frames.map((frame) => frame.row), tolerance)) {
		const path = frames.slice(from, to + 1).map((frame) => frame.row);
		const start = Math.max(0, from - CATCH_UP_FRAMES);
		const gave = frames[to].composer - frames[start].composer;
		const added = frames[to].content - frames[start].content;
		if (Math.abs(frames[to].row - frames[from].row) > tolerance) continue;
		if (!matches(Math.max(...path) - Math.min(...path)) || !matches(gave) || !matches(added)) continue;
		return frames.map((frame, i) => i <= start ? frame : {
			...frame,
			row: i > from && i < to ? frames[from].row : frame.row,
			composer: i < to ? frames[start].composer : frame.composer - gave,
			content: i < to ? frames[start].content : frame.content - added
		});
	}
	return frames;
}

/**
 * Spends the newest unspent changes that add up to `size`; false if none do. A row
 * following the bottom can stop short by the empty room `leftBelow` measured below
 * the reader on that frame, so that shortfall is the same change.
 */
function spend(pool, size, tolerance, leftBelow) {
	let sum = 0;
	for (let i = pool.length - 1; i >= 0; i--) {
		sum += pool[i].px;
		if (Math.abs(sum - size) <= tolerance || Math.abs(sum - leftBelow - size) <= tolerance) {
			pool.splice(i);
			return true;
		}
	}
	return false;
}

/**
 * Separate movements of a transcript row that nothing explains, over a whole send.
 * Frames are `{ row: top, content: scrollHeight, composer: top, gap: room below the reader }`.
 *
 * `knownCorrection` names the one exception, in pixels, once per send and only when
 * the journey passes it (desktop): the composer gives back that much, the row
 * follows it, and the bottom-follow code takes it back. It is removed first.
 *
 * A bounce is then read from the row's whole path, and any bounce is two movements
 * however each step is explained. With no bounce, a step only counts when nothing
 * accounts for it. A row moving down must match height the composer gave back, and
 * one moving up must match content added below it, each usable once, within the
 * few frames the row can land behind them.
 */
export function transcriptMovements(frames, { tolerance = STILL, knownCorrection = null } = {}) {
	const seen = frames.filter((frame) => Number.isFinite(frame.row) && Number.isFinite(frame.composer));
	if (seen.length < 2) return 0;
	const measured = knownCorrection === null ? seen : withoutCorrection(seen, knownCorrection, tolerance);
	const bounces = findReturns(measured.map((frame) => frame.row), tolerance).length;
	if (bounces > 0) return bounces * 2;
	const steps = [];
	const gaveBack = [];
	const added = [];
	for (let i = 1; i < measured.length; i++) {
		for (const pool of [gaveBack, added]) while (pool.length && i - pool[0].at > CATCH_UP_FRAMES) pool.shift();
		const composer = measured[i].composer - measured[i - 1].composer;
		const content = measured[i].content - measured[i - 1].content;
		if (composer > tolerance) gaveBack.push({ at: i, px: composer });
		if (content > tolerance) added.push({ at: i, px: content });
		const shift = beyondStill(measured[i].row - measured[i - 1].row, tolerance);
		// Moving down used up the room that was below the row; moving up may leave some.
		const leftBelow = Math.max(0, (shift > 0 ? measured[i - 1].gap : measured[i].gap) ?? 0);
		steps.push(shift !== 0 && spend(shift > 0 ? gaveBack : added, Math.abs(shift), tolerance, leftBelow) ? 0 : shift);
	}
	return countRuns(steps);
}

/**
 * Frames of a watched interval with no row or composer reading, including frames
 * that never reported at all. Each one is time the movement count did not see,
 * such as a row that was replaced or removed partway through.
 */
export function unmeasuredFrames(frames) {
	let missing = 0;
	frames.forEach((frame, i) => {
		if (!Number.isFinite(frame.row) || !Number.isFinite(frame.composer)) missing++;
		if (i > 0 && Number.isFinite(frame.seq)) missing += Math.max(0, frame.seq - frames[i - 1].seq - 1);
	});
	return missing;
}
