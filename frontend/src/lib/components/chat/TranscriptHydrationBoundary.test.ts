import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import type { Writable } from 'svelte/store';
import { activeSession, type ActiveSessionState } from '$lib/stores/active-session';
import Harness from './test-fixtures/TranscriptHydrationHarness.svelte';

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
	cleanup(); vi.useRealTimers();
	if (nativeInert) Object.defineProperty(HTMLElement.prototype, 'inert', nativeInert);
	else delete (HTMLElement.prototype as { inert?: boolean }).inert;
});

describe('transcript hydration presentation', () => {
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

	it('keeps the outgoing rows inert under grace while the composer and permissions bind to the real target', async () => {
		const surfaces: string[] = [];
		const view = render(Harness, { onSurface: surface => surfaces.push(surface) });
		const composer = view.getByRole('textbox') as HTMLInputElement;
		composer.focus(); composer.setSelectionRange(2, 2);
		const outgoing = view.getByText('Previous conversation');
		live.set(state('incoming', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(120); await tick();
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
		await vi.advanceTimersByTimeAsync(199); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		await vi.advanceTimersByTimeAsync(1); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		expect(outgoing.isConnected).toBe(true);
		expect(outgoing.closest('.transcript-presentation')?.classList.contains('is-concealed')).toBe(true);
		live.set(state('incoming', 'Incoming conversation')); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Incoming conversation').closest('[inert]')).toBeNull();
	});

	it('restarts grace for a newer target and ignores the obsolete timer after readiness', async () => {
		const view = render(Harness);
		live.set(state('middle', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(120);
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
		await vi.advanceTimersByTimeAsync(250); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.container.querySelectorAll('[inert]')).toHaveLength(1);
		expect(view.getByText('Previous conversation').closest('[inert]')).toBe(view.getByTestId('mirror-owner'));
	});

	it('does not flash old rows back in when an already-visible loader is superseded', async () => {
		const view = render(Harness);
		live.set(state('middle', '', true)); await tick();
		await vi.advanceTimersByTimeAsync(220); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		live.set(state('latest', '', true)); await tick();
		expect(view.getByText('Loading conversation…')).toBeTruthy();
		expect(view.getByText('Previous conversation').closest('.transcript-presentation')?.classList.contains('is-concealed')).toBe(true);
		live.set(state('latest', 'Latest conversation')); await tick();
		expect(view.queryByText('Loading conversation…')).toBeNull();
		expect(view.getByText('Latest conversation')).toBeTruthy();
	});

	it('reports the session and count actually painted, not the incoming empty vector', async () => {
		const frames: { surface: string; count: number; id: string | null | undefined }[] = [];
		render(Harness, { onSurface: (surface, count, id) => { frames.push({ surface, count, id }); } });
		live.set(state('incoming', '', true)); await tick();
		expect(frames.at(-1)).toEqual({ surface: 'active', count: 1, id: 'outgoing' });
		await vi.advanceTimersByTimeAsync(200); await tick();
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
});
