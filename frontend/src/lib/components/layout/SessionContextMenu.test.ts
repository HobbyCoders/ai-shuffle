/**
 * Behavior tests for SessionContextMenu — the right-click / long-press menu.
 *
 * Covers the load-bearing behavior: open/close rendering, selection dispatch,
 * viewport clamping of the anchor position, keyboard navigation + Escape/Tab
 * dismissal, backdrop click-away, and active/disabled item handling.
 */
import { describe, it, expect, vi, afterEach, beforeAll } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import SessionContextMenu, { type ContextMenuItem } from './SessionContextMenu.svelte';

// jsdom lacks ResizeObserver, which Svelte's bind:clientWidth/Height relies on.
beforeAll(() => {
	if (!('ResizeObserver' in globalThis)) {
		(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver = class {
			observe() {}
			unobserve() {}
			disconnect() {}
		};
	}
});

function baseItems(over: Partial<Record<string, Partial<ContextMenuItem>>> = {}): ContextMenuItem[] {
	return [
		{ id: 'resume', label: 'Resume here', ...over.resume },
		{ id: 'fork', label: 'Fork', ...over.fork },
		{ id: 'favorite', label: 'Favorite', active: false, ...over.favorite },
		{ id: 'clear', label: 'Clear', danger: true, ...over.clear }
	];
}

function open(props: Partial<Parameters<typeof render>[1]> = {}) {
	return render(SessionContextMenu, {
		props: {
			open: true,
			x: 100,
			y: 100,
			items: baseItems(),
			onSelect: vi.fn(),
			onClose: vi.fn(),
			...props
		}
	});
}

afterEach(() => cleanup());

describe('SessionContextMenu', () => {
	it('renders nothing when closed', () => {
		const { container } = render(SessionContextMenu, {
			props: { open: false, x: 0, y: 0, items: baseItems(), onSelect: vi.fn(), onClose: vi.fn() }
		});
		expect(container.querySelector('.ctx-menu')).toBeNull();
		expect(container.querySelector('.ctx-backdrop')).toBeNull();
	});

	it('renders a menu with one menuitem per item when open', () => {
		open();
		expect(screen.getByRole('menu')).toBeTruthy();
		expect(screen.getAllByRole('menuitem')).toHaveLength(4);
		expect(screen.getByText('Resume here')).toBeTruthy();
		expect(screen.getByText('Clear')).toBeTruthy();
	});

	it('dispatches onSelect with the item id on click', async () => {
		const onSelect = vi.fn();
		open({ onSelect });
		await fireEvent.click(screen.getByText('Fork'));
		expect(onSelect).toHaveBeenCalledWith('fork');
	});

	it('shows a heading when provided', () => {
		open({ heading: 'My session' });
		expect(screen.getByText('My session')).toBeTruthy();
	});

	it('marks an active item and does not dispatch for a disabled item', async () => {
		const onSelect = vi.fn();
		open({
			onSelect,
			items: baseItems({ favorite: { active: true, label: 'Unfavorite' }, fork: { disabled: true } })
		});
		const fav = screen.getByText('Unfavorite').closest('button')!;
		expect(fav.classList.contains('is-active')).toBe(true);

		const fork = screen.getByText('Fork').closest('button')!;
		expect(fork.hasAttribute('disabled')).toBe(true);
		await fireEvent.click(fork);
		expect(onSelect).not.toHaveBeenCalled();
	});

	// --- Viewport clamping (jsdom window is 1024x768; unlaid-out menu is 0x0) ---
	it('keeps the menu inside the viewport when the anchor is near the right/bottom edge', () => {
		const { container } = open({ x: 5000, y: 5000 });
		const menu = container.querySelector('.ctx-menu') as HTMLElement;
		// Clamped to viewport - GAP(8): innerWidth 1024 -> 1016, innerHeight 768 -> 760.
		expect(menu.style.left).toBe('1016px');
		expect(menu.style.top).toBe('760px');
	});

	it('never positions the menu past the top-left gap', () => {
		const { container } = open({ x: -50, y: -50 });
		const menu = container.querySelector('.ctx-menu') as HTMLElement;
		expect(menu.style.left).toBe('8px');
		expect(menu.style.top).toBe('8px');
	});

	// --- Dismissal ---
	it('closes on Escape', async () => {
		const onClose = vi.fn();
		open({ onClose });
		await fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
		expect(onClose).toHaveBeenCalled();
	});

	it('closes on Tab (transient layer does not leak focus)', async () => {
		const onClose = vi.fn();
		open({ onClose });
		await fireEvent.keyDown(screen.getByRole('menu'), { key: 'Tab' });
		expect(onClose).toHaveBeenCalled();
	});

	it('closes when the backdrop is pressed (click-away)', async () => {
		const onClose = vi.fn();
		const { container } = open({ onClose });
		const backdrop = container.querySelector('.ctx-backdrop') as HTMLElement;
		await fireEvent.pointerDown(backdrop);
		expect(onClose).toHaveBeenCalled();
	});

	it('closes on scroll (the anchor would drift)', async () => {
		const onClose = vi.fn();
		open({ onClose });
		await fireEvent.scroll(window);
		expect(onClose).toHaveBeenCalled();
	});

	// --- Keyboard navigation ---
	it('focuses the first item on open and moves focus with ArrowDown, skipping disabled', async () => {
		open({ items: baseItems({ fork: { disabled: true } }) });
		const menu = screen.getByRole('menu');
		const items = screen.getAllByRole('menuitem');

		// First enabled item (resume) is focused on open (deferred via microtask).
		await vi.waitFor(() => expect(document.activeElement).toBe(items[0]));

		await fireEvent.keyDown(menu, { key: 'ArrowDown' });
		// fork is disabled → focus skips to favorite (index 2).
		expect(document.activeElement).toBe(items[2]);

		await fireEvent.keyDown(menu, { key: 'ArrowUp' });
		// Wraps back over the disabled fork to resume.
		expect(document.activeElement).toBe(items[0]);
	});
});
