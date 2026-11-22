import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: transport,
	getErrorMessage: (e: { detail?: string; message?: string }, fallback: string) => e?.detail ?? e?.message ?? fallback,
	isApiError: (e: unknown) => typeof e === 'object' && e !== null && 'status' in e
}));

import MemoryHealthDetail from './MemoryHealthDetail.svelte';
import { memoryHealth as memoryHealthStore } from '$lib/stores/memory-health.svelte';

let memoryHealth: unknown;
const asked = () => transport.get.mock.calls.filter(([path]) => path === '/memory/health').length;
const hintOf = (row: HTMLElement) => (row.closest('.st-row') as HTMLElement).querySelector('.st-rh')?.textContent;

beforeEach(() => {
	vi.resetAllMocks();
	memoryHealthStore.reset();
	memoryHealth = { state: 'healthy', checked_at: 1, signals: [], repairs: [] };
	transport.get.mockImplementation(async (path: string) => {
		if (path === '/memory/health') {
			if (memoryHealth instanceof Error) throw memoryHealth;
			return memoryHealth;
		}
		return {};
	});
});
afterEach(() => {
	cleanup();
	memoryHealthStore.reset();
});

describe('Settings > Memory > Memory health', () => {
	it('renders the registry row and shows a healthy store with one line', async () => {
		const { container } = render(MemoryHealthDetail);
		expect(container.querySelector('[data-setting-anchor="memory.health"]')).toBeTruthy();
		const row = screen.getByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Healthy'));
		expect(await screen.findByText('Nothing needs attention')).toBeTruthy();
	});

	it('names what needs attention and what was repaired, without opening anything', async () => {
		const at = Date.now() / 1000;
		memoryHealth = {
			state: 'attention',
			checked_at: at,
			signals: [
				{ id: 'learner', label: 'Learning', state: 'attention', reason: 'Learning has not run despite recent sessions.' },
				{ id: 'saves', label: 'Saving', state: 'healthy', reason: 'Saves are writing normally.' }
			],
			repairs: [{ kind: 'duplicates', count: 1, at, summary: 'Archived 1 exact duplicate.' }]
		};
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Needs attention'));
		expect(await screen.findByText('Learning has not run despite recent sessions.')).toBeTruthy();
		expect(screen.getByText(/Archived 1 exact duplicate\./)).toBeTruthy();
		expect(screen.queryByText('Saves are writing normally.')).toBeNull();
	});

	it('says when it last checked, with no warning banner (#1042)', async () => {
		memoryHealth = {
			state: 'attention',
			checked_at: Date.now() / 1000 - 12 * 60,
			signals: [
				{ id: 'capacity', label: 'Capacity', state: 'attention', reason: 'Automatic cleanup has recorded 3 permanent deletions of older facts. Recall cannot recover a deleted copy.' }
			],
			repairs: []
		};
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Needs attention'));
		expect(hintOf(row)).toBe('Checked 12m ago');
		expect(document.querySelector('.st-banner.warn')).toBeNull();
		expect(screen.getByText(/Recall cannot recover a deleted copy\./)).toBeTruthy();
		expect(screen.getByText(/^Last checked /)).toBeTruthy();
	});

	it('shows the last answer at once on reopening, then asks again', async () => {
		memoryHealth = {
			state: 'attention',
			checked_at: Date.now() / 1000 - 60,
			signals: [{ id: 'capacity', label: 'Capacity', state: 'attention', reason: 'Automatic cleanup has recorded 3 permanent deletions of older facts.' }],
			repairs: []
		};
		await memoryHealthStore.refresh();
		expect(asked()).toBe(1);
		memoryHealth = { state: 'healthy', checked_at: Date.now() / 1000, signals: [], repairs: [] };
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		// The last answer is on the row at once, with no Loading in between.
		expect(row.textContent).toContain('Needs attention');
		await waitFor(() => expect(row.textContent).toContain('Healthy'));
		expect(asked()).toBe(2);
	});

	it('asks again when the store forgets its answer while the pane stays open', async () => {
		memoryHealth = { state: 'healthy', checked_at: Date.now() / 1000, signals: [], repairs: [] };
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Healthy'));
		const before = asked();
		memoryHealthStore.reset();
		await waitFor(() => expect(asked()).toBe(before + 1));
		await waitFor(() => expect(row.textContent).toContain('Healthy'));
	});

	it('says when it has not checked yet', async () => {
		memoryHealth = { state: 'healthy', checked_at: null, signals: [], repairs: [] };
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Healthy'));
		expect(hintOf(row)).toBe('Not checked yet');
	});

	it('is owner only', async () => {
		memoryHealth = Object.assign(new Error('Owner access required'), { status: 403 });
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Available after approval'));
		expect(screen.queryByText('Nothing needs attention')).toBeNull();
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('shows a busy dot while loading and the answer in the same row once it lands', async () => {
		let answer!: (health: unknown) => void;
		memoryHealth = new Promise((done) => (answer = done));
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		expect(row.textContent).toContain('Loading');
		expect(row.querySelector('.st-dot.busy')).toBeTruthy();
		answer({ state: 'healthy', checked_at: Date.now() / 1000 - 60, signals: [], repairs: [] });
		await waitFor(() => expect(row.textContent).toContain('Healthy'));
		expect(screen.getByTestId('memory-health')).toBe(row);
		expect(row.querySelector('.st-dot.ok')).toBeTruthy();
	});

	it('says when the check cannot be read', async () => {
		memoryHealth = Object.assign(new Error('Server error'), { status: 500 });
		render(MemoryHealthDetail);
		const row = screen.getByTestId('memory-health');
		await waitFor(() => expect(row.textContent).toContain('Unavailable'));
		expect(row.querySelector('.st-dot.off')).toBeTruthy();
		expect(document.querySelector('.st-banner.warn')).toBeNull();
	});
});
