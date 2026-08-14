/**
 * `ensureModelRegistryHydrated()` — boot hydration with background retries.
 *
 * A page that boots during a deploy/server-restart window loses the single
 * `GET /models` and would otherwise keep the static Claude-only fallback
 * forever. These tests pin the retry chain: one immediate attempt, then a
 * [2s, 5s, 15s, 30s] backoff, a single chain at a time, and a cleared guard
 * afterwards so a later call (e.g. from the WS `onConnected` hook) can start
 * fresh.
 *
 * The registry keeps module-level `$state`, and the retry guard is a module
 * global, so each test re-imports the module fresh (`vi.resetModules()` +
 * dynamic import) instead of sharing one instance like models.providers.test.ts
 * does — the exhaustion case must start from an unhydrated registry.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mocks = vi.hoisted(() => ({
	get: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get }
}));

const OK_PAYLOAD = {
	models: [{ value: 'sonnet', label: 'Sonnet', family: 'sonnet', supports_1m: true }],
	default: 'sonnet',
	legacy_map: {},
	providers: []
};

/** Fresh module instance (registry state + retry guard) per test. */
async function loadRegistry() {
	vi.resetModules();
	return await import('./models.svelte');
}

let warnSpy: ReturnType<typeof vi.spyOn>;
let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
	mocks.get.mockReset();
	vi.useFakeTimers();
	warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
	errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
	vi.clearAllTimers();
	vi.useRealTimers();
	warnSpy.mockRestore();
	errorSpy.mockRestore();
});

describe('ensureModelRegistryHydrated', () => {
	it('retries in the background after a failed boot attempt and hydrates on the 2s retry', async () => {
		const registry = await loadRegistry();
		mocks.get.mockRejectedValueOnce(new Error('backend restarting'));

		// Resolves immediately (the caller is never blocked on the retry chain),
		// leaving the static fallback in place for now.
		await registry.ensureModelRegistryHydrated();
		expect(mocks.get).toHaveBeenCalledTimes(1);
		expect(registry.isRegistryHydrated()).toBe(false);

		mocks.get.mockResolvedValueOnce(OK_PAYLOAD);
		await vi.advanceTimersByTimeAsync(2000);

		expect(mocks.get).toHaveBeenCalledTimes(2);
		expect(registry.isRegistryHydrated()).toBe(true);
		expect(registry.getDefaultModel()).toBe('sonnet');
	});

	it('does not fetch again once the registry is hydrated', async () => {
		const registry = await loadRegistry();
		mocks.get.mockResolvedValueOnce(OK_PAYLOAD);

		await registry.ensureModelRegistryHydrated();
		expect(registry.isRegistryHydrated()).toBe(true);
		expect(mocks.get).toHaveBeenCalledTimes(1);

		await registry.ensureModelRegistryHydrated();
		expect(mocks.get).toHaveBeenCalledTimes(1);
	});

	it('gives up after the backoff schedule is exhausted, then allows a fresh chain', async () => {
		const registry = await loadRegistry();
		mocks.get.mockRejectedValue(new Error('backend down'));

		await registry.ensureModelRegistryHydrated();
		expect(mocks.get).toHaveBeenCalledTimes(1);

		// 2s + 5s + 15s + 30s of backoff → four more attempts, then it stops.
		await vi.advanceTimersByTimeAsync(2000 + 5000 + 15000 + 30000);

		expect(mocks.get).toHaveBeenCalledTimes(5);
		expect(registry.isRegistryHydrated()).toBe(false);
		expect(warnSpy).toHaveBeenCalledWith(
			'[model-registry] hydration retries exhausted, keeping static fallback'
		);

		// No further attempts once the chain has ended.
		await vi.advanceTimersByTimeAsync(120000);
		expect(mocks.get).toHaveBeenCalledTimes(5);

		// The guard is cleared, so a later call (WS reconnect) starts fresh.
		mocks.get.mockReset();
		mocks.get.mockRejectedValueOnce(new Error('still down'));
		await registry.ensureModelRegistryHydrated();
		expect(mocks.get).toHaveBeenCalledTimes(1);

		mocks.get.mockResolvedValueOnce(OK_PAYLOAD);
		await vi.advanceTimersByTimeAsync(2000);
		expect(mocks.get).toHaveBeenCalledTimes(2);
		expect(registry.isRegistryHydrated()).toBe(true);
	});
});
