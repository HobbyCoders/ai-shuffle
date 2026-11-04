/**
 * Pure half of the phone's chat controls (issue #1816): the quick bar under
 * the composer, the full "Chat controls" panel it pulls up, and the docked
 * slide-to-answer prompt. The components own the pointer listeners and the
 * rendering; the decisions live here so they can be tested without touch.
 */

/** The tool-access mode a swipe must never land on. */
export const BYPASS = 'bypassPermissions';

/** Movement before a press on the quick bar becomes a drag. Below it, a tap. */
export const DRAG_SLOP = 6;
/** Sideways travel on Effort or Access that counts as one step. */
export const STEP_DISTANCE = 28;
/** A flick this fast (px/ms) settles the panel whatever the distance. */
export const FLICK_VELOCITY = 0.4;
/** Past this share of its height, letting go settles the panel the drag's way. */
export const SETTLE_FRACTION = 0.3;
/** How far the knob must travel (share of its rail half) to answer. */
export const ANSWER_FRACTION = 0.8;

export type DragAxis = 'vertical' | 'horizontal';

/** Which axis a first real movement picks, or null while it is still a tap. */
export function dragAxis(dx: number, dy: number): DragAxis | null {
	if (Math.hypot(dx, dy) <= DRAG_SLOP) return null;
	return Math.abs(dy) >= Math.abs(dx) ? 'vertical' : 'horizontal';
}

/**
 * The value one sideways swipe on Effort or Access moves to. A swipe moves
 * one level (right raises, left lowers), stays inside the list, and never
 * lands on `guard` (Bypass) unless the chat is already there.
 */
export function stepValue(values: readonly string[], current: string | null, dx: number, guard?: string): string | null {
	if (!values.length || Math.abs(dx) < STEP_DISTANCE) return current;
	const start = current === null ? -1 : values.indexOf(current);
	const from = start === -1 ? (dx > 0 ? -1 : values.length) : start;
	const next = Math.min(values.length - 1, Math.max(0, from + (dx > 0 ? 1 : -1)));
	if (guard && values[next] === guard && current !== guard) return current;
	return values[next] ?? current;
}

/** Whether a drag that pulled the panel up should leave it open. */
export function settlesOpen(progress: number, velocity: number): boolean {
	if (velocity < -FLICK_VELOCITY) return true;
	if (velocity > FLICK_VELOCITY) return false;
	return progress > SETTLE_FRACTION;
}

/** Whether a drag that pulled the open panel down should close it. */
export function settlesClosed(offset: number, height: number, velocity: number): boolean {
	if (velocity > FLICK_VELOCITY) return true;
	if (velocity < -FLICK_VELOCITY) return false;
	return height > 0 && offset > height * SETTLE_FRACTION;
}

/** Position of the slide knob as a share of its travel, -1 (deny) to 1 (approve). */
export function knobFraction(dx: number, travel: number): number {
	if (travel <= 0) return 0;
	return Math.max(-1, Math.min(1, dx / travel));
}

export type SlideAnswer = 'approve' | 'deny';

/** What letting go of the knob at `fraction` answers, or null to spring back. */
export function slideAnswer(fraction: number): SlideAnswer | null {
	if (fraction >= ANSWER_FRACTION) return 'approve';
	if (fraction <= -ANSWER_FRACTION) return 'deny';
	return null;
}

/** Recent pointer samples, enough to read a release velocity. */
export class VelocityTracker {
	private samples: { at: number; pos: number }[] = [];

	add(pos: number, at = performance.now()): void {
		this.samples.push({ at, pos });
		while (this.samples.length > 2 && at - this.samples[0].at > 100) this.samples.shift();
	}

	/** px/ms over the last ~100ms; positive means down (or right). */
	velocity(): number {
		const s = this.samples;
		if (s.length < 2) return 0;
		const a = s[0];
		const b = s[s.length - 1];
		return (b.pos - a.pos) / Math.max(1, b.at - a.at);
	}
}

/** Short labels for the quick bar, where five cells share 390px. */
export const ACCESS_SHORT: Record<string, string> = {
	default: 'Ask',
	auto: 'Auto',
	acceptEdits: 'Edits',
	bypassPermissions: 'Bypass'
};

/** Plain names for the picker and the panel. */
export const ACCESS_LABEL: Record<string, string> = {
	default: 'Ask',
	auto: 'Auto',
	acceptEdits: 'Accept edits',
	bypassPermissions: 'Bypass'
};

/** Helper lines, five words or fewer, only where a mode needs one. */
export const ACCESS_HELP: Record<string, string> = {
	auto: 'Model checks each call',
	bypassPermissions: 'Runs every tool unasked'
};

/** "2 agents · 1 shell" for the running-tasks summary row. */
export function taskSummary(tasks: readonly { kind: string }[]): string {
	const agents = tasks.filter((t) => t.kind !== 'shell').length;
	const shell = tasks.length - agents;
	const parts: string[] = [];
	if (agents) parts.push(`${agents} agent${agents === 1 ? '' : 's'}`);
	if (shell) parts.push(`${shell} shell`);
	return parts.join(' · ');
}
