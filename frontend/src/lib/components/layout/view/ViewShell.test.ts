import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { shellHistory } from '$lib/services/app-history.svelte';
import { page } from '$app/state';
import { layout, viewOwnsPanel } from '$lib/stores/layout.svelte';
import { platform } from '$lib/stores/platform.svelte';
import { viewPanel } from '$lib/stores/view-panel.svelte';
import ViewHarness from './test-fixtures/ViewHarness.svelte';
import { readFileSync } from 'node:fs';
const desktopStyles = readFileSync('src/app.css', 'utf8');

const mocks = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('$app/navigation', () => ({ goto: (_url: string, opts: { replace: boolean; state: App.PageState }) => { if (!opts.replace) mocks.push('', opts.state); } }));
beforeEach(() => {
	vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	layout.setSidebarOverlay(false); platform.set('desktop'); page.state = {}; mocks.push.mockReset(); shellHistory.start({}, { type: 'notes' });
});
afterEach(() => { cleanup(); shellHistory.stop(); platform.set('desktop'); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function resize(width: number) {
	vi.stubGlobal('innerWidth', width);
	layout.setSidebarOverlay(width <= 1080);
	window.dispatchEvent(new Event('resize'));
	await tick();
}

describe('ViewShell', () => {
	it('keeps an empty detail title band and covers shared desktop drag/no-drag regions', () => {
		const { container } = render(ViewHarness);
		expect(container.querySelector('.view-main-slot > .view-titlebar[aria-hidden="true"]')).toBeTruthy();
		const dragRule = desktopStyles.match(/html\.desktop :is\(\.sb-head[^}]+}/)?.[0] ?? '';
		for (const selector of ['.rail-titlebar', '.panel-header', '.p-head', '.view-titlebar', '.st-dtop', '.page-terminal > .bar']) expect(dragRule).toContain(selector);
		expect(dragRule).toContain('-webkit-app-region: drag');
		expect(desktopStyles).toMatch(/html\.desktop :is\(\.sidebar, \.view-shell, \.reopen-panel\) :is\(button, a, input, textarea, select, summary[^}]+-webkit-app-region: no-drag/s);
		expect(desktopStyles).toMatch(/:has\(> \.sidebar-slot:is\(\.floating, \.panel-overlay:not\(\.rail-only\)\)\)[^}]+-webkit-app-region: no-drag/s);
	});
	it('opens a newly picked item at the top of the detail, not where the last one was scrolled', async () => {
		const { container } = render(ViewHarness);
		await fireEvent.click(screen.getByRole('button', { name: 'Open detail' }));
		await tick();
		const slot = container.querySelector<HTMLElement>('.view-main-slot')!;
		const scrollTo = vi.fn();
		slot.scrollTo = scrollTo as unknown as typeof slot.scrollTo;
		await fireEvent.click(screen.getByRole('button', { name: 'Open detail' }));
		await tick();
		expect(scrollTo).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Open item B' }));
		await tick();
		expect(scrollTo).toHaveBeenCalledWith({ top: 0 });
	});
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
	it('leaving a detail does not pop its entry', async () => {
		platform.set('mobile');
		const back = vi.spyOn(history, 'back').mockImplementation(() => {});
		const view = render(ViewHarness);
		await fireEvent.click(screen.getByRole('button', { name: 'Open detail' }));
		await view.rerender({ active: false });
		expect(back).not.toHaveBeenCalled();
		expect(mocks.push).toHaveBeenCalledOnce();
	});
	it('offers content-first phone destinations without an invisible panel or history step', async () => {
		platform.set('mobile');
		const view = render(ViewHarness, { contentFirst: true });
		await tick();
		expect(view.container.querySelector('[data-content-first][data-pane="detail"]')).toBeTruthy();
		expect(view.container.querySelector('.inline-panel[hidden]')).toBeTruthy();
		expect((view.container.querySelector('.inline-panel') as HTMLElement).inert).toBe(true);
		expect(view.container.querySelector('.view-main-slot[hidden]')).toBeNull();
		expect(mocks.push).not.toHaveBeenCalled();
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
		if (mode === 'overlay') { vi.stubGlobal('innerWidth', 767); layout.setSidebarOverlay(true); }
		if (mode === 'mobile') platform.set('mobile');
		const view = render(ViewHarness, { host: mode !== 'no-host' });
		await tick();
		expect(viewPanel.panel).toBeNull();
		expect(view.container.querySelector('.inline-panel .view-panel')).toBeTruthy();
	});
	it('follows Back and Forward with selection intact; rotating writes no entry', async () => {
		platform.set('mobile');
		const view = render(ViewHarness);
		await tick();
		await fireEvent.click(screen.getByRole('button', { name: 'Open item B' }));
		await resize(1000); await resize(390);
		expect(mocks.push).toHaveBeenCalledOnce();
		const entry = mocks.push.mock.calls[0][1];
		expect(entry.shell.place.panel).toBe('b');
		shellHistory.land({ shell: { id: 'list', place: { type: 'notes', panel: null } } });
		await tick(); expect(view.container.querySelector('[data-pane="panel"]')).toBeTruthy();
		shellHistory.land(entry);
		await tick(); expect(view.container.querySelector('[data-pane="detail"]')).toBeTruthy();
		expect(mocks.push).toHaveBeenCalledOnce();
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
