/**
 * Behavior tests for ChatGptDeviceFlow's two mount modes.
 *
 * DRAFT mode (no `existingProviderId`): mount CREATES a placeholder row and OWNS
 * it, so cancel/close DELETES the row (the original behavior — regression guard).
 *
 * RECONNECT mode (`existingProviderId` set, e.g. the Providers-list "Reconnect"
 * button): mount reuses the given row — it must NOT create one, must start
 * device/start against that id, and cancel/close must NOT delete the row.
 *
 * The repo mocks the API module (see providers.svelte.test.ts). We mock
 * `$lib/api/model-providers`, the `providers` store coupling, and `getErrorMessage`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, fireEvent, cleanup, waitFor } from '@testing-library/svelte';

// jsdom lacks ResizeObserver; some lucide/layout paths probe for it.
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
	codexDeviceStart: vi.fn(),
	codexDevicePoll: vi.fn(),
	refreshProviders: vi.fn(),
	fetchProviderModels: vi.fn(),
	// Backing list for the mocked store's `current.providers` (reconnect skip check).
	storeProviders: [] as { id: string; models: { id: string; label: string }[] }[]
}));

vi.mock('$lib/api/model-providers', () => ({
	createModelProvider: mocks.createModelProvider,
	deleteModelProvider: mocks.deleteModelProvider,
	updateModelProvider: mocks.updateModelProvider,
	codexDeviceStart: mocks.codexDeviceStart,
	codexDevicePoll: mocks.codexDevicePoll
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

import ChatGptDeviceFlow from './ChatGptDeviceFlow.svelte';

const DEVICE = { user_code: 'ABCD-1234', verification_uri: 'https://example.test/verify' };

const FN_MOCKS = [
	'createModelProvider',
	'deleteModelProvider',
	'updateModelProvider',
	'codexDeviceStart',
	'codexDevicePoll',
	'refreshProviders',
	'fetchProviderModels'
] as const;

beforeEach(() => {
	FN_MOCKS.forEach((k) => mocks[k].mockReset());
	mocks.storeProviders = [];
	mocks.codexDeviceStart.mockResolvedValue(DEVICE);
	// Never let a scheduled poll resolve into a status branch during a test.
	mocks.codexDevicePoll.mockResolvedValue({ status: 'pending' });
	mocks.refreshProviders.mockResolvedValue(undefined);
	mocks.fetchProviderModels.mockResolvedValue({ source: 'preset', models: [] });
	mocks.updateModelProvider.mockResolvedValue(undefined);
});

afterEach(() => cleanup());

describe('ChatGptDeviceFlow — reconnect mode (existingProviderId)', () => {
	it('mount starts device on the existing row and does NOT create one', async () => {
		render(ChatGptDeviceFlow, {
			props: { existingProviderId: 'prov-9', onDone: vi.fn(), onCancel: vi.fn() }
		});
		await waitFor(() => expect(mocks.codexDeviceStart).toHaveBeenCalledWith('prov-9'));
		expect(mocks.createModelProvider).not.toHaveBeenCalled();
	});

	it('cancel from awaiting does NOT delete the row and calls onCancel', async () => {
		const onCancel = vi.fn();
		const { findByText } = render(ChatGptDeviceFlow, {
			props: { existingProviderId: 'prov-9', onDone: vi.fn(), onCancel }
		});
		// awaiting phase renders the Cancel affordance once device/start resolves.
		const cancelBtn = await findByText('Cancel');
		await fireEvent.click(cancelBtn);
		expect(mocks.deleteModelProvider).not.toHaveBeenCalled();
		expect(onCancel).toHaveBeenCalled();
	});
});

describe('ChatGptDeviceFlow — draft mode (regression)', () => {
	it('mount creates a placeholder row then starts device on it', async () => {
		mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
		render(ChatGptDeviceFlow, { props: { onDone: vi.fn(), onCancel: vi.fn() } });
		await waitFor(() => expect(mocks.createModelProvider).toHaveBeenCalled());
		await waitFor(() => expect(mocks.codexDeviceStart).toHaveBeenCalledWith('draft-1'));
	});

	it('cancel from awaiting deletes the draft row and calls onCancel', async () => {
		mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
		const onCancel = vi.fn();
		const { findByText } = render(ChatGptDeviceFlow, {
			props: { onDone: vi.fn(), onCancel }
		});
		const cancelBtn = await findByText('Cancel');
		await fireEvent.click(cancelBtn);
		await waitFor(() => expect(mocks.deleteModelProvider).toHaveBeenCalledWith('draft-1'));
		expect(onCancel).toHaveBeenCalled();
	});
});

describe('ChatGptDeviceFlow — model seeding on completion', () => {
	it('draft completion seeds the full preset roster onto the new row', async () => {
		vi.useFakeTimers();
		try {
			mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
			const roster = [
				{ id: 'gpt-5.6', label: 'GPT-5.6' },
				{ id: 'gpt-5.6-mini', label: 'GPT-5.6 mini' }
			];
			mocks.fetchProviderModels.mockResolvedValue({ source: 'preset', models: roster });
			// The first poll reports completion → finish() → seed → onDone.
			mocks.codexDevicePoll.mockResolvedValue({ status: 'complete' });
			const onDone = vi.fn();

			render(ChatGptDeviceFlow, { props: { onDone, onCancel: vi.fn() } });
			// Flush mount (create → device/start → schedule the poll timer).
			await vi.advanceTimersByTimeAsync(0);
			// Fire the 3s poll → complete → seed → onDone.
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
			// The reconnected row already carries a curated model list.
			mocks.storeProviders = [
				{ id: 'prov-9', models: [{ id: 'gpt-5.6', label: 'GPT-5.6' }] }
			];
			mocks.codexDevicePoll.mockResolvedValue({ status: 'complete' });
			const onDone = vi.fn();

			render(ChatGptDeviceFlow, {
				props: { existingProviderId: 'prov-9', onDone, onCancel: vi.fn() }
			});
			await vi.advanceTimersByTimeAsync(0);
			await vi.advanceTimersByTimeAsync(3000);

			// Seeding is skipped entirely — no fetch, no clobbering update.
			expect(mocks.fetchProviderModels).not.toHaveBeenCalled();
			expect(mocks.updateModelProvider).not.toHaveBeenCalled();
			expect(onDone).toHaveBeenCalled();
		} finally {
			vi.useRealTimers();
		}
	});
});
