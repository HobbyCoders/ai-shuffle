import type { Readable } from 'svelte/store';
import { get } from 'svelte/store';
import { liveBanners, notify } from '$lib/stores/notifications';
import type { VoiceCallStatus, VoiceCallStoreState } from './voiceCall';
import { APP_UPDATED_MESSAGE, CALL_LOAD_FAILED_MESSAGE, reportChunkLoadFailure } from './app-update';

type CallOutcome = Pick<VoiceCallStoreState, 'status' | 'sessionId' | 'error' | 'notice'>;

/** A call is up (or coming up): the last outcome no longer applies. */
const CALL_UP: readonly VoiceCallStatus[] = ['connecting', 'live', 'reconnecting'];

/**
 * The one banner that explains how the last call ended. There is only ever
 * one: a new outcome replaces it and a call that comes up removes it, so a
 * retry never stacks banners and a call that works leaves none behind
 * (the same pattern as `explainBlockedCall`).
 */
let outcomeBanner: string | null = null;

function clearOutcome(): void {
	if (outcomeBanner) notify.dismissLive(outcomeBanner);
	outcomeBanner = null;
}

function showOutcome(message: string, severity: 'error' | 'warning', sessionId: string | null): void {
	clearOutcome();
	// The call's code would not load: the only way forward is a reload, so
	// the banner offers it. Category 'app-update' makes it the one reload
	// banner on screen: the update watcher's own gives way to it.
	const stale = message === CALL_LOAD_FAILED_MESSAGE || message === APP_UPDATED_MESSAGE;
	if (stale) for (const banner of get(liveBanners)) if (banner.category === 'app-update') notify.dismissLive(banner.id);
	outcomeBanner = notify.banner(message, {
		severity: stale ? 'warning' : severity,
		category: stale ? 'app-update' : undefined,
		persist: !stale,
		session_id: sessionId ?? undefined,
		action: stale ? { label: 'Reload', onClick: () => window.location.reload() } : undefined,
	});
}

/**
 * Reports how a call ended when it ends by itself (a failed start, a transport
 * error, the length cap). The call controls that showed the reason are gone by
 * then, so the reason has to appear where the user is looking.
 *
 * It is a page banner, never only a bell item. A call started from the
 * new-session composer creates its conversation first, so a start that fails
 * (microphone, provider, credential) leaves the user on that empty
 * conversation. With the reason only in the notification inbox, nothing on
 * screen said why no call opened. The banner stays until dismissed and is
 * kept in the inbox as well.
 *
 * Keyed on the message, so the same ending is reported once however often the
 * store ticks (level updates, captions). Any status that means a call is up
 * resets the key, so the next ending is reported even when it says the same.
 *
 * Use `watchCallOutcomes` in the app: it holds one reporter for the page, so
 * a component that remounts never reports an ending a second time.
 */
export function createCallOutcomeReporter(): (call: CallOutcome) => void {
	let last: string | null = null;
	return (call) => {
		if (CALL_UP.includes(call.status)) {
			last = null;
			clearOutcome();
			return;
		}
		// `error` on any terminal status: a start refused before the call got
		// going can leave the controller idle with the reason set.
		const message = call.error ?? (call.status === 'ended' ? call.notice : null);
		const key = message ? `${call.status}:${call.sessionId}:${message}` : null;
		if (key === last) return;
		last = key;
		if (!message) return;
		showOutcome(message, call.error ? 'error' : 'warning', call.sessionId);
	};
}

let watched: Readable<CallOutcome> | null = null;

/**
 * Report call outcomes for the life of the page, from one subscription.
 *
 * Views that show calls mount and unmount (opening Settings, switching
 * panels), and the call store keeps its last ending until the next call. A
 * reporter per view would re-report that ending on every remount: the banner
 * the user dismissed would come back, with a new inbox row each time. Calling
 * this again is a no-op.
 */
export function watchCallOutcomes(source: Readable<CallOutcome>): void {
	if (watched === source) return;
	if (watched) throw new Error('Call outcomes are already watched on another store.');
	watched = source;
	source.subscribe(createCallOutcomeReporter());
}

/**
 * A start that failed before the call controller took it over (the call
 * button's own request threw). Shares the outcome banner, so it replaces the
 * previous outcome and the next call clears it.
 */
export function reportCallStartFailure(error: unknown, sessionId: string | null): void {
	const detail = (error as { detail?: unknown } | null)?.detail;
	// The call's code would not load (app-update.ts checks whether it was an update).
	const message = reportChunkLoadFailure(error)
		? CALL_LOAD_FAILED_MESSAGE
		: typeof detail === 'string' && detail
		? detail
		: error instanceof Error && error.message
			? error.message
			: 'Could not start the call.';
	showOutcome(message, 'error', sessionId);
}
