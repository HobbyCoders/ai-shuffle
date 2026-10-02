/**
 * Device-code sign-in (ChatGPT and SuperGrok), ported from the old
 * ChatGptDeviceFlow and GrokDeviceFlow tests.
 *
 * DRAFT mode creates a placeholder row and owns it: Cancel deletes it.
 * RECONNECT mode reuses a row: it never creates one and never deletes it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	createModelProvider: vi.fn(),
	deleteModelProvider: vi.fn(),
	updateModelProvider: vi.fn(),
	codexDeviceStart: vi.fn(),
	codexDevicePoll: vi.fn(),
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
	codexDeviceStart: mocks.codexDeviceStart,
	codexDevicePoll: mocks.codexDevicePoll,
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

import DeviceCodeSetup from './setup/DeviceCodeSetup.svelte';
import { DeviceEnrollment } from './device-enrollment.svelte';

const DEVICE = { user_code: 'ABCD-1234', verification_uri: 'https://example.test/verify' };

const KINDS = [
	{
		kind: 'codex' as const,
		start: 'codexDeviceStart' as const,
		poll: 'codexDevicePoll' as const,
		type: 'chatgpt',
		base: 'https://chatgpt.com/backend-api/codex',
		name: 'ChatGPT'
	},
	{
		kind: 'xai' as const,
		start: 'xaiDeviceStart' as const,
		poll: 'xaiDevicePoll' as const,
		type: 'grok',
		base: 'https://api.x.ai/v1',
		name: 'Grok'
	}
];

beforeEach(() => {
	for (const [key, value] of Object.entries(mocks))
		if (typeof value === 'function') (value as ReturnType<typeof vi.fn>).mockReset();
	mocks.storeProviders = [];
	for (const k of KINDS) {
		mocks[k.start].mockResolvedValue(DEVICE);
		// Never let a scheduled poll resolve into a status branch unless a test says so.
		mocks[k.poll].mockResolvedValue({ status: 'pending' });
	}
	mocks.refreshProviders.mockResolvedValue(undefined);
	mocks.fetchProviderModels.mockResolvedValue({ source: 'preset', models: [] });
	mocks.updateModelProvider.mockResolvedValue(undefined);
	mocks.deleteModelProvider.mockResolvedValue(undefined);
});

afterEach(() => cleanup());

describe.each(KINDS)('$name sign-in', ({ kind, start, poll, type, base, name }) => {
	it.each([null, 'prov-9'])(
		'keeps the original row and ownership after prop updates (%s)',
		async (existingProviderId) => {
			mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
			const onCancel = vi.fn();
			const { findByText, rerender } = render(DeviceCodeSetup, { kind, existingProviderId, onCancel });
			await findByText('Cancel');
			await rerender({ kind, existingProviderId: existingProviderId ? null : 'prov-other', onCancel });
			await fireEvent.click(await findByText('Cancel'));
			await waitFor(() => expect(onCancel).toHaveBeenCalledOnce());
			expect(mocks[start]).toHaveBeenCalledExactlyOnceWith(existingProviderId ?? 'draft-1');
			if (existingProviderId) {
				expect(mocks.createModelProvider).not.toHaveBeenCalled();
				expect(mocks.deleteModelProvider).not.toHaveBeenCalled();
			} else {
				expect(mocks.deleteModelProvider).toHaveBeenCalledExactlyOnceWith('draft-1');
			}
		}
	);

	it('draft mode creates the placeholder row with the fixed endpoint, then starts', async () => {
		mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
		const onRow = vi.fn();
		const { findByText } = render(DeviceCodeSetup, { kind, onRow });
		await waitFor(() => expect(mocks[start]).toHaveBeenCalledWith('draft-1'));
		expect(mocks.createModelProvider).toHaveBeenCalledWith({ provider_type: type, name, base_url: base });
		expect(await findByText('ABCD-1234')).toBeTruthy();
		await waitFor(() => expect(onRow).toHaveBeenLastCalledWith('draft-1'));
	});

	it('resuming a draft row reuses it and still deletes it on Cancel', async () => {
		const onCancel = vi.fn();
		const { findByText } = render(DeviceCodeSetup, { kind, draftRowId: 'draft-7', onCancel });
		await fireEvent.click(await findByText('Cancel'));
		await waitFor(() => expect(onCancel).toHaveBeenCalled());
		expect(mocks.createModelProvider).not.toHaveBeenCalled();
		expect(mocks[start]).toHaveBeenCalledWith('draft-7');
		expect(mocks.deleteModelProvider).toHaveBeenCalledWith('draft-7');
	});

	it('a failed start offers Remove draft and Try again on the same row', async () => {
		mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
		mocks[start].mockRejectedValueOnce(new Error('upstream'));
		const { findByRole, getByRole } = render(DeviceCodeSetup, { kind });
		expect((await findByRole('alert')).textContent).toContain(`Could not start ${name} sign-in.`);
		await fireEvent.click(getByRole('button', { name: 'Try again' }));
		await waitFor(() => expect(mocks[start]).toHaveBeenCalledTimes(2));
		expect(mocks[start]).toHaveBeenLastCalledWith('draft-1');
		expect(mocks.createModelProvider).toHaveBeenCalledOnce();
	});

	it('a reconnect error closes without deleting', async () => {
		mocks[start].mockRejectedValueOnce(new Error('upstream'));
		const onCancel = vi.fn();
		const { findByRole } = render(DeviceCodeSetup, { kind, existingProviderId: 'prov-9', onCancel });
		await fireEvent.click(await findByRole('button', { name: 'Close' }));
		expect(onCancel).toHaveBeenCalled();
		expect(mocks.deleteModelProvider).not.toHaveBeenCalled();
	});

	describe('completion', () => {
		beforeEach(() => vi.useFakeTimers());
		afterEach(() => vi.useRealTimers());

		it('without a picker, seeds the roster onto a new row', async () => {
			mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
			const roster = [{ id: 'm-1', label: 'Model 1' }];
			mocks.fetchProviderModels.mockResolvedValue({ source: 'preset', models: roster });
			mocks[poll].mockResolvedValue({ status: 'complete' });
			const onDone = vi.fn();
			render(DeviceCodeSetup, { kind, onDone });
			await vi.advanceTimersByTimeAsync(0);
			await vi.advanceTimersByTimeAsync(3000);
			expect(mocks.updateModelProvider).toHaveBeenCalledWith('draft-1', { models: roster });
			expect(onDone).toHaveBeenCalled();
		});

		it('a reconnect never overwrites a curated roster', async () => {
			mocks.storeProviders = [{ id: 'prov-9', models: [{ id: 'm-1', label: 'Model 1' }] }];
			mocks[poll].mockResolvedValue({ status: 'complete' });
			const onDone = vi.fn();
			render(DeviceCodeSetup, { kind, existingProviderId: 'prov-9', onDone });
			await vi.advanceTimersByTimeAsync(0);
			await vi.advanceTimersByTimeAsync(3000);
			expect(mocks.fetchProviderModels).not.toHaveBeenCalled();
			expect(mocks.updateModelProvider).not.toHaveBeenCalled();
			expect(onDone).toHaveBeenCalled();
		});

		it('a new subscription continues to model selection without saving the catalog', async () => {
			mocks.createModelProvider.mockResolvedValue({ id: 'draft-1' });
			mocks[poll].mockResolvedValue({ status: 'complete' });
			const onDone = vi.fn();
			const onConnected = vi.fn();
			render(DeviceCodeSetup, { kind, onDone, onConnected });
			await vi.advanceTimersByTimeAsync(0);
			await vi.advanceTimersByTimeAsync(3000);
			expect(onConnected).toHaveBeenCalledWith('draft-1');
			expect(onDone).not.toHaveBeenCalled();
			expect(mocks.updateModelProvider).not.toHaveBeenCalled();
			expect(mocks.refreshProviders).toHaveBeenCalled();
		});

		it('widens the poll interval after slow_down', async () => {
			const e = new DeviceEnrollment({ kind, existingProviderId: 'prov-9' });
			mocks[poll].mockResolvedValueOnce({ status: 'slow_down' }).mockResolvedValue({ status: 'pending' });
			e.start();
			await vi.advanceTimersByTimeAsync(0);
			await vi.advanceTimersByTimeAsync(3000);
			expect(mocks[poll]).toHaveBeenCalledTimes(1);
			await vi.advanceTimersByTimeAsync(3000);
			expect(mocks[poll]).toHaveBeenCalledTimes(1);
			await vi.advanceTimersByTimeAsync(2000);
			expect(mocks[poll]).toHaveBeenCalledTimes(2);
			e.dispose();
		});
	});
});

describe('clipboard', () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	it.each(['missing', 'blocked'])('copies the sign-in code when the clipboard API is %s', async (mode) => {
		vi.stubGlobal(
			'navigator',
			mode === 'missing' ? {} : { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('blocked')) } }
		);
		const exec = vi.fn(() => {
			expect((document.activeElement as HTMLTextAreaElement).value).toBe(DEVICE.user_code);
			return true;
		});
		Object.defineProperty(document, 'execCommand', { value: exec, configurable: true });
		const { findByRole } = render(DeviceCodeSetup, { kind: 'codex', existingProviderId: 'prov-9' });
		await fireEvent.click(await findByRole('button', { name: 'Copy' }));
		expect(exec).toHaveBeenCalledWith('copy');
		expect(await findByRole('button', { name: 'Copied' })).toBeTruthy();
	});

	it('shows a copy failure while keeping sign-in available', async () => {
		vi.stubGlobal('navigator', {});
		Object.defineProperty(document, 'execCommand', { value: vi.fn(() => false), configurable: true });
		const { findByRole, queryByRole } = render(DeviceCodeSetup, { kind: 'xai', existingProviderId: 'prov-9' });
		await fireEvent.click(await findByRole('button', { name: 'Copy' }));
		expect((await findByRole('alert')).textContent).toContain('Could not copy');
		expect(queryByRole('button', { name: 'Copied' })).toBeNull();
		expect(await findByRole('link', { name: 'Open sign-in page' })).toBeTruthy();
	});
});
