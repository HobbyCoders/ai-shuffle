import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import ControlsDock from '$lib/components/chat/ControlsDock.svelte';
import { tick } from 'svelte';
import '$lib/test-stubs/composer-dom';
import { installMobileShell } from './register';

// The dock renders the controls sheet only once the mobile shell is registered.
installMobileShell();

vi.hoisted(() => {
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
		ok: false,
		status: 404,
		json: vi.fn().mockResolvedValue({})
	}));
});

vi.mock('$lib/api/client', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return {
		...actual,
		getUsageLimits: vi.fn().mockResolvedValue({
			available: false,
			buckets: {},
			provider_buckets: {}
		})
	};
});

afterEach(() => cleanup());
afterAll(() => vi.unstubAllGlobals());

describe('ControlsDock mobile control order', () => {
	it('uses the same exported setter for gestures, preserving outside focus and restoring panel focus', async () => {
		const outside = document.createElement('button');
		document.body.append(outside);
		outside.focus();
		const onSheetOpenChange = vi.fn();
		const { component, container } = render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn(), onSheetOpenChange } });
		const trigger = container.querySelector('.mobile-settings-trigger')!;
		component.setMobileSheetOpen(true);
		await tick();
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		expect(document.activeElement).toBe(outside);
		component.setMobileSheetOpen(false);
		await tick();
		expect(document.activeElement).toBe(outside);
		component.setMobileSheetOpen(true);
		await tick();
		(container.querySelector('.controls-panel') as HTMLElement).focus();
		component.setMobileSheetOpen(false);
		await tick();
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(document.activeElement).toBe(trigger);
		expect(onSheetOpenChange.mock.calls).toEqual([[true], [false], [true], [false]]);
		outside.remove();
	});
	it('toggles closed from settings even when the panel is scrolled', async () => {
		const onSheetOpenChange = vi.fn();
		const { container } = render(ControlsDock, { props: { isUploading: false, onAttachClick: vi.fn(), onSheetOpenChange } });
		const trigger = container.querySelector('.mobile-settings-trigger')!;
		await fireEvent.click(trigger);
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		const panel = container.querySelector('.controls-panel') as HTMLElement;
		panel.scrollTop = 160;
		(trigger as HTMLElement).focus();
		await fireEvent.click(trigger);
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		expect(screen.queryByRole('button', { name: 'Collapse chat controls' })).toBeNull();
		expect(document.activeElement).toBe(trigger);
		await fireEvent.click(trigger);
		expect(onSheetOpenChange).toHaveBeenLastCalledWith(true);
		panel.focus();
		await fireEvent.keyDown(panel, { key: 'Escape' });
		expect(document.activeElement).toBe(trigger);
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(trigger);
		expect(trigger.getAttribute('aria-expanded')).toBe('true');
		expect(onSheetOpenChange).toHaveBeenLastCalledWith(true);
	});

	it('places chat settings first, then browser immediately before attach, with usage on the right', () => {
		const { container } = render(ControlsDock, {
			props: {
				isUploading: false,
				onAttachClick: vi.fn(),
				onBrowserToggle: vi.fn()
			}
		});
		const dock = container.querySelector('.controls-dock');
		expect(dock instanceof HTMLElement && !dock.inert).toBe(true);
		const left = container.querySelector('.dock-left');
		const mobile = container.querySelector('.mobile-controls');
		const settings = left?.querySelector('.mobile-settings-trigger');
		const attach = left?.querySelector('.attach-btn');
		const contextUsage = mobile?.querySelector('.usage-context-wrapper');

		const browser = screen.getByRole('button', { name: 'Toggle live browser view' });
		expect(browser.nextElementSibling).toBe(attach);
		expect(browser.classList.contains('dock-ico')).toBe(true);
		expect(browser.classList.contains('desktop-only')).toBe(false);
		expect(left?.contains(browser)).toBe(true);
		expect(settings).toBeInstanceOf(Element);
		expect(attach).toBeInstanceOf(Element);
		expect(contextUsage).toBeInstanceOf(Element);
		if (
			!(settings instanceof Element) ||
			!(attach instanceof Element) ||
			!(contextUsage instanceof Element)
		) {
			return;
		}
		expect(settings.classList.contains('dock-ico')).toBe(true);
		expect(settings.classList.contains('composer-mobile-only')).toBe(true);
		expect(settings.classList.contains('mobile-only')).toBe(false);
		expect(settings.compareDocumentPosition(attach) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBeTruthy();
		expect(attach.compareDocumentPosition(contextUsage) & Node.DOCUMENT_POSITION_FOLLOWING)
			.toBeTruthy();
	});

	it('marks the dock inert when ChatInput is guarding the expand tap', () => {
		const { container } = render(ControlsDock, {
			props: {
				isUploading: false,
				onAttachClick: vi.fn(),
				inertToolbar: true
			}
		});
		const dock = container.querySelector('.controls-dock');
		expect(dock).toBeInstanceOf(HTMLElement);
		expect(dock instanceof HTMLElement && dock.inert).toBe(true);
	});
});
