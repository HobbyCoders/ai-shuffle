import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put, post: mocks.post },
	getErrorMessage: (cause: { detail?: string; message?: string }, fallback: string) => cause?.detail ?? cause?.message ?? fallback
}));
import LoopGuardPanel from './LoopGuardPanel.svelte';

const defaults = { enabled: true, session_turns_per_hour: 12, install_turns_per_hour: 60, worker_max_minutes: 60 };
const status = (overrides: Record<string, unknown> = {}) => ({
	...defaults,
	defaults,
	applies: true,
	unattended_sources: ['automation', 'wakeup', 'provider_agent', 'connector'],
	usage: { install_last_hour: 3 },
	held: [],
	...overrides
});

beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

it('shows whether it applies and the documented defaults', async () => {
	mocks.get.mockResolvedValue(status());
	render(LoopGuardPanel, { isAdmin: true });
	expect((await screen.findByTestId('loop-guard-applies')).textContent).toContain('Unattended turns this hour: 3 of 60');
	expect(screen.getByText('Default 12')).toBeTruthy();
	expect(screen.getByText('Default 60; 0 turns it off')).toBeTruthy();
});

it('says it does nothing in API key mode', async () => {
	mocks.get.mockResolvedValue(status({ applies: false }));
	render(LoopGuardPanel, { isAdmin: true });
	expect((await screen.findByTestId('loop-guard-applies')).textContent).toContain('Not active in API key mode');
});

it('confirms a held turn', async () => {
	mocks.get.mockResolvedValueOnce(status({
		held: [{ id: 'h1', session_id: 's1', turn_id: 't1', source: 'automation', prompt: 'nightly report', reason: 'x', held_at: 0 }]
	}));
	mocks.get.mockResolvedValue(status());
	mocks.post.mockResolvedValue({ success: true, turn_id: 't2' });
	render(LoopGuardPanel, { isAdmin: true });
	expect(await screen.findByText('nightly report')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/settings/loop-guard/held/h1/confirm', {}));
	await waitFor(() => expect(screen.queryByText('nightly report')).toBeNull());
});

it('saves a changed budget', async () => {
	mocks.get.mockResolvedValue(status());
	mocks.put.mockResolvedValue(status({ session_turns_per_hour: 5 }));
	render(LoopGuardPanel, { isAdmin: true });
	const input = await screen.findByLabelText('Unattended turns per session per hour');
	await fireEvent.change(input, { target: { value: '5' } });
	await waitFor(() => expect(mocks.put).toHaveBeenCalledWith('/settings/loop-guard', { session_turns_per_hour: 5 }));
});
