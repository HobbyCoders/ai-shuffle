import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import WorkspaceSelector from '$lib/components/layout/WorkspaceSelector.svelte';
import { platform } from '$lib/stores/platform.svelte';
import '$lib/test-stubs/composer-dom';

vi.hoisted(() => vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) })));
vi.mock('$lib/components/AddProjectModal.svelte', () => ({ default: () => {} }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openFeaturePage: vi.fn() } }));
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { readable } = await import('svelte/store');
	return {
		appData: { loadClusterProjects: vi.fn() },
		projects: readable(Array.from({ length: 40 }, (_, i) => ({ id: `p${i + 1}`, name: `Project ${i + 1}` }))),
		clusterProjects: readable([]), profiles: readable([{ id: 'agent' }]),
		profilesLoaded: readable(true)
	};
});

const originalWidth = window.innerWidth;
const originalHeight = window.innerHeight;
beforeEach(() => {
	// The popup is a mobile-shell affordance; the viewport numbers stay because
	// the popup measures the viewport to place and size itself.
	platform.set('mobile');
	window.innerWidth = 390;
	window.innerHeight = 600;
	vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
		const popup = this.classList.contains('picker-dropdown');
		return { x: 35, y: popup ? 0 : 430, left: 35, right: 355, top: popup ? 0 : 430, bottom: popup ? 300 : 470, width: 320, height: popup ? 300 : 40, toJSON() {} };
	});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); platform.set('desktop'); });
afterAll(() => { window.innerWidth = originalWidth; window.innerHeight = originalHeight; vi.unstubAllGlobals(); });

describe('mobile workspace popup', () => {
	it('repositions on surface scroll, ignores catalog scroll, and dismisses offscreen with cleanup', async () => {
		const surface = document.createElement('div');
		surface.className = 'chat-view';
		document.body.append(surface);
		Object.defineProperty(surface, 'getBoundingClientRect', { value: () => ({ top: 0, bottom: 600 } as DOMRect) });
		const remove = vi.spyOn(surface, 'removeEventListener');
		const { container, unmount } = render(WorkspaceSelector, { target: surface, props: { selectedProject: null, onSelectProject: vi.fn() } });
		const trigger = screen.getByRole('button', { name: 'Choose a project' });
		await fireEvent.click(trigger);
		const anchor = trigger.closest('.picker-container')!;
		const rect = vi.fn();
		Object.defineProperty(anchor, 'getBoundingClientRect', { value: rect });
		rect.mockReturnValue({ top: 100, bottom: 140, left: 35, width: 320 } as DOMRect);
		const popup = container.querySelector('.picker-dropdown') as HTMLElement;
		await fireEvent.scroll(surface);
		expect(popup.style.getPropertyValue('--picker-top')).toBe('148px');
		rect.mockReturnValue({ top: -80, bottom: -40, left: 35, width: 320 } as DOMRect);
		await fireEvent.scroll(popup);
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		await fireEvent.scroll(surface);
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(remove).toHaveBeenCalledWith('scroll', expect.any(Function));
		unmount(); surface.remove();
	});
	it('keeps a long catalog selectable and closes after selecting its final project', async () => {
		const onSelectProject = vi.fn();
		render(WorkspaceSelector, { selectedProject: null, onSelectProject });
		const trigger = screen.getByRole('button', { name: 'Choose a project' });
		await fireEvent.click(trigger);
		await fireEvent.click(screen.getByRole('button', { name: 'Project 40' }));
		expect(onSelectProject).toHaveBeenCalledWith('p40', null);
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByText('Select your workspace')).toBeNull();
	});

	it('fits the popup inside a short viewport and restores desktop positioning when the shell is desktop', async () => {
		const { container } = render(WorkspaceSelector, { selectedProject: null, onSelectProject: vi.fn() });
		await fireEvent.click(screen.getByRole('button', { name: 'Choose a project' }));
		const popup = container.querySelector('.picker-dropdown') as HTMLElement;
		const top = parseFloat(popup.style.getPropertyValue('--picker-top'));
		expect(top).toBeGreaterThanOrEqual(12);
		expect(top + 300).toBeLessThanOrEqual(588);
		expect(parseFloat(popup.style.getPropertyValue('--selector-scroll-height'))).toBeGreaterThan(0);
		platform.set('desktop');
		window.innerWidth = 1280;
		await fireEvent.resize(window);
		expect(popup.classList.contains('mobile-popup')).toBe(false);
		expect(popup.style.getPropertyValue('--selector-scroll-height')).toBe('');
		expect(popup.hasAttribute('popover')).toBe(false);
	});

	it('dismisses with Escape and restores focus to the project trigger', async () => {
		render(WorkspaceSelector, { selectedProject: null, onSelectProject: vi.fn() });
		const trigger = screen.getByRole('button', { name: 'Choose a project' });
		await fireEvent.click(trigger);
		screen.getByRole('button', { name: 'Project 1' }).focus();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(document.activeElement).toBe(trigger);
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByText('Select your workspace')).toBeNull();
	});
});
