/**
 * A failed first load on a preference page (Security, Agent instructions): the row's
 * one-line hint, the full sentence for a screen reader, and where focus goes
 * when the row's Try again button disappears.
 */
import { tick } from 'svelte';

/** A failed load's one-line hint: the HTTP status, or that the worker did not answer. */
export function couldNotLoad(code: number | null | undefined): string {
	return code ? `Could not load (error ${code})` : 'Could not load (no answer)';
}

/** The same failure in full, for a screen reader's alert. */
export function couldNotLoadSentence(what: string, code: number | null | undefined): string {
	return `Could not load ${what}. ${code ? `The worker answered with an error (status ${code}).` : 'The worker did not answer.'}`;
}

/**
 * Call from an `$effect.pre` that reads the page's load states. A Try again
 * goes away when its row loads or is refused. If it has focus then, focus
 * would fall to the page body: move it to that row's value, else another
 * button in the row, else a Try again still on the page. Runs before the
 * rows update; focus anywhere else is never moved.
 */
export function handOffRetryFocus(): void {
	const button = document.activeElement;
	if (!(button instanceof HTMLElement) || !button.matches('[data-retry-load]')) return;
	const row = button.closest('.st-row');
	const page = button.closest('.st-page');
	void tick().then(() => {
		if (button.isConnected || (document.activeElement && document.activeElement !== document.body)) return;
		(
			row?.querySelector<HTMLElement>('.st-vbtn:not(:disabled)') ??
			row?.querySelector<HTMLElement>('button:not(:disabled)') ??
			page?.querySelector<HTMLElement>('[data-retry-load]')
		)?.focus();
	});
}
