/**
 * Claude subscription > Policy: the loop guard and its held turns. Ported
 * from LoopGuardPanel.test.ts: usage and defaults hints, the API key / off
 * text, confirming a held turn, and saving a changed budget.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { ClaudeAuthMode } from '$lib/api/claude-auth';
import ClaudePolicy from './ClaudePolicy.svelte';
import { LoopGuard } from '../loop-guard.svelte';
import { fakeWorker, loopGuardStatus } from '../fake-worker';

let worker: ReturnType<typeof fakeWorker>;

async function show(mode: ClaudeAuthMode = 'subscription') {
	const guard = new LoopGuard();
	await guard.load();
	render(ClaudePolicy, { guard, mode });
	return guard;
}

beforeEach(() => {
	worker = fakeWorker({ loopGuard: loopGuardStatus() });
	vi.stubGlobal('fetch', worker.fetch);
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it('shows this hour’s unattended turns and the budgets at their defaults', async () => {
	await show();
	expect(screen.getByText('This hour: 3 of 60')).toBeTruthy();
	expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true');
	expect((screen.getByLabelText('Per session') as HTMLInputElement).value).toBe('12');
	expect((screen.getByLabelText('Per worker') as HTMLInputElement).value).toBe('60');
	expect((screen.getByLabelText('Background worker limit') as HTMLInputElement).value).toBe('60');
	expect(screen.getByText('0 turns it off')).toBeTruthy();
	// At the default no "Default N" hint shows.
	expect(screen.queryByText(/^Default \d+$/)).toBeNull();
});

it('names the default once a budget differs from it', async () => {
	worker.state.loopGuard = loopGuardStatus({
		session_turns_per_hour: 5,
		install_turns_per_hour: 90
	});
	await show();
	expect(screen.getByText('Default 12')).toBeTruthy();
	expect(screen.getByText('Default 60')).toBeTruthy();
});

it('says it does nothing in API key mode', async () => {
	worker.state.loopGuard = loopGuardStatus({ applies: false });
	await show('api_key');
	expect(screen.getByText('Off in API key mode')).toBeTruthy();
	expect(screen.getByText('Console spend limit applies')).toBeTruthy();
	expect(screen.queryByRole('switch')).toBeNull();
	expect(screen.queryByLabelText('Per session')).toBeNull();
});

it('says it does nothing while Claude is off', async () => {
	worker.state.loopGuard = loopGuardStatus({ applies: false });
	await show('off');
	expect(screen.getByText('Off while Claude is off')).toBeTruthy();
	expect(screen.queryByRole('switch')).toBeNull();
});

it('confirms a held turn', async () => {
	worker.state.loopGuard = loopGuardStatus({
		held: [
			{
				id: 'h1',
				session_id: 's1',
				turn_id: 't1',
				source: 'automation',
				prompt: 'nightly report',
				reason: 'x',
				held_at: 0
			}
		]
	});
	let confirmed = false;
	worker = fakeWorker(
		{ loopGuard: worker.state.loopGuard },
		{
			'POST /settings/loop-guard/held/:id/confirm': () => {
				confirmed = true;
				worker.state.loopGuard = loopGuardStatus();
				return { success: true, turn_id: 't2' };
			}
		}
	);
	vi.stubGlobal('fetch', worker.fetch);
	await show();
	const held = screen.getByRole('region', { name: /Waiting for you/ });
	expect(within(held).getByText('nightly report')).toBeTruthy();
	expect(within(held).getByText(/^Automation, /)).toBeTruthy();
	await fireEvent.click(within(held).getByRole('button', { name: 'Confirm' }));
	await waitFor(() => expect(confirmed).toBe(true));
	expect(worker.sent('POST', '/settings/loop-guard/held/h1/confirm')[0].body).toEqual({});
	await waitFor(() => expect(screen.queryByText('nightly report')).toBeNull());
	expect(screen.getByText('No held turns')).toBeTruthy();
});

it('dismisses a held turn', async () => {
	worker.state.loopGuard = loopGuardStatus({
		held: [
			{
				id: 'h 2',
				session_id: 's1',
				turn_id: null,
				source: 'wakeup',
				prompt: null,
				reason: 'x',
				held_at: 0
			}
		]
	});
	await show();
	await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
	await waitFor(() => expect(worker.sent('POST', '/settings/loop-guard/held/h%202/dismiss')).toHaveLength(1));
});

it('saves a changed budget', async () => {
	await show();
	const input = screen.getByLabelText('Per session') as HTMLInputElement;
	await fireEvent.input(input, { target: { value: '5' } });
	await fireEvent.blur(input);
	await waitFor(() => expect(worker.sent('PUT', '/settings/loop-guard')).toHaveLength(1));
	expect(worker.sent('PUT', '/settings/loop-guard')[0].body).toEqual({
		session_turns_per_hour: 5
	});
	await waitFor(() => expect(screen.getByText('Default 12')).toBeTruthy());
});

it('turns the guard off and hides the budgets', async () => {
	await show();
	await fireEvent.click(screen.getByRole('switch'));
	await waitFor(() =>
		expect(worker.sent('PUT', '/settings/loop-guard')[0]?.body).toEqual({
			enabled: false
		})
	);
	await waitFor(() => expect(screen.queryByLabelText('Per session')).toBeNull());
	expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('false');
});

it('reloads and says so when a save fails', async () => {
	worker = fakeWorker(
		{},
		{
			'PUT /settings/loop-guard': () => {
				throw { status: 400, detail: 'Budget too high' };
			}
		}
	);
	vi.stubGlobal('fetch', worker.fetch);
	await show();
	const input = screen.getByLabelText('Per worker') as HTMLInputElement;
	await fireEvent.input(input, { target: { value: '500' } });
	await fireEvent.blur(input);
	expect((await screen.findByRole('alert')).textContent).toBe('Budget too high');
	await waitFor(() => expect(input.value).toBe('60'));
});
