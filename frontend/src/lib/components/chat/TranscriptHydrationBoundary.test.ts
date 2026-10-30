import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Writable } from 'svelte/store';
import { activeSession, type ActiveSessionState } from '$lib/stores/active-session';
import Harness from './test-fixtures/TranscriptHydrationHarness.svelte';
import { TRANSCRIPT_LOADING_GRACE_MS as GRACE_MS, TRANSCRIPT_LOADING_MIN_VISIBLE_MS as MIN_VISIBLE_MS } from './TranscriptHydrationBoundary.svelte';

vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	return { activeSession: writable({}) };
});
const live = activeSession as unknown as Writable<ActiveSessionState>;
function state(id: string | null, content = '', loading = false): ActiveSessionState {
	return {
		sessionId: id,
		messages: content ? [{ id: `${id}-answer`, role: 'assistant', type: 'text', content }] : [],
		pendingPeerMessages: [], pendingPermissions: id ? [{ session_id: id }] : [],
		isLoadingSession: loading, isRestoringSession: false
	} as unknown as ActiveSessionState;
}
const nativeInert = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'inert');
beforeEach(() => {
	// jsdom has no native inert reflection; Chromium's real setter is covered
	// separately by the running-app journey.
	Object.defineProperty(HTMLElement.prototype, 'inert', {
		configurable: true,
		get(this: HTMLElement) { return this.hasAttribute('inert'); },
		set(this: HTMLElement, value: boolean) { this.toggleAttribute('inert', Boolean(value)); }
	});
	vi.useFakeTimers(); live.set(state('outgoing', 'Previous conversation'));
});
afterEach(() => {
	cleanup(); vi.useRealTimers(); vi.unstubAllEnvs();
	if (nativeInert) Object.defineProperty(HTMLElement.prototype, 'inert', nativeInert);
	else delete (HTMLElement.prototype as { inert?: boolean }).inert;
});

describe('transcript hydration presentation', () => {
	it('rolls back to the live empty/loading publication when the kill switch is disabled', async () => {
		vi.stubEnv('VITE_PERF_STABLE_TRANSCRIPT_LOADING', '0');
		const view = render(Harness);
		const outgoing = view.getByText('Previous conversation');
		live.set(state('incoming', '', true)); await tick();
		expect(outgoing.isConnected).toBe(false);
		expect(view.getByTestId('presented').dataset.session).toBe('incoming');
		expect(view.getByText('No messages')).toBeTruthy();
		expect(view.container.querySelector('[data-transcript-pending="true"]')).toBeNull();
		await vi.advanceTimersByTimeAsync(500); await tick();
		expect(view.container.querySelector('.hydration-overlay')).toBeNull();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('does not manufacture an outgoing presentation on a cold first load', async () => {
		live.set(state('incoming', '', true));
		const view = render(Harness);
		await vi.advanceTimersByTimeAsync(500); await tick();
		expect(view.getByTestId('presented').dataset.session).toBe('incoming');
		expect(view.container.querySelector('[data-transcript-pending="true"]')).toBeNull();
		expect(view.container.querySelector('.is-concealed')).toBeNull();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		live.set(state('incoming', 'First conversation')); await tick();
		expect(view.getByText('First conversation')).toBeTruthy();
	});

	it('snapshots the outgoing state shell even when the native store reuses its mutable object', async () => {
		const current = state('outgoing', 'Previous conversation');
		live.set(current);
		const view = render(Harness);
		Object.assign(current, state('incoming', '', true));
		live.set(current); await tick();
		expect(view.getByText('Previous conversation')).toBeTruthy();
		expect(view.getByTestId('presented').dataset.session).toBe('outgoing');
		Object.assign(current, state('incoming', 'Incoming conversation'));
		live.set(current); await tick();
		expect(view.getByText('Incoming conversation')).toBeTruthy();
		expect(view.queryByText('Previous conversation')).toBeNull();
	});

	it('cancels the pending grace timer when its mounted boundary is destroyed', async () => {
		const view = render(Harness);
		live.set(state('incoming', '', true)); await tick();
		expect(vi.getTimerCount()).toBeGreaterThan(0);
		await view.unmount(); await tick();
		expect(vi.getTimerCount()).toBe(0);
		live.set(state('latest', '', true)); await tick();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('keeps outgoing rows inert for a 300 ms short load while the composer and permissions bind to the real target', async () => {
		const surfaces: string[] = [];
		const view = render(Harness, { onSurface: surface => surfaces.push(surface) });
		const composer = view.getByRole('textbox') as HTMLInputElement;
		composer.focus(); composer.setSelectionRange(2, 2);
		const outgoing = view.getByText('Previous conversation');
		live.set(state('incoming', '', true)); await tick();
		// The real 4× short-history fixture takes up to 284 ms to hydrate.
		await vi.advanceTimersByTimeAsync(300); await tick();
		expect(outgoing.isConnected).toBe(true);
		expect(outgoing.closest('[inert]')).not.toBeNull();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(composer.dataset.session).toBe('incoming');
		expect(view.getByTestId('permissions').textContent).toBe('incoming');
		expect(surfaces).not.toContain('loading');
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.getByText('Incoming conversation')).toBeTruthy();
		expect(view.queryByText('Previous conversation')).toBeNull();
		expect(view.getByRole('textbox')).toBe(composer);
		expect(composer.selectionStart).toBe(2);
	});

	it('shows a late skeleton overlay without discarding the outgoing rows or their geometry', async () => {
		const view = render(Harness);
		const outgoing = view.getByText('Previous conversation');
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS - 1); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		await vi.advanceTimersByTimeAsync(1); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		expect(outgoing.isConnected).toBe(true);
		expect(outgoing.closest('.transcript-presentation')?.classList.contains('is-concealed')).toBe(true);
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Incoming conversation').closest('[inert]')).toBeNull();
	});

	it.each([false, true])('defers a first notice through the native drawer-close handoff (cached return: %s)', async (cachedReturn) => {
		const view = render(Harness, { navigationOverlayOpen: true });
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS + 50); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Previous conversation').closest('[inert]')).not.toBeNull();
		await view.rerender({ navigationOverlayOpen: false }); await tick();
		// openSession selects in the next rAF, before this presentation handoff.
		expect(view.queryByText('Loading conversation…')).toBeNull();
		if (cachedReturn) { live.set(state('outgoing', 'Previous conversation')); await tick(); }
		await vi.advanceTimersByTimeAsync(32); await tick();
		if (cachedReturn) {
			expect(view.queryByText('Loading conversation…')).toBeNull();
			expect(view.getByText('Previous conversation').closest('[inert]')).toBeNull();
		} else {
			// Dismissing the drawer without navigating reveals the already-due
			// notice immediately after the handoff, not after another 350 ms.
			expect(view.getByText('Loading conversation…')).toBeTruthy();
		}
	});

	it('does not hide an already-visible slow notice when the drawer opens or closes', async () => {
		const view = render(Harness);
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		await view.rerender({ navigationOverlayOpen: true }); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		await view.rerender({ navigationOverlayOpen: false }); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
	});

	it('restarts grace for a newer target and ignores the obsolete timer after readiness', async () => {
		const view = render(Harness);
		live.set(state('middle', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS - 50);
		live.set(state('latest', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(150); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Previous conversation')).toBeTruthy();
		live.set(state('latest', 'Latest conversation')); await tick();
		await vi.advanceTimersByTimeAsync(1000); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByTestId('presented').dataset.session).toBe('latest');
	});

	it('delegates inertness and the loading notice to the remote gate while it is blocking', async () => {
		const view = render(Harness, { mirrorPending: true });
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS + 50); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.container.querySelectorAll('[inert]')).toHaveLength(1);
		expect(view.getByText('Previous conversation').closest('[inert]')).toBe(view.getByTestId('mirror-owner'));
	});

	it('cancels a shown notice on navigation and gives an unloaded new target its own grace', async () => {
		const view = render(Harness);
		live.set(state('middle', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS + 20); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		live.set(state('latest', '', true)); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Previous conversation').closest('.transcript-presentation')?.classList.contains('is-concealed')).toBe(false);
		await vi.advanceTimersByTimeAsync(GRACE_MS - 1); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		await vi.advanceTimersByTimeAsync(1); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		live.set(state('latest', 'Latest conversation')); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Latest conversation')).toBeTruthy();
	});

	it('publishes and renders ready messages immediately but holds a painted notice for its minimum', async () => {
		const surfaces: string[] = [];
		const view = render(Harness, { onSurface: surface => surfaces.push(surface) });
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		vi.advanceTimersToNextFrame(); await tick();
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.getByText('Incoming conversation')).toBeTruthy();
		expect(view.queryByText('Previous conversation')).toBeNull();
		expect(view.getByRole('textbox').dataset.session).toBe('incoming');
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		expect(view.getByText('Incoming conversation').closest('[inert]')).not.toBeNull();
		expect(surfaces.at(-1)).toBe('loading');
		await vi.advanceTimersByTimeAsync(50); await tick();
		live.set(state('incoming', 'Incoming conversation updated')); await tick();
		// Neither streaming/native publications nor their DOM work reset the minimum.
		await vi.advanceTimersByTimeAsync(MIN_VISIBLE_MS - 51); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		await vi.advanceTimersByTimeAsync(1); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Incoming conversation updated').closest('[inert]')).toBeNull();
		expect(surfaces.at(-1)).toBe('active');
	});

	it('does not impose a minimum if ready before the notice reaches a frame', async () => {
		const view = render(Harness);
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Incoming conversation').closest('[inert]')).toBeNull();
		await vi.advanceTimersByTimeAsync(MIN_VISIBLE_MS); await tick();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('cancels the minimum immediately for a cached return and ignores the obsolete removal timer', async () => {
		const view = render(Harness);
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		vi.advanceTimersToNextFrame(); await tick();
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		live.set(state('outgoing', 'Previous conversation')); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Previous conversation').closest('[inert]')).toBeNull();
		await vi.advanceTimersByTimeAsync(MIN_VISIBLE_MS); await tick();
		expect(view.getByTestId('presented').dataset.session).toBe('outgoing');
		expect(vi.getTimerCount()).toBe(0);
	});

	it('keeps the notice minimum for an empty outcome without retaining the old messages', async () => {
		const surfaces: string[] = [];
		const view = render(Harness, { onSurface: surface => surfaces.push(surface) });
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		vi.advanceTimersToNextFrame(); await tick();
		live.set(state('incoming')); await tick();
		expect(view.queryByText('Previous conversation')).toBeNull();
		expect(view.getByText('No messages')).toBeTruthy();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		await vi.advanceTimersByTimeAsync(MIN_VISIBLE_MS); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(surfaces.at(-1)).toBe('cleared');
	});

	it('does not let a completed-load removal timer hide a restarted same-chat load', async () => {
		const view = render(Harness);
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		vi.advanceTimersToNextFrame(); await tick();
		live.set(state('incoming', 'Incoming conversation')); await tick();
		await vi.advanceTimersByTimeAsync(50); await tick();
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(MIN_VISIBLE_MS); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		expect(view.getByText('Incoming conversation')).toBeTruthy();
		live.set(state('incoming', 'Reloaded conversation')); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Reloaded conversation')).toBeTruthy();
	});

	it('cleans up a pending minimum on destruction', async () => {
		const view = render(Harness);
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		vi.advanceTimersToNextFrame(); await tick();
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		await view.unmount(); await tick();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('reports the session and count actually painted, not the incoming empty vector', async () => {
		const frames: { surface: string; count: number; id: string | null | undefined }[] = [];
		render(Harness, { onSurface: (surface, count, id) => { frames.push({ surface, count, id }); } });
		live.set(state('incoming', '', true)); await tick();
		expect(frames.at(-1)).toEqual({ surface: 'active', count: 1, id: 'outgoing' });
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		expect(frames.at(-1)).toEqual({ surface: 'loading', count: 0, id: 'incoming' });
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(frames.at(-1)).toEqual({ surface: 'active', count: 1, id: 'incoming' });
	});

	it('does not leave old content behind when the target settles empty or the chat is cleared', async () => {
		const view = render(Harness);
		live.set(state('empty', '', true)); await tick();
		live.set(state('empty')); await tick();
		expect(view.queryByText('Previous conversation')).toBeNull();
		expect(view.getByTestId('presented').dataset.session).toBe('empty');
		live.set(state(null)); await tick();
		await vi.advanceTimersByTimeAsync(500); await tick();
		expect(view.getByTestId('presented').dataset.session).toBeUndefined();
		expect(view.queryByText('Loading conversation…')).toBeNull();
	});

	it('(#913) keeps the retained rows and offers Retry when the load is released without any history', async () => {
		const reloadSession = vi.fn();
		Object.assign(live, { reloadSession });
		const surfaces: string[] = [];
		const view = render(Harness, { onSurface: surface => surfaces.push(surface) });
		const outgoing = view.getByText('Previous conversation');
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(GRACE_MS); await tick();
		vi.advanceTimersToNextFrame(); await tick();
		// The feed's ceiling: loading ends (sends stop waiting), nothing arrived.
		live.set({ ...state('incoming'), historyStall: 'timeout' }); await tick();
		await vi.advanceTimersByTimeAsync(MIN_VISIBLE_MS * 4); await tick();
		expect(outgoing.isConnected).toBe(true);
		expect(view.queryByText('No messages')).toBeNull();
		expect(view.getByText('This is taking longer than usual.')).toBeTruthy();
		expect(surfaces).not.toContain('cleared');
		expect(surfaces.at(-1)).toBe('loading');
		view.getByRole('button', { name: 'Retry loading the conversation' }).click();
		expect(reloadSession).toHaveBeenCalledTimes(1);
		// A failed request says so instead, in the same slot.
		live.set({ ...state('incoming'), historyStall: 'error' }); await tick();
		expect(view.getByText('Could not load the conversation.')).toBeTruthy();
		// The late history paints in place of the retained rows.
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.getByText('Incoming conversation').closest('[inert]')).toBeNull();
		expect(view.queryByText('Previous conversation')).toBeNull();
		expect(view.queryByText('Could not load the conversation.')).toBeNull();
		expect(surfaces).not.toContain('cleared');
		expect(surfaces.at(-1)).toBe('active');
	});
});
