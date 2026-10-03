import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import BackgroundWorkStrip from './BackgroundWorkStrip.svelte';
import type { BackgroundJob } from '$lib/utils/background-work';

const job = (n: number, patch: Partial<BackgroundJob> = {}): BackgroundJob => ({
	key: `task:${n}`, label: `Job number ${n}`, startedAt: Date.now() - 6 * 60_000,
	sessionId: `chat-${n}`, stop: { kind: 'task', taskId: `t${n}` }, ...patch
});

afterEach(cleanup);

describe('BackgroundWorkStrip', () => {
	it('renders nothing while no work runs', () => {
		const { container } = render(BackgroundWorkStrip, { props: { jobs: [] } });
		expect(container.querySelector('.work-strip')).toBeNull();
	});

	it('shows one plain line with how long it has run, and opens its chat', async () => {
		const onOpen = vi.fn();
		render(BackgroundWorkStrip, { props: { jobs: [job(1, { label: 'Fixing the sidebar bug' })], onOpen, onStop: vi.fn() } });
		const line = screen.getByRole('button', { name: 'Fixing the sidebar bug, 6 min' });
		await fireEvent.click(line);
		expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ key: 'task:1' }));
		expect(screen.getAllByRole('button', { name: /^Stop / })).toHaveLength(1);
	});

	it('shows three lines and collapses the rest to "and N more"', async () => {
		render(BackgroundWorkStrip, { props: { jobs: [1, 2, 3, 4, 5].map((n) => job(n)), onOpen: vi.fn(), onStop: vi.fn() } });
		expect(screen.getAllByRole('listitem')).toHaveLength(3);
		const more = screen.getByRole('button', { name: 'and 2 more' });
		expect(more.getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(more);
		expect(screen.getAllByRole('listitem')).toHaveLength(5);
		expect(screen.getByRole('button', { name: 'Show fewer' })).toBeTruthy();
	});

	it('has one Stop per line that disables while stopping and re-enables if it fails', async () => {
		let resolve!: (ok: boolean) => void;
		const onStop = vi.fn(() => new Promise<boolean>((r) => { resolve = r; }));
		render(BackgroundWorkStrip, { props: { jobs: [job(1)], onStop } });
		const stop = screen.getByRole('button', { name: 'Stop Job number 1' }) as HTMLButtonElement;
		await fireEvent.click(stop);
		expect(onStop).toHaveBeenCalledWith(expect.objectContaining({ stop: { kind: 'task', taskId: 't1' } }));
		await waitFor(() => expect(stop.disabled).toBe(true));
		resolve(false);
		await waitFor(() => expect(stop.disabled).toBe(false));
	});

	it('keeps a line without a chat as plain text', () => {
		render(BackgroundWorkStrip, { props: { jobs: [job(1, { sessionId: null, label: 'Watching checks' })], onOpen: vi.fn(), onStop: vi.fn() } });
		expect(screen.queryByRole('button', { name: /Watching checks, / })).toBeNull();
		expect(screen.getByText('Watching checks')).toBeTruthy();
	});
});
