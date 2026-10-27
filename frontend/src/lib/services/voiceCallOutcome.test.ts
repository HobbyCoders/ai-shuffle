import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(() => Promise.resolve({})), delete: vi.fn(() => Promise.resolve({})) } }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));
import { api } from '$lib/api/client';
import { notify, notifications } from '$lib/stores/notifications';
import { createCallOutcomeReporter, reportCallStartFailure, watchCallOutcomes } from './voiceCallOutcome';
import { APP_UPDATED_MESSAGE } from './app-update';

beforeEach(() => {
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	notify.clearAll();
	vi.clearAllMocks();
});
afterEach(() => notify.resetLocal());

type Call = Parameters<ReturnType<typeof createCallOutcomeReporter>>[0];
const idle: Call = { status: 'idle', sessionId: null, error: null, notice: null };
const connecting = (sessionId: string): Call => ({ status: 'connecting', sessionId, error: null, notice: null });
const failed = (sessionId: string, error = 'Microphone access denied.'): Call => ({ status: 'error', sessionId, error, notice: null });
const live = () => notifications.current.live;

// The reported bug: the call button created a conversation, the call failed to
// start, and the reason went only to the notification inbox. The user saw an
// empty conversation with no call and nothing saying why.
it('shows a failed call start on the page, not only in the inbox', () => {
	const report = createCallOutcomeReporter();
	report(idle);
	report(connecting('fresh'));
	report(failed('fresh'));

	const [banner] = live();
	expect(banner).toMatchObject({ surface: 'banner', severity: 'error', message: 'Microphone access denied.', session_id: 'fresh' });
	// Sticky until dismissed, and still recorded for the inbox.
	expect(banner.duration_ms).toBe(0);
	expect(notifications.current.history.map(n => n.id)).toContain(banner.id);
	expect(api.post).toHaveBeenCalledWith('/notifications', expect.objectContaining({ message: 'Microphone access denied.', surface: 'banner' }));
});

it('reports an ending notice as a warning banner', () => {
	const report = createCallOutcomeReporter();
	report({ status: 'ended', sessionId: 's', error: null, notice: 'This call reached its length limit.' });
	expect(live()).toEqual([expect.objectContaining({ severity: 'warning', message: 'This call reached its length limit.' })]);
});

// startCallForSession leaves the controller idle, with a reason, when the
// voice bridge hands back no credential.
it('reports a reason left on an idle controller', () => {
	const report = createCallOutcomeReporter();
	report(connecting('s'));
	report({ status: 'idle', sessionId: 's', error: 'Call backend not available yet.', notice: null });
	expect(live()).toEqual([expect.objectContaining({ severity: 'error', message: 'Call backend not available yet.' })]);
});

it('reports one ending once however often the store ticks', () => {
	const report = createCallOutcomeReporter();
	report(failed('s'));
	report({ ...failed('s') });
	report({ ...failed('s') });
	expect(live()).toHaveLength(1);
	expect(api.post).toHaveBeenCalledTimes(1);
});

it('a retry replaces the previous outcome instead of stacking banners', () => {
	const report = createCallOutcomeReporter();
	report(connecting('s'));
	report(failed('s'));
	const first = live()[0].id;
	// The retry clears the old reason while it connects…
	report(connecting('s'));
	expect(live()).toEqual([]);
	// …and its own failure, even with the same message, is reported once more.
	report(failed('s'));
	expect(live()).toHaveLength(1);
	expect(live()[0].id).not.toBe(first);
	// Both endings stay in the inbox.
	expect(notifications.current.history).toHaveLength(2);
});

it('a call that comes up leaves no banner from the failed attempt', () => {
	const report = createCallOutcomeReporter();
	report(failed('s'));
	report(connecting('s'));
	report({ status: 'live', sessionId: 's', error: null, notice: null });
	expect(live()).toEqual([]);
	// Hanging up normally says nothing.
	report({ status: 'ended', sessionId: 's', error: null, notice: null });
	expect(live()).toEqual([]);
});

it('a call-button failure shares the banner and the next call clears it', () => {
	const report = createCallOutcomeReporter();
	report(failed('s', 'The voice provider rejected the connection.'));
	reportCallStartFailure(new Error('End the current voice call before starting another.'), 's');
	expect(live()).toEqual([expect.objectContaining({ severity: 'error', message: 'End the current voice call before starting another.' })]);
	report(connecting('s'));
	expect(live()).toEqual([]);
});

// #707: the worker updated under the page and the call's code was gone. The
// browser's import error is meaningless to the user; the way forward is a reload.
it('a start that failed on a missing chunk says the app was updated, with a Reload', () => {
	const reload = vi.fn();
	vi.stubGlobal('location', { ...window.location, reload });
	try {
		reportCallStartFailure(new TypeError('Failed to fetch dynamically imported module: https://w.example/_app/immutable/chunks/Old.js'), 'fresh');
		const [banner] = live();
		expect(banner).toMatchObject({ severity: 'warning', message: APP_UPDATED_MESSAGE, session_id: 'fresh', action: { label: 'Reload' } });
		// Nothing for the inbox: after the reload the reason no longer applies.
		expect(api.post).not.toHaveBeenCalled();
		banner.action!.onClick();
		expect(reload).toHaveBeenCalledTimes(1);
	} finally {
		vi.unstubAllGlobals();
	}
});

it('an ending carrying the app-updated reason gets the Reload too', () => {
	const report = createCallOutcomeReporter();
	report(connecting('s'));
	report(failed('s', APP_UPDATED_MESSAGE));
	expect(live()).toEqual([expect.objectContaining({ message: APP_UPDATED_MESSAGE, action: expect.objectContaining({ label: 'Reload' }) })]);
});

// The reviewed bug: every ChatView built its own reporter, and the call store
// keeps the last ending until the next call. Opening Settings and coming back
// remounted the view, brought back the dismissed banner, added an inbox row
// and posted it again.
it('a view remounting does not bring back a dismissed ending', () => {
	const store = writable<Call>(idle);
	watchCallOutcomes(store); // first mount
	store.set(connecting('s'));
	store.set(failed('s'));
	expect(live()).toHaveLength(1);
	notify.dismissLive(live()[0].id);

	watchCallOutcomes(store); // remount after visiting Settings
	watchCallOutcomes(store); // the second ChatView instance
	store.update(s => ({ ...s })); // unrelated store churn
	expect(live()).toEqual([]);
	expect(notifications.current.history).toHaveLength(1);
	expect(api.post).toHaveBeenCalledTimes(1);

	// The next call is still watched.
	store.set(connecting('s'));
	store.set(failed('s', 'The voice connection dropped.'));
	expect(live()).toEqual([expect.objectContaining({ message: 'The voice connection dropped.' })]);
});
