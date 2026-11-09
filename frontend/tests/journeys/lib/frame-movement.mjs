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

/** How many frames after a composer or content change the row may still be catching up. */
const CATCH_UP_FRAMES = 3;

/** How far the row may differ from a named correction's size and still be that correction. */
const CORRECTION_MARGIN = 1;

/** The recent changes whose expected row shifts add up to this one, or null. */
function matchCredits(credits, shift, tolerance) {
	for (let mask = 1; mask < 1 << credits.length; mask++) {
		const picked = credits.filter((_, bit) => mask & (1 << bit));
		if (Math.abs(picked.reduce((sum, credit) => sum + credit.shift, 0) - shift) <= tolerance) return picked;
	}
	return null;
}

/**
 * Separate movements of a transcript row that nothing explains.
 * Frames are `{ row: top, content: scrollHeight, composer: top }`.
 *
 * Each frame's row shift must be accounted for, to the pixel tolerance, by
 * changes of the last few frames that have not already been spent: content
 * added below it (a reader kept at the bottom moves up by that much) or height
 * the composer gave back (the row moves down by that much). The row, content
 * and composer do not land on the same frame, hence the short window. Whatever
 * is left over counts, however much content is arriving around it. A row that
 * goes back where it just was is a bounce, two movements, even when each half
 * has an explanation.
 *
 * `knownCorrection` names the one exception, in pixels: the composer gives back
 * that much, the row follows it, and the bottom-follow code takes it back. It
 * is forgiven once per send, and only when the journey passes it (desktop).
 */
export function transcriptMovements(frames, { tolerance = STILL, knownCorrection = null } = {}) {
	const measured = frames.filter((frame) => Number.isFinite(frame.row) && Number.isFinite(frame.composer));
	const steps = [];
	let credits = [];
	let last = null;
	let correctionUsed = false;
	for (let i = 1; i < measured.length; i++) {
		const shift = measured[i].row - measured[i - 1].row;
		const added = measured[i].content - measured[i - 1].content;
		const composer = measured[i].composer - measured[i - 1].composer;
		credits = credits.filter((credit) => i - credit.at <= CATCH_UP_FRAMES);
		if (Math.abs(added) > tolerance) credits.push({ at: i, shift: -added, kind: 'content' });
		if (Math.abs(composer) > tolerance) credits.push({ at: i, shift: composer, kind: 'composer' });
		if (Math.abs(shift) <= tolerance) {
			steps.push(0);
			continue;
		}
		const spent = matchCredits(credits, shift, tolerance);
		if (spent) credits = credits.filter((credit) => !spent.includes(credit));
		const kind = spent?.length === 1 ? spent[0].kind : null;
		const returned = last && i - last.at <= CATCH_UP_FRAMES && Math.abs(last.shift + shift) <= tolerance;
		if (returned) {
			const forgiven = knownCorrection !== null && !correctionUsed && last.kind === 'composer' && kind === 'content'
				&& Math.abs(Math.abs(shift) - knownCorrection) <= CORRECTION_MARGIN;
			correctionUsed ||= forgiven;
			steps[last.at - 1] = forgiven ? 0 : last.shift;
			steps.push(forgiven ? 0 : shift);
			last = null;
			continue;
		}
		steps.push(spent ? 0 : shift);
		last = { at: i, shift, kind };
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
