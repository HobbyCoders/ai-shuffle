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
	codexDeviceStart: vi.fn(),
	codexDevicePoll: vi.fn(),
	refreshProviders: vi.fn()
}));

vi.mock('$lib/api/model-providers', () => ({
	createModelProvider: mocks.createModelProvider,
	deleteModelProvider: mocks.deleteModelProvider,
	codexDeviceStart: mocks.codexDeviceStart,
	codexDevicePoll: mocks.codexDevicePoll
}));
vi.mock('$lib/stores/providers.svelte', () => ({
	providers: { refreshProviders: mocks.refreshProviders }
}));
vi.mock('$lib/api/client', () => ({
	getErrorMessage: (_e: unknown, fallback: string) => fallback
}));

import ChatGptDeviceFlow from './ChatGptDeviceFlow.svelte';

const DEVICE = { user_code: 'ABCD-1234', verification_uri: 'https://example.test/verify' };

beforeEach(() => {
	Object.values(mocks).forEach((m) => m.mockReset());
	mocks.codexDeviceStart.mockResolvedValue(DEVICE);
	// Never let a scheduled poll resolve into a status branch during a test.
	mocks.codexDevicePoll.mockResolvedValue({ status: 'pending' });
	mocks.refreshProviders.mockResolvedValue(undefined);
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
