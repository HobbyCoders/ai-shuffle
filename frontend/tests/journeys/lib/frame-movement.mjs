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

/** How many frames after a composer step the transcript may still be catching up with it. */
const CATCH_UP_FRAMES = 3;

/**
 * Separate movements of a transcript row that nothing explains.
 * Frames are `{ row: top, content: scrollHeight, composer: top }`.
 *
 * Frames that change in an unbroken run are one burst, because the row, the
 * content height and the composer do not land on the same frame. A burst did
 * not move the transcript if the row's net shift is what the content added
 * below it (a reader kept at the bottom) plus what the composer gave back. The
 * rest counts: one for an unexplained shift, two for a bounce, which is travel
 * beyond the net shift. The transcript catching up with a composer step a few
 * frames later, and being scrolled back to the bottom, is not a bounce.
 */
export function transcriptMovements(frames, tolerance = STILL) {
	const measured = frames.filter((frame) => Number.isFinite(frame.row) && Number.isFinite(frame.composer));
	let count = 0;
	let burst = null;
	let lastComposer = { step: 0, at: -Infinity };
	const close = () => {
		if (!burst) return;
		const nearby = burst.start - lastComposer.at <= CATCH_UP_FRAMES ? lastComposer.step : 0;
		const allowance = Math.max(Math.abs(burst.composer), Math.abs(nearby));
		const explained = [burst.composer, nearby].some((step) => Math.abs(burst.net + burst.added - step) <= tolerance);
		if (burst.travel - Math.abs(burst.net) > 2 * (allowance + tolerance)) count += 2;
		else if (Math.abs(burst.net) > tolerance && !explained) count += 1;
		burst = null;
	};
	for (let i = 1; i < measured.length; i++) {
		const shift = measured[i].row - measured[i - 1].row;
		const added = measured[i].content - measured[i - 1].content;
		const composer = measured[i].composer - measured[i - 1].composer;
		if (Math.max(Math.abs(shift), Math.abs(added), Math.abs(composer)) <= tolerance) {
			close();
			continue;
		}
		burst ??= { start: i, net: 0, added: 0, composer: 0, travel: 0 };
		burst.net += shift;
		burst.added += added;
		burst.composer += composer;
		burst.travel += Math.abs(shift);
		if (Math.abs(composer) > tolerance) lastComposer = { step: composer, at: i };
	}
	close();
	return count;
}
