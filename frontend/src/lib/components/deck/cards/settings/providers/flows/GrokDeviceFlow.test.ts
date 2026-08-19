/**
 * Behavior tests for GrokDeviceFlow's two mount modes.
 *
 * DRAFT mode creates a placeholder `grok` row; RECONNECT reuses an existing id
 * and never deletes it. Mirrors ChatGptDeviceFlow.test.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, cleanup, waitFor } from '@testing-library/svelte';

if (!('ResizeObserver' in globalThis)) {
	(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
		observe() {}
		unobserve() {}
		disconnect() {}
	};
}

const mocks = vi.hoisted(() => ({
	createModelProvider: vi.fn(),
	deleteModelProvider: vi.fn(),
	updateModelProvider: vi.fn(),
	xaiDeviceStart: vi.fn(),
	xaiDevicePoll: vi.fn(),
	refreshProviders: vi.fn(),
	fetchProviderModels: vi.fn(),
	storeProviders: [] as { id: string; models: { id: string; label: string }[] }[]
}));

vi.mock('$lib/api/model-providers', () => ({
	createModelProvider: mocks.createModelProvider,
	deleteModelProvider: mocks.deleteModelProvider,
	updateModelProvider: mocks.updateModelProvider,
	xaiDeviceStart: mocks.xaiDeviceStart,
	xaiDevicePoll: mocks.xaiDevicePoll
}));
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: {
		refreshProviders: mocks.refreshProviders,
		fetchProviderModels: mocks.fetchProviderModels,
		get current() {
			return { providers: mocks.storeProviders };
		}
	}
}));
vi.mock('$lib/api/client', () => ({
	getErrorMessage: (_e: unknown, fallback: string) => fallback
}));

import GrokDeviceFlow from './GrokDeviceFlow.svelte';

const DEVICE = { user_code: 'ABCD-1234', verification_uri: 'https://auth.x.ai/activate' };

const FN_MOCKS = [
	'createModelProvider',
	'deleteModelProvider',
	'updateModelProvider',
	'xaiDeviceStart',
	'xaiDevicePoll',
	'refreshProviders',
	'fetchProviderModels'
] as const;

beforeEach(() => {
	FN_MOCKS.forEach((k) => mocks[k].mockReset());
	mocks.storeProviders = [];
	mocks.xaiDeviceStart.mockResolvedValue(DEVICE);
	mocks.xaiDevicePoll.mockResolvedValue({ status: 'pending' });
	mocks.refreshProviders.mockResolvedValue(undefined);
	mocks.fetchProviderModels.mockResolvedValue({ source: 'preset', models: [] });
	mocks.updateModelProvider.mockResolvedValue(undefined);
});

afterEach(() => cleanup());

describe('GrokDeviceFlow — reconnect mode (existingProviderId)', () => {
	it('mount starts device on the existing row and does NOT create one', async () => {
		render(GrokDeviceFlow, {
			props: { existingProviderId: 'prov-9', onDone: vi.fn(), onCancel: vi.fn() }
		});
		await waitFor(() => expect(mocks.xaiDeviceStart).toHaveBeenCalledWith('prov-9'));
		expect(mocks.createModelProvider).not.toHaveBeenCalled();
	});

	it('cancel from awaiting does NOT delete the row and calls onCancel', async () => {
		const onCancel = vi.fn();
		const { findByText } = render(GrokDeviceFlow, {
			props: { existingProviderId: 'prov-9', onDone: vi.fn(), onCancel }
		});
		const cancelBtn = await findByText('Cancel');
		await fireEvent.click(cancelBtn);
		expect(mocks.deleteModelProvider).not.toHaveBeenCalled();
		expect(onCancel).toHaveBeenCalled();
	});
});

describe('GrokDeviceFlow — draft mode', () => {
	it('mount creates a placeholder grok row then starts device on it', async () => {
		mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
		render(GrokDeviceFlow, { props: { onDone: vi.fn(), onCancel: vi.fn() } });
		await waitFor(() => expect(mocks.createModelProvider).toHaveBeenCalled());
		expect(mocks.createModelProvider.mock.calls[0][0]).toMatchObject({
			provider_type: 'grok',
			name: 'Grok',
			base_url: 'https://api.x.ai/v1'
		});
		await waitFor(() => expect(mocks.xaiDeviceStart).toHaveBeenCalledWith('draft-1'));
	});

	it('cancel from awaiting deletes the draft row and calls onCancel', async () => {
		mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
		const onCancel = vi.fn();
		const { findByText } = render(GrokDeviceFlow, {
			props: { onDone: vi.fn(), onCancel }
		});
		const cancelBtn = await findByText('Cancel');
		await fireEvent.click(cancelBtn);
		await waitFor(() => expect(mocks.deleteModelProvider).toHaveBeenCalledWith('draft-1'));
		expect(onCancel).toHaveBeenCalled();
	});
});

describe('GrokDeviceFlow — model seeding on completion', () => {
	it('draft completion seeds the full preset roster onto the new row', async () => {
		vi.useFakeTimers();
		try {
			mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
			const roster = [
				{ id: 'grok-4.6', label: 'Grok 4.6' },
				{ id: 'grok-4.3', label: 'Grok 4.3' }
			];
			mocks.fetchProviderModels.mockResolvedValue({ source: 'preset', models: roster });
			mocks.xaiDevicePoll.mockResolvedValue({ status: 'complete' });
			const onDone = vi.fn();

			render(GrokDeviceFlow, { props: { onDone, onCancel: vi.fn() } });
			await vi.advanceTimersByTimeAsync(0);
			await vi.advanceTimersByTimeAsync(3000);

			expect(mocks.fetchProviderModels).toHaveBeenCalledWith('draft-1');
			expect(mocks.updateModelProvider).toHaveBeenCalledWith('draft-1', { models: roster });
			expect(onDone).toHaveBeenCalled();
		} finally {
			vi.useRealTimers();
		}
	});

	it('reconnect completion does NOT overwrite an existing curated roster', async () => {
		vi.useFakeTimers();
		try {
			mocks.storeProviders = [
				{ id: 'prov-9', models: [{ id: 'grok-4.6', label: 'Grok 4.6' }] }
			];
			mocks.xaiDevicePoll.mockResolvedValue({ status: 'complete' });
			const onDone = vi.fn();

			render(GrokDeviceFlow, {
				props: { existingProviderId: 'prov-9', onDone, onCancel: vi.fn() }
			});
			await vi.advanceTimersByTimeAsync(0);
			await vi.advanceTimersByTimeAsync(3000);

			expect(mocks.fetchProviderModels).not.toHaveBeenCalled();
			expect(mocks.updateModelProvider).not.toHaveBeenCalled();
			expect(onDone).toHaveBeenCalled();
		} finally {
			vi.useRealTimers();
		}
	});
});
