/**
 * Settings > Providers > Claude > Sessions: the Claude compaction switch.
 * On by default, saves the owner's change, flips back when a save fails, and
 * is read-only for a viewer who cannot change Settings.
 */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import ClaudeCompaction from './ClaudeCompaction.svelte';
import { fakeWorker } from './fake-worker';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
import { getEntry } from '$lib/settings/registry';

let worker: ReturnType<typeof fakeWorker>;

function show(readOnly = false) {
	const context = readOnly ? new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]]) : undefined;
	render(ClaudeCompaction, context ? { context } : {});
}

const toggle = () => screen.findByRole('switch', { name: /Smarter compaction/ });

beforeEach(() => {
	worker = fakeWorker();
	vi.stubGlobal('fetch', worker.fetch);
});
afterEach(() => {
	cleanup();
	vi.unstubAllGlobals();
});

it('renders on by default with a short plain description', async () => {
	show();
	const sw = await toggle();
	expect(sw.getAttribute('aria-checked')).toBe('true');
	const group = screen.getByRole('region', { name: 'Sessions' });
	expect(within(group).getByText('Smarter compaction')).toBeTruthy();
	expect(within(group).getByText('Keeps more of the conversation')).toBeTruthy();
	expect(getEntry('providers.claudeCompaction')?.default).toBe(true);
	expect(getEntry('providers.claudeCompaction')?.help).toMatch(/more tokens/);
	expect(worker.sent('PUT', '/settings/claude-compaction')).toHaveLength(0);
});

it('saves a change and shows the saved value', async () => {
	show();
	await fireEvent.click(await toggle());
	await waitFor(() =>
		expect(worker.sent('PUT', '/settings/claude-compaction')[0]?.body).toEqual({ enabled: false })
	);
	expect(worker.state.compaction).toBe(false);
	await waitFor(async () => expect((await toggle()).getAttribute('aria-checked')).toBe('false'));
	await fireEvent.click(await toggle());
	await waitFor(() => expect(worker.sent('PUT', '/settings/claude-compaction')[1]?.body).toEqual({ enabled: true }));
	expect(worker.state.compaction).toBe(true);
});

it('shows a saved off value', async () => {
	worker.state.compaction = false;
	show();
	await waitFor(async () => expect((await toggle()).getAttribute('aria-checked')).toBe('false'));
});

it('flips back and says why when the save fails', async () => {
	worker = fakeWorker(
		{},
		{
			'PUT /settings/claude-compaction': () => {
				throw { status: 500, detail: 'Database unavailable' };
			}
		}
	);
	vi.stubGlobal('fetch', worker.fetch);
	show();
	await fireEvent.click(await toggle());
	expect((await screen.findByRole('alert')).textContent).toBe('Database unavailable');
	await waitFor(async () => expect((await toggle()).getAttribute('aria-checked')).toBe('true'));
});

it('is read-only for a viewer who cannot change Settings', async () => {
	show(true);
	const sw = await toggle();
	expect((sw as HTMLButtonElement).disabled).toBe(true);
	await fireEvent.click(sw);
	expect(worker.sent('PUT', '/settings/claude-compaction')).toHaveLength(0);
});
