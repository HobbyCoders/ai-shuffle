import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { get } from 'svelte/store';
import { activeSession } from '$lib/stores/active-session';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { getContextUsage } from '$lib/api/sessions';
import ControlsDock from './ControlsDock.svelte';
import '../../test-stubs/composer-dom';

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn((url: unknown) => String(url).includes('/events?')
		? new Promise(() => {})
		: Promise.resolve({ ok: false, status: 404, json: vi.fn().mockResolvedValue({}) })));
});
vi.mock('$lib/api/client', async (original) => ({
	...await original<Record<string, unknown>>(),
	getUsageLimits: vi.fn().mockResolvedValue({ available: false, buckets: {}, provider_buckets: {} })
}));
vi.mock('$lib/api/sessions', async (original) => ({
	...await original<Record<string, unknown>>(),
	getContextUsage: vi.fn().mockResolvedValue({ available: false, reason: 'not_connected' })
}));

beforeEach(() => {
	appMode.set('code');
	activeSession.newChat();
	vi.mocked(getContextUsage).mockClear();
});
afterEach(() => { cleanup(); activeSession.newChat(); });
afterAll(() => vi.unstubAllGlobals());

async function openSession(mode: 'code' | 'chat' | 'voice') {
	await activeSession.switchSession('context-fixture');
	await waitFor(() => expect(get(activeSession).sessionId).toBe('context-fixture'));
	activeSession.reconcileSessionMode([{ id: 'context-fixture', mode }]);
	expect(get(activeSession).mode).toBe(mode);
}
function mount(assistantMode = false) {
	return render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn(), assistantMode } });
}

describe('ControlsDock single responsive context producer', () => {
	for (const mode of ['code', 'chat', 'voice'] as const) {
		it.each([1280, 390])(`mounts one ring and reads once in ${mode} at %ipx`, async width => {
			Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
			await openSession(mode);
			const { container } = mount();
			await waitFor(() => expect(getContextUsage).toHaveBeenCalledTimes(1));
			expect(screen.getAllByRole('button', { name: /^Context and usage/ })).toHaveLength(1);
			const ring = container.querySelector('.usage-context-wrapper')!;
			expect(ring.closest('.dock-right')).not.toBeNull();
			// Only model/effort wrappers hide in the mobile shell, never the producer.
			expect(ring.closest('.desktop-controls')).toBeNull();
		});
	}
	it('retains the ring, focus and open popover across resize without remounting or rereading', async () => {
		await openSession('chat');
		const { container } = mount();
		await waitFor(() => expect(getContextUsage).toHaveBeenCalledTimes(1));
		const ring = screen.getByRole('button', { name: /^Context and usage/ });
		ring.focus();
		await fireEvent.click(ring);
		const popover = container.querySelector('.usage-popover');
		expect(popover).not.toBeNull();
		// Unavailable load permits one retry on open. Resizing must not trigger another.
		await waitFor(() => expect(getContextUsage).toHaveBeenCalledTimes(2));
		Object.defineProperty(window, 'innerWidth', { value: 390, configurable: true });
		await fireEvent(window, new Event('resize'));
		await tick();
		expect(screen.getByRole('button', { name: /^Context and usage/ })).toBe(ring);
		expect(container.querySelector('.usage-popover')).toBe(popover);
		expect(document.activeElement).toBe(ring);
		expect(getContextUsage).toHaveBeenCalledTimes(2);
		await fireEvent.keyDown(document, { key: 'Escape' });
		expect(container.querySelector('.usage-popover')).toBeNull();
	});
	it('never starts a context read in assistant mode', async () => {
		await openSession('chat');
		mount(true);
		await tick();
		expect(screen.queryByRole('button', { name: /^Context and usage/ })).toBeNull();
		expect(getContextUsage).not.toHaveBeenCalled();
	});
});
