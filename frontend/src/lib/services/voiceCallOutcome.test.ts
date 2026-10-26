import { afterEach, beforeEach, expect, it, vi } from 'vitest';
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(() => Promise.resolve({})), delete: vi.fn(() => Promise.resolve({})) } }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));
import { api } from '$lib/api/client';
import { notify, notifications } from '$lib/stores/notifications';
import { createCallOutcomeReporter } from './voiceCallOutcome';

beforeEach(() => {
	for (const n of notifications.current.live) notify.dismissLive(n.id);
	notify.clearAll();
	vi.clearAllMocks();
});
afterEach(() => notify.resetLocal());

const idle = { status: 'idle', sessionId: null, error: null, notice: null } as const;

// The reported bug: the call button created a conversation, the call failed to
// start, and the reason went only to the notification inbox. The user saw an
// empty conversation with no call and nothing saying why.
it('shows a failed call start on the page, not only in the inbox', () => {
	const report = createCallOutcomeReporter();
	report(idle);
	report({ status: 'connecting', sessionId: 'fresh', error: null, notice: null });
	report({ status: 'error', sessionId: 'fresh', error: 'Microphone access denied.', notice: null });

	const [banner] = notifications.current.live;
	expect(banner).toMatchObject({ surface: 'banner', severity: 'error', message: 'Microphone access denied.', session_id: 'fresh' });
	// Sticky until dismissed, and still recorded for the inbox.
	expect(banner.duration_ms).toBe(0);
	expect(notifications.current.history.map(n => n.id)).toContain(banner.id);
	expect(api.post).toHaveBeenCalledWith('/notifications', expect.objectContaining({ message: 'Microphone access denied.', surface: 'banner' }));
});

it('reports an ending notice as a warning banner', () => {
	const report = createCallOutcomeReporter();
	report({ status: 'ended', sessionId: 's', error: null, notice: 'This call reached its length limit.' });
	expect(notifications.current.live).toEqual([expect.objectContaining({ severity: 'warning', message: 'This call reached its length limit.' })]);
});

it('reports one ending once however often the store ticks, and stays quiet otherwise', () => {
	const report = createCallOutcomeReporter();
	const failed = { status: 'error', sessionId: 's', error: 'The voice provider rejected the connection.', notice: null } as const;
	report(failed);
	report({ ...failed });
	report({ ...failed });
	expect(notifications.current.live).toHaveLength(1);
	report({ status: 'live', sessionId: 's', error: null, notice: null });
	report({ status: 'ended', sessionId: 's', error: null, notice: null });
	expect(notifications.current.live).toHaveLength(1);
	// A later failure of the same kind is a new ending and is reported again.
	report(failed);
	expect(notifications.current.live).toHaveLength(2);
});
