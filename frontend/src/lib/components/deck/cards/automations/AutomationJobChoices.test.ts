import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { GenerationChoice, GenerationOption } from '$lib/utils/generation-choice';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	put: vi.fn(),
	post: vi.fn(),
	del: vi.fn(),
	openSettings: vi.fn()
}));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put, post: mocks.post, delete: mocks.del },
	getErrorMessage: (e: unknown, fallback: string) => {
		if (e && typeof e === 'object' && 'detail' in e) return String((e as { detail: unknown }).detail);
		return e instanceof Error ? e.message : fallback;
	}
}));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	const store = writable({ messages: [] as unknown[], sessionId: 's1' });
	return { activeSession: { subscribe: store.subscribe, sendMessage: vi.fn() } };
});
vi.mock('$lib/stores/worker-presence', async () => {
	const { readable } = await import('svelte/store');
	return {
		workerInstalls: readable([
			{ id: 'i-a', device_id: 'worker-a', device_name: 'DesktopWorker', is_self: true },
			{ id: 'i-b', device_id: 'worker-b', device_name: 'ServerWorker' }
		]),
		refreshWorkerPresence: vi.fn(() => Promise.resolve([]))
	};
});
vi.mock('$lib/settings/navigation.svelte', () => ({ openSettings: mocks.openSettings }));

import { generationChoices } from '$lib/stores/generation-choices.svelte';
import AutomationJobChoices from './AutomationJobChoices.svelte';

const JOB = '11111111-2222-4333-8444-555555555555';
const PRICE = { currency: 'USD', unit: 'item', amount: '0.025', total: '0.025', status: 'estimated' };

const REMEMBERED: GenerationChoice = {
	source: 'supergrok',
	account: 'grok-1',
	worker: 'worker-a',
	model: 'grok-imagine-image',
	options: {}
};
const PINNED: GenerationChoice = {
	source: 'fal',
	account: 'fal-ai',
	worker: 'worker-b',
	model: 'fal-ai/flux/dev',
	options: { image_size: 'square' }
};
const FAL_OPTION: GenerationOption = {
	source: 'fal',
	account: 'fal-ai',
	worker: 'worker-b',
	model: 'fal-ai/flux/dev',
	kind: 'image',
	options: { image_size: 'square' },
	verified: false,
	price: PRICE,
	schema: null,
	available: true,
	reason: null
};

interface Fixture {
	remembered?: Partial<Record<string, GenerationChoice>>;
	job?: Partial<Record<string, GenerationChoice>> | Error;
}

function routes({ remembered = {}, job = {} }: Fixture = {}) {
	mocks.get.mockImplementation((url: string) => {
		if (url === '/generation/choices') {
			return Promise.resolve({ remembered, last: {}, migrated: true, worker: 'worker-a' });
		}
		if (url === `/generation/jobs/${JOB}/choices`) {
			return job instanceof Error ? Promise.reject(job) : Promise.resolve({ job_id: JOB, choices: job });
		}
		if (url.startsWith('/generation/options/image')) return Promise.resolve({ options: [FAL_OPTION] });
		return Promise.reject(new Error(`unexpected GET ${url}`));
	});
	mocks.post.mockResolvedValue({ ok: true, price: PRICE, priced: true, options: {} });
}

function rowFor(kind: string): HTMLElement {
	const row = document.querySelector<HTMLElement>(`.ajc-row[data-kind="${kind}"]`);
	if (!row) throw new Error(`no row for ${kind}`);
	return row;
}

beforeEach(() => {
	vi.clearAllMocks();
	generationChoices.remembered = {};
	generationChoices.last = {};
	generationChoices.worker = '';
	generationChoices.status = 'idle';
});

afterEach(() => cleanup());

describe('AutomationJobChoices', () => {
	it('shows the remembered-or-skip empty state for every kind with nothing pinned', async () => {
		routes({ remembered: { image: REMEMBERED } });
		render(AutomationJobChoices, { props: { jobId: null, mode: 'draft', draft: {} } });

		await waitFor(() => expect(within(rowFor('image')).getByText(/Remembered:/)).toBeTruthy());
		for (const kind of ['image', 'video', 'audio', '3d']) {
			expect(within(rowFor(kind)).getByText('Use remembered choice; otherwise skip and report.')).toBeTruthy();
		}
		const image = within(rowFor('image'));
		expect(image.getByText('SuperGrok · Grok Imagine Image')).toBeTruthy();
		expect(rowFor('image').dataset.tier).toBe('remembered');
		expect(within(rowFor('video')).getByText(/No remembered choice for videos/)).toBeTruthy();
		expect(rowFor('video').dataset.tier).toBe('skip');
		// A new automation has no id yet: nothing is read from the job API.
		expect(mocks.get).not.toHaveBeenCalledWith(`/generation/jobs/${JOB}/choices`);
	});

	it('stages a pick in draft mode without writing anything', async () => {
		routes();
		const onDraftChange = vi.fn();
		render(AutomationJobChoices, { props: { jobId: null, mode: 'draft', draft: {}, onDraftChange } });

		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Pin a choice/ }));
		const pin = await screen.findByRole('button', { name: /Pin to this job/ });
		await fireEvent.click(pin);

		await waitFor(() => expect(onDraftChange).toHaveBeenCalledTimes(1));
		expect(onDraftChange.mock.calls[0][0]).toEqual({
			image: { source: 'fal', account: 'fal-ai', worker: 'worker-b', model: 'fal-ai/flux/dev', options: { image_size: 'square' } }
		});
		expect(mocks.put).not.toHaveBeenCalled();
	});

	it('pins a choice in live mode through the job API, never the remembered choice', async () => {
		routes({ remembered: { image: REMEMBERED } });
		mocks.put.mockImplementation((_url: string, body: GenerationChoice) => Promise.resolve({ job_id: JOB, kind: 'image', choice: body }));
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('remembered'));
		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Pin a choice/ }));
		await fireEvent.click(await screen.findByRole('button', { name: /Pin to this job/ }));

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('job'));
		expect(mocks.put).toHaveBeenCalledTimes(1);
		expect(mocks.put).toHaveBeenCalledWith(`/generation/jobs/${JOB}/choices/image`, {
			source: 'fal',
			account: 'fal-ai',
			worker: 'worker-b',
			model: 'fal-ai/flux/dev',
			options: { image_size: 'square' }
		});
		expect(mocks.put).not.toHaveBeenCalledWith('/generation/choices/image', expect.anything());
		const row = within(rowFor('image'));
		expect(row.getByText('Job override')).toBeTruthy();
		expect(row.getByText('ServerWorker')).toBeTruthy();
		await waitFor(() => expect(row.getByText('$0.025')).toBeTruthy());
	});

	it('shows the save-time price on a live pin and closes the picker, without quoting again', async () => {
		routes();
		const fresh = { ...PRICE, amount: '0.03', total: '0.03', status: 'quoted' };
		mocks.put.mockImplementation((_url: string, body: GenerationChoice) =>
			Promise.resolve({ job_id: JOB, kind: 'image', choice: body, price: fresh, priced: true, quote_error: null, unattended_skip_reason: null, warning: null })
		);
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('skip'));
		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Pin a choice/ }));
		await fireEvent.click(await screen.findByRole('button', { name: /Pin to this job/ }));

		await waitFor(() => expect(within(rowFor('image')).getByText('$0.03')).toBeTruthy());
		expect(rowFor('image').querySelector('.ajc-picker')).toBeNull();
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it.each([
		[
			'unpriced',
			{ price: { ...PRICE, amount: null, total: null, status: 'unknown' }, priced: false, quote_error: null, unattended_skip_reason: 'stored_choice_unpriced', warning: 'This saved choice has no verified price. Unattended runs will skip it until a price is available.' },
			/Price unknown\. Runs skip it until it has a price\./
		],
		[
			'quote timeout',
			{ price: { ...PRICE, amount: null, total: null, status: 'unknown' }, priced: false, quote_error: "The saved choice's quote timed out. Its worker or provider may be unavailable.", unattended_skip_reason: 'stored_choice_unavailable', warning: "The saved choice's quote timed out. Its worker or provider may be unavailable. Unattended runs will skip it." },
			/quote timed out\. Its worker or provider may be unavailable\. Runs skip it and report why\./
		]
	])('keeps the picker open on a live %s pin and warns on the row', async (_name, quote, rowText) => {
		routes();
		mocks.put.mockImplementation((_url: string, body: GenerationChoice) =>
			Promise.resolve({ job_id: JOB, kind: 'image', choice: body, ...quote })
		);
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('skip'));
		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Pin a choice/ }));
		await fireEvent.click(await screen.findByRole('button', { name: /Pin to this job/ }));

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('job'));
		const row = within(rowFor('image'));
		expect(row.getByText(rowText)).toBeTruthy();
		// The picker stays open with the card's notice, so the warning is not collapsed away.
		const alert = within(rowFor('image').querySelector('.ajc-picker') as HTMLElement).getByRole('alert');
		expect(alert.textContent).toContain('Pinned to this job:');
		expect(alert.textContent).toContain(quote.warning);
		expect(mocks.put).toHaveBeenCalledTimes(1);
		expect(mocks.post).not.toHaveBeenCalled();
	});

	it('shows a job override against the remembered choice it replaces', async () => {
		routes({ remembered: { image: REMEMBERED }, job: { image: PINNED } });
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('job'));
		const row = within(rowFor('image'));
		expect(row.getByText('fal.ai · Flux Dev')).toBeTruthy();
		const replaced = row.getByText('SuperGrok · Grok Imagine Image');
		expect(replaced.tagName).toBe('S');
		expect(replaced.closest('p')?.textContent).toMatch(/Replaces your remembered choice/);
		const ladder = rowFor('image').querySelectorAll('.ajc-ladder li');
		expect(ladder[0].getAttribute('aria-current')).toBe('true');
		expect(ladder[1].classList.contains('overridden')).toBe(true);
		expect(row.getByText('fal-ai')).toBeTruthy();
		expect(row.getByText('image size square')).toBeTruthy();
	});

	it('clears a pinned choice in live mode and falls back to the remembered choice', async () => {
		routes({ remembered: { image: REMEMBERED }, job: { image: PINNED } });
		mocks.del.mockResolvedValue({ job_id: JOB, kind: 'image', choice: null });
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('job'));
		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Clear the job choice for images/ }));

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('remembered'));
		expect(mocks.del).toHaveBeenCalledWith(`/generation/jobs/${JOB}/choices/image`);
		expect(within(rowFor('image')).getByText('Use remembered choice; otherwise skip and report.')).toBeTruthy();
	});

	it('stages a clear in draft mode as null and offers undo', async () => {
		routes({ job: { image: PINNED } });
		const onDraftChange = vi.fn();
		const { rerender } = render(AutomationJobChoices, {
			props: { jobId: JOB, mode: 'draft', draft: {}, onDraftChange }
		});

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('job'));
		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Clear the job choice for images/ }));
		expect(onDraftChange).toHaveBeenLastCalledWith({ image: null });
		expect(mocks.del).not.toHaveBeenCalled();

		await rerender({ jobId: JOB, mode: 'draft', draft: { image: null }, onDraftChange });
		const row = within(rowFor('image'));
		expect(rowFor('image').dataset.tier).toBe('skip');
		expect(row.getByText(/cleared when you save/)).toBeTruthy();
		await fireEvent.click(row.getByRole('button', { name: /Undo/ }));
		expect(onDraftChange).toHaveBeenLastCalledWith({});
	});

	it('reports an unreadable job record and fails closed instead of showing the empty state', async () => {
		routes({ job: Object.assign(new Error('x'), { detail: "The job's generation choices could not be read.", status: 503 }) });
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toMatch(/could not be read/);
		expect(alert.textContent).toMatch(/Runs fail closed/);
		expect(rowFor('image').dataset.tier).toBe('unreadable');
		expect(within(rowFor('image')).queryByText('Use remembered choice; otherwise skip and report.')).toBeNull();
		expect(within(rowFor('image')).getByRole('button', { name: 'Clear what is stored' })).toBeTruthy();
	});

	it('does not offer to clear a record when the job could not be loaded at all', async () => {
		routes({ job: Object.assign(new Error('x'), { detail: 'Unknown job.', status: 404 }) });
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		const alert = await screen.findByRole('alert');
		expect(alert.textContent).toMatch(/Unknown job\./);
		expect(alert.textContent).toMatch(/could not be loaded/);
		expect(rowFor('image').dataset.tier).toBe('unreadable');
		expect(within(rowFor('image')).queryByRole('button', { name: 'Clear what is stored' })).toBeNull();
	});

	it('keeps the row unpinned and shows the error when saving a pick fails', async () => {
		routes();
		mocks.put.mockRejectedValue(Object.assign(new Error('x'), { detail: 'A job choice must pin its account, worker and model.' }));
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('skip'));
		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Pin a choice/ }));
		await fireEvent.click(await screen.findByRole('button', { name: /Pin to this job/ }));

		await waitFor(() => expect(screen.getByText('A job choice must pin its account, worker and model.')).toBeTruthy());
		expect(rowFor('image').dataset.tier).toBe('skip');
		// The picker stays open so the owner can try again.
		expect(screen.getByRole('button', { name: /Pin to this job/ })).toBeTruthy();
	});

	it('keeps the pinned choice and explains when clearing fails', async () => {
		routes({ job: { image: PINNED } });
		mocks.del.mockRejectedValue(Object.assign(new Error('x'), { detail: 'Network down.' }));
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		await waitFor(() => expect(rowFor('image').dataset.tier).toBe('job'));
		await fireEvent.click(within(rowFor('image')).getByRole('button', { name: /Clear the job choice for images/ }));

		const error = await within(rowFor('image')).findByRole('alert');
		expect(error.textContent).toMatch(/Network down\. The job still uses its pinned choice\./);
		expect(rowFor('image').dataset.tier).toBe('job');
	});

	it('shows an unavailable quote as a warning, never as a zero price', async () => {
		routes({ job: { image: PINNED } });
		mocks.post.mockResolvedValue({ ok: false, error: 'This account or model is not available right now.' });
		render(AutomationJobChoices, { props: { jobId: JOB, mode: 'live' } });

		const row = within(rowFor('image'));
		await waitFor(() => expect(row.getByText(/not available right now\. Runs skip it and report why\./)).toBeTruthy());
		expect(row.queryByText('$0')).toBeNull();
	});
});
