import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { page } from '$app/state';
import { layout, viewOwnsPanel } from '$lib/stores/layout.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { viewPanel } from '$lib/stores/view-panel.svelte';
import ViewHarness from './test-fixtures/ViewHarness.svelte';

const mocks = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('$app/navigation', () => ({ pushState: mocks.push, goto: vi.fn() }));
beforeEach(() => { layout.setSidebarOverlay(false); platform.set('desktop'); page.state = {}; mocks.push.mockReset(); });
afterEach(() => { cleanup(); platform.set('desktop'); vi.restoreAllMocks(); });

describe('ViewShell', () => {
	it('bridges feature, row and live Settings access contexts into the docked snippet', async () => {
		const view = render(ViewHarness);
		await tick();
		const host = view.container.querySelector('[data-test-host]')!;
		const control = screen.getByRole('switch', { name: 'Bridged switch' }) as HTMLButtonElement;
		expect(host.contains(control)).toBe(true);
		expect(control.disabled).toBe(true);
		expect(host.querySelector('[data-probe-active="true"]')).toBeTruthy();
		expect(host.querySelector('.settings-shell.view-panel')).toBeTruthy();
		await view.rerender({ readOnly: false });
		expect(control.disabled).toBe(false);
		await fireEvent.click(control);
		expect(control.getAttribute('aria-checked')).toBe('true');
	});
	it('releases its claim off screen and reclaims only when active', async () => {
		const view = render(ViewHarness);
		await tick();
		expect(viewPanel.panel).not.toBeNull();
		await view.rerender({ active: false });
		expect(viewPanel.panel).toBeNull();
		await view.rerender({ active: true });
		expect(viewPanel.panel).not.toBeNull();
		view.unmount();
		expect(viewPanel.panel).toBeNull();
		expect(viewPanel.hostMounted).toBe(false);
	});
	it.each(['no-host', 'overlay', 'mobile'])('renders inline without a claim: %s', async mode => {
		if (mode === 'overlay') layout.setSidebarOverlay(true);
		if (mode === 'mobile') platform.set('mobile');
		const view = render(ViewHarness, { host: mode !== 'no-host' });
		await tick();
		expect(viewPanel.panel).toBeNull();
		expect(view.container.querySelector('.inline-panel .view-panel')).toBeTruthy();
	});
	it('pushes the phone detail and follows browser Back without a duplicate history step', async () => {
		platform.set('mobile');
		const back = vi.spyOn(history, 'back').mockImplementation(() => {});
		render(ViewHarness);
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: 'Open detail' }));
		expect(mocks.push).toHaveBeenCalledOnce();
		const state = mocks.push.mock.calls[0][1];
		page.state = state;
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: 'Back to panel' }));
		expect(back).toHaveBeenCalledOnce();
		page.state = {};
		await tick();
		expect(screen.getByRole('button', { name: 'Open detail' })).toBeTruthy();
		expect(back).toHaveBeenCalledOnce();
	});
	it('keeps a newer token claim when an older page releases', () => {
		const older = Symbol(), newer = Symbol();
		const snippet = (() => {}) as unknown as NonNullable<typeof viewPanel.panel>;
		viewPanel.claim(older, snippet); viewPanel.claim(newer, snippet); viewPanel.release(older);
		expect(viewPanel.panel).toBe(snippet);
		viewPanel.release(newer); expect(viewPanel.panel).toBeNull();
	});
	it('treats the retired board as Chats, every other rail destination owns a panel', () => {
		expect(viewOwnsPanel('session')).toBe(false); expect(viewOwnsPanel('board')).toBe(false);
		expect(viewOwnsPanel(null)).toBe(false); expect(viewOwnsPanel('build')).toBe(true);
		expect(viewOwnsPanel('notes')).toBe(true); expect(viewOwnsPanel('settings')).toBe(true);
	});
});
