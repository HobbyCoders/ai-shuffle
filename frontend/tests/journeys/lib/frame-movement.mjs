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

/**
 * Every time the row left a position and later came back to it, however many
 * frames that took: `{ from, to }`, the last frame at the position and the frame
 * back at it. Passing a position on the way somewhere else is not a return.
 */
function findReturns(rows, tolerance) {
	const returns = [];
	let visited = [{ top: rows[0], at: 0 }];
	for (let i = 1; i < rows.length; i++) {
		const back = visited.findIndex((place, index) => index < visited.length - 1 && Math.abs(place.top - rows[i]) <= tolerance);
		if (back !== -1) {
			returns.push({ from: visited[back].at, to: i });
			visited = visited.slice(0, back + 1);
			visited[back].at = i;
		} else if (Math.abs(rows[i] - visited[visited.length - 1].top) > tolerance) {
			visited.push({ top: rows[i], at: i });
		} else {
			visited[visited.length - 1].at = i;
		}
	}
	return returns;
}

/** Spends the newest unspent changes that add up to `size`; false if none do. */
function spend(pool, size, tolerance) {
	let sum = 0;
	for (let i = pool.length - 1; i >= 0; i--) {
		sum += pool[i].px;
		if (Math.abs(sum - size) <= tolerance) {
			pool.splice(i);
			return true;
		}
	}
	return false;
}

/**
 * Separate movements of a transcript row that nothing explains, over a whole send.
 * Frames are `{ row: top, content: scrollHeight, composer: top }`.
 *
 * A bounce is a property of the row's whole path, so it is read from the path:
 * a row that left a position and came back to it moved twice, whatever explains
 * each step. `knownCorrection` names the one exception, in pixels, once per send
 * and only when the journey passes it (desktop): the composer gives back that
 * much, the row follows it, and the bottom-follow code takes it back.
 *
 * Off those excursions a step only counts when nothing accounts for it. A row
 * moving down must match height the composer gave back, and one moving up must
 * match content added below it, each usable once, within the few frames the row
 * can land behind them.
 */
export function transcriptMovements(frames, { tolerance = STILL, knownCorrection = null } = {}) {
	const measured = frames.filter((frame) => Number.isFinite(frame.row) && Number.isFinite(frame.composer));
	if (measured.length < 2) return 0;
	const returns = findReturns(measured.map((frame) => frame.row), tolerance);
	const inReturn = new Set();
	let bounces = 0;
	let correctionUsed = false;
	for (const { from, to } of returns) {
		for (let i = from + 1; i <= to; i++) inReturn.add(i);
		const before = measured[Math.max(0, from - CATCH_UP_FRAMES)];
		const excursion = measured.slice(from, to + 1).map((frame) => frame.row);
		const matches = (change) => Math.abs(change - knownCorrection) <= CORRECTION_MARGIN;
		const forgiven = knownCorrection !== null && !correctionUsed
			&& matches(Math.max(...excursion) - Math.min(...excursion))
			&& matches(measured[to].composer - before.composer)
			&& matches(measured[to].content - before.content);
		correctionUsed ||= forgiven;
		if (!forgiven) bounces += 2;
	}
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
		if (inReturn.has(i)) steps.push(0);
		else steps.push(shift !== 0 && spend(shift > 0 ? gaveBack : added, Math.abs(shift), tolerance) ? 0 : shift);
	}
	return bounces + countRuns(steps);
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
