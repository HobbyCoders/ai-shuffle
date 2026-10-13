/** Local-only, bounded User Timing samples. No prompts, URLs or session IDs leave
 * this module. A double rAF ends after a rendering opportunity, not at fetch end.
 * These are lab diagnostics, not field analytics or proof of compositor paint.
 */
export type Journey = 'app.bootstrap' | 'chat.open';
type Pending = { key: string; start: number; hidden: boolean; onVisibility: () => void };
const pending = new Map<Journey, Pending>();
const slots = new Map<Journey, number>();
export const JOURNEY_SAMPLE_LIMIT = 32;
let navigationOriginClaimed = false;

/** Navigation start (0) is a fair origin only for the document's first journey
 * and only before any user activation: after the login form, or on a remount
 * after re-login, it would include the user's own time. Null means skip. */
export function claimNavigationOrigin(): number | null {
	if (navigationOriginClaimed || typeof navigator === 'undefined') return null;
	navigationOriginClaimed = true;
	return navigator.userActivation?.hasBeenActive ? null : 0;
}

export function beginJourney(journey: Journey, key: string, start?: number): void {
	if (typeof document === 'undefined' || typeof performance === 'undefined') return;
	const previous = pending.get(journey);
	if (previous) cancelJourney(journey, previous.key);
	const sample: Pending = { key, start: start ?? performance.now(), hidden: document.hidden,
		onVisibility: () => { if (document.hidden) sample.hidden = true; } };
	pending.set(journey, sample);
	document.addEventListener('visibilitychange', sample.onVisibility);
}

/** Discard superseded/hidden samples instead of reporting them as fast loads. */
export function cancelJourney(journey: Journey, key: string): void {
	const sample = pending.get(journey);
	if (sample?.key !== key) return;
	document.removeEventListener('visibilitychange', sample.onVisibility);
	pending.delete(journey);
}

export function finishJourneyAfterPaint(journey: Journey, key: string): () => void {
	const sample = pending.get(journey);
	if (!sample || sample.key !== key || typeof requestAnimationFrame === 'undefined') return () => {};
	let first = 0, second = 0;
	// Visibility has been tracked since begin, including time waiting for data.
	const cleanup = () => {
		cancelAnimationFrame(first);
		cancelAnimationFrame(second);
	};
	first = requestAnimationFrame(() => {
		second = requestAnimationFrame(() => {
			cleanup();
			if (pending.get(journey) !== sample) return;
			cancelJourney(journey, key);
			if (sample.hidden || document.hidden) return;
			const slot = slots.get(journey) ?? 0;
			const name = `shuffle:${journey}:${slot}`;
			// Fixed slots bound the browser buffer even across thousands of opens.
			try {
				performance.clearMeasures(name);
				performance.measure(name, { start: sample.start, end: performance.now(), detail: { journey } });
				slots.set(journey, (slot + 1) % JOURNEY_SAMPLE_LIMIT);
			} catch { /* Older webviews must never lose navigation to diagnostics. */ }
		});
	});
	return cleanup;
}
