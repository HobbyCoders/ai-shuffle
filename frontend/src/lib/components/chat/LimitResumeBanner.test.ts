/**
 * The composer's usage-limit banner (#1831): reset time, "Resume at reset"
 * for this chat once, then "Resumes at" with Cancel. The worker's state
 * decides, so a fresh mount (a reload) shows what the worker has armed.
 */
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SessionLimitResume } from '$lib/api/limit-resume';

const mocks = vi.hoisted(() => ({ get: vi.fn(), arm: vi.fn(), cancel: vi.fn(), notify: vi.fn() }));
vi.mock('$lib/api/limit-resume', () => ({
	getSessionLimitResume: mocks.get,
	armLimitResume: mocks.arm,
	cancelLimitResume: mocks.cancel
}));
vi.mock('$lib/stores/notifications', () => ({ notify: mocks.notify }));
import LimitResumeBanner from './LimitResumeBanner.svelte';

const RESET = Date.now() / 1000 + 3 * 3600;
const STOPPED: SessionLimitResume = {
	session_id: 's1', pending: null, workers: [], armed: false, resume_at: null,
	limit_stopped: { turn_id: 't1', provider: 'claude', reset_at: RESET, reset_known: true, can_arm: true, reason: null }
};
const ARMED: SessionLimitResume = {
	...STOPPED, armed: true, resume_at: RESET + 30, pending: { fire_at: RESET + 30 },
	limit_stopped: { ...STOPPED.limit_stopped!, can_arm: false }
};

beforeEach(() => {
	vi.clearAllMocks();
	mocks.cancel.mockResolvedValue({ session_id: 's1', task_id: null, cancelled: true });
});
afterEach(cleanup);

describe('LimitResumeBanner', () => {
	it('shows the reset time and arms this chat once', async () => {
		mocks.get.mockResolvedValue(STOPPED);
		mocks.arm.mockResolvedValue(ARMED);
		render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		expect(await screen.findByText(/^Usage limit reached\. Resets at /)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Resume at reset' }));
		expect(mocks.arm).toHaveBeenCalledWith('s1', 't1');
		expect(await screen.findByText(/^Usage limit reached\. Resumes at /)).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Resume at reset' })).toBeNull();
	});

	it('comes back armed after a reload, and Cancel cancels it', async () => {
		mocks.get.mockResolvedValueOnce(ARMED).mockResolvedValue({
			...STOPPED, limit_stopped: { ...STOPPED.limit_stopped!, can_arm: false, reason: 'already_handled' }
		});
		render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		await fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
		expect(mocks.cancel).toHaveBeenCalledWith('s1');
		expect(await screen.findByText(/Resets at/)).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Resume at reset' })).toBeNull();
	});

	it('says the reset time is unknown without an arm button', async () => {
		mocks.get.mockResolvedValue({
			...STOPPED,
			limit_stopped: { ...STOPPED.limit_stopped!, reset_at: null, reset_known: false, can_arm: false, reason: 'reset_unknown' }
		});
		render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		expect(await screen.findByText('Usage limit reached. Reset time unknown')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Resume at reset' })).toBeNull();
	});

	it('shows a refused arm and reads the state again', async () => {
		mocks.get.mockResolvedValue(STOPPED);
		mocks.arm.mockRejectedValue(new Error('This limit was already handled. Retry manually.'));
		render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		await fireEvent.click(await screen.findByRole('button', { name: 'Resume at reset' }));
		await waitFor(() => expect(mocks.notify).toHaveBeenCalled());
		expect(mocks.notify.mock.calls[0][1]).toEqual({ severity: 'error' });
		expect(mocks.get).toHaveBeenCalledTimes(2);
	});

	it('gives a non-owner the times but no actions', async () => {
		mocks.get.mockResolvedValue(ARMED);
		render(LimitResumeBanner, { sessionId: 's1', messages: [], canAct: false });
		expect(await screen.findByText(/Resumes at/)).toBeTruthy();
		expect(screen.queryByRole('button')).toBeNull();
	});

	it('can be dismissed while unarmed', async () => {
		mocks.get.mockResolvedValue(STOPPED);
		render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		await fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByText(/Usage limit reached/)).toBeNull();
	});

	it("falls back to the transcript's pause when the worker cannot answer", async () => {
		mocks.get.mockRejectedValue(new Error('404'));
		const row = { id: 'r1', role: 'system' as const, type: 'system' as const, content: '',
			systemSubtype: 'limit_resume', systemData: { status: 'scheduled', resume_at: RESET } };
		render(LimitResumeBanner, { sessionId: 's1', messages: [row] });
		expect(await screen.findByText(/Resumes at/)).toBeTruthy();
	});

	it('keeps the arm answer over an older read that lands after it', async () => {
		mocks.get.mockResolvedValueOnce(STOPPED);
		const view = render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		await screen.findByRole('button', { name: 'Resume at reset' });
		let finishStale!: (v: SessionLimitResume) => void;
		mocks.get.mockReturnValueOnce(new Promise((r) => (finishStale = r)));
		const row = { id: 'r9', role: 'system' as const, type: 'system' as const, content: '',
			systemSubtype: 'limit_resume', systemData: { status: 'available', reset_at: RESET } };
		await view.rerender({ sessionId: 's1', messages: [row] });
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		mocks.arm.mockResolvedValue(ARMED);
		await fireEvent.click(screen.getByRole('button', { name: 'Resume at reset' }));
		expect(await screen.findByText(/Resumes at/)).toBeTruthy();
		finishStale(STOPPED);
		await new Promise((r) => setTimeout(r, 20));
		expect(screen.getByText(/Resumes at/)).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Resume at reset' })).toBeNull();
	});

	it('keeps the cancel over an older armed read that lands after it', async () => {
		mocks.get.mockResolvedValueOnce(ARMED);
		const view = render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		await screen.findByRole('button', { name: 'Cancel' });
		let finishStale!: (v: SessionLimitResume) => void;
		mocks.get.mockReturnValueOnce(new Promise((r) => (finishStale = r)));
		const row = { id: 'r10', role: 'system' as const, type: 'system' as const, content: '',
			systemSubtype: 'limit_resume', systemData: { status: 'scheduled', resume_at: RESET + 30 } };
		await view.rerender({ sessionId: 's1', messages: [row] });
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
		mocks.get.mockResolvedValue({
			...STOPPED, limit_stopped: { ...STOPPED.limit_stopped!, can_arm: false, reason: 'already_handled' }
		});
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(await screen.findByText(/Resets at/)).toBeTruthy();
		finishStale({ ...ARMED, resume_at: RESET + 90, pending: { fire_at: RESET + 90 } });
		await new Promise((r) => setTimeout(r, 20));
		expect(screen.getByText(/Resets at/)).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
	});

	it('does not ask again while nothing that matters changed', async () => {
		mocks.get.mockResolvedValue(STOPPED);
		const view = render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		await screen.findByRole('button', { name: 'Resume at reset' });
		const reply = { id: 'a1', role: 'assistant' as const, type: 'text' as const, content: 'streamed text' };
		await view.rerender({ sessionId: 's1', messages: [reply] });
		await view.rerender({ sessionId: 's1', messages: [{ ...reply, content: 'more streamed text' }] });
		await new Promise((r) => setTimeout(r, 400));
		expect(mocks.get).toHaveBeenCalledTimes(1);
	});

	it('reads the state again when a new limit row arrives', async () => {
		mocks.get.mockResolvedValue({ ...STOPPED, limit_stopped: null });
		const view = render(LimitResumeBanner, { sessionId: 's1', messages: [] });
		await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(1));
		mocks.get.mockResolvedValue(STOPPED);
		const row = { id: 'r2', role: 'system' as const, type: 'system' as const, content: '',
			systemSubtype: 'limit_resume', systemData: { status: 'available', reset_at: RESET } };
		await view.rerender({ sessionId: 's1', messages: [row] });
		expect(await screen.findByRole('button', { name: 'Resume at reset' })).toBeTruthy();
		expect(mocks.get).toHaveBeenCalledTimes(2);
	});
});
