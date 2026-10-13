/**
 * ControlsDock — the usage popover re-reads the cached usage rows when the
 * session's own turn ends while it is open (provider quotas such as Codex
 * headers are captured from that turn's responses and are not broadcast).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import ControlsDock from './ControlsDock.svelte';
import '../../test-stubs/composer-dom';
import { api, getUsageLimits } from '$lib/api/client';
import { getModels, hydrateModelRegistry } from '$lib/constants/models';

const streaming = vi.hoisted(() => {
	let value = false;
	const listeners = new Set<() => void>();
	return {
		get value() {
			return value;
		},
		set(next: boolean) {
			value = next;
			listeners.forEach((fn) => fn());
		},
		listeners,
	};
});

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: vi.fn().mockResolvedValue({}) }));
});

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, getUsageLimits: vi.fn() };
});

// The real active session, with `isStreaming` driven by the test.
vi.mock('$lib/stores/active-session', async (importOriginal) => {
	const actual = await importOriginal<Record<string, any>>();
	const real = actual.activeSession;
	const view = (state: Record<string, unknown>) => ({ ...state, isStreaming: streaming.value });
	const activeSession = new Proxy(real, {
		get(target, prop, receiver) {
			if (prop === 'current') return view(target.current);
			if (prop === 'subscribe') {
				return (fn: (v: unknown) => void) => {
					let last: Record<string, unknown> = target.current;
					const unsubscribe = target.subscribe((v: Record<string, unknown>) => {
						last = v;
						fn(view(v));
					});
					const onToggle = () => fn(view(last));
					streaming.listeners.add(onToggle);
					return () => {
						streaming.listeners.delete(onToggle);
						unsubscribe();
					};
				};
			}
			return Reflect.get(target, prop, receiver);
		},
	});
	return { ...actual, activeSession };
});

const snapshot = { windows: [], available: false, provider_refreshing: false, providers: [] };

beforeEach(async () => {
	const registryGet = vi.spyOn(api, 'get').mockResolvedValue({
		models: getModels(), providers: [], claude: { auth_kind: 'subscription', plan_type: null },
	});
	await hydrateModelRegistry();
	registryGet.mockRestore();
	vi.mocked(getUsageLimits).mockReset();
	vi.mocked(getUsageLimits).mockResolvedValue(snapshot);
	streaming.set(false);
});
afterEach(() => cleanup());

async function settle() {
	for (let i = 0; i < 5; i++) await tick();
	await new Promise((r) => setTimeout(r, 0));
}

describe('ControlsDock usage refresh at turn end', () => {
	it('re-reads the cached rows (no forced refresh) when a turn ends with the popover open', async () => {
		const view = render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });
		await settle();
		await fireEvent.click(view.container.querySelector('.usage-indicator')!);
		await settle();
		const before = vi.mocked(getUsageLimits).mock.calls.length;
		streaming.set(true);
		await settle();
		expect(getUsageLimits).toHaveBeenCalledTimes(before);
		streaming.set(false);
		await settle();
		expect(getUsageLimits).toHaveBeenCalledTimes(before + 1);
		expect(getUsageLimits).toHaveBeenLastCalledWith(false);
	});

	it('does not fetch at turn end while the popover is closed', async () => {
		render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn() } });
		await settle();
		const before = vi.mocked(getUsageLimits).mock.calls.length;
		streaming.set(true);
		await settle();
		streaming.set(false);
		await settle();
		expect(getUsageLimits).toHaveBeenCalledTimes(before);
	});
});
