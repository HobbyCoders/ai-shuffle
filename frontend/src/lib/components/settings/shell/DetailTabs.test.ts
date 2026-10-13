/** The detail tab strip: keyboard selection, keeping the active tab in view, wheel and drag scrolling. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import DetailTabs, { revealScrollLeft } from './DetailTabs.svelte';

const TABS = ['Profile', 'Instructions', 'Tools', 'Subagents', 'Models', 'Advanced'].map((label) => ({
	id: label.toLowerCase(),
	label
}));

afterEach(cleanup);

function setup(current = 'profile') {
	const onselect = vi.fn();
	const view = render(DetailTabs, { tabs: TABS, current, onselect, label: 'Agent sections', idPrefix: 't' });
	const strip = screen.getByRole('tablist', { name: 'Agent sections' }) as HTMLElement;
	return { ...view, onselect, strip };
}

/** jsdom has no layout: give the strip a scroll range. */
function overflow(strip: HTMLElement, scrollWidth = 600, clientWidth = 300) {
	Object.defineProperty(strip, 'scrollWidth', { configurable: true, value: scrollWidth });
	Object.defineProperty(strip, 'clientWidth', { configurable: true, value: clientWidth });
}

describe('keyboard', () => {
	it('moves and selects with the arrow keys, Home and End, wrapping at the ends', async () => {
		const { onselect } = setup('profile');
		const tab = (name: string) => screen.getByRole('tab', { name });
		await fireEvent.keyDown(tab('Profile'), { key: 'ArrowRight' });
		expect(onselect).toHaveBeenLastCalledWith('instructions');
		expect(document.activeElement).toBe(tab('Instructions'));
		await fireEvent.keyDown(tab('Profile'), { key: 'ArrowLeft' });
		expect(onselect).toHaveBeenLastCalledWith('advanced');
		await fireEvent.keyDown(tab('Profile'), { key: 'End' });
		expect(onselect).toHaveBeenLastCalledWith('advanced');
		await fireEvent.keyDown(tab('Profile'), { key: 'Home' });
		expect(onselect).toHaveBeenLastCalledWith('profile');
	});

	it('keeps one tab in the tab order: the selected one', () => {
		setup('tools');
		const tabs = screen.getAllByRole('tab');
		expect(tabs.filter((t) => t.tabIndex === 0).map((t) => t.textContent)).toEqual(['Tools']);
		expect(screen.getByRole('tab', { name: 'Tools' }).getAttribute('aria-selected')).toBe('true');
	});

	it('ignores other keys', async () => {
		const { onselect } = setup();
		await fireEvent.keyDown(screen.getByRole('tab', { name: 'Profile' }), { key: 'a' });
		expect(onselect).not.toHaveBeenCalled();
	});
});

describe('keeping the active tab in view', () => {
	it('scrolls left or right only as far as needed, with some room', () => {
		const view = { scrollLeft: 100, width: 300 };
		// Already visible.
		expect(revealScrollLeft({ left: 200, width: 60 }, view)).toBeNull();
		// Off to the left: show it with the pad, never past 0.
		expect(revealScrollLeft({ left: 90, width: 60 }, view)).toBe(58);
		expect(revealScrollLeft({ left: 10, width: 60 }, view)).toBe(0);
		// Off to the right.
		expect(revealScrollLeft({ left: 380, width: 60 }, view)).toBe(380 + 60 + 32 - 300);
	});
});

describe('wheel and drag', () => {
	it('turns a vertical wheel into sideways scroll while the strip can move', async () => {
		const { strip } = setup();
		overflow(strip);
		const event = new WheelEvent('wheel', { deltaY: 80, cancelable: true, bubbles: true });
		strip.dispatchEvent(event);
		expect(strip.scrollLeft).toBe(80);
		expect(event.defaultPrevented).toBe(true);
	});

	it('lets the page scroll once the strip is at its end', async () => {
		const { strip } = setup();
		overflow(strip);
		const up = new WheelEvent('wheel', { deltaY: -40, cancelable: true, bubbles: true });
		strip.dispatchEvent(up);
		expect(up.defaultPrevented).toBe(false);
	});

	it('does nothing when every tab fits', async () => {
		const { strip } = setup();
		overflow(strip, 300, 300);
		const event = new WheelEvent('wheel', { deltaY: 80, cancelable: true, bubbles: true });
		strip.dispatchEvent(event);
		expect(event.defaultPrevented).toBe(false);
	});

	it('drags with the mouse and swallows the click that ends the drag', async () => {
		const { strip, onselect } = setup();
		overflow(strip);
		strip.scrollLeft = 100;
		await fireEvent.pointerDown(strip, { pointerType: 'mouse', button: 0, clientX: 200, pointerId: 1 });
		await fireEvent.pointerMove(strip, { pointerType: 'mouse', clientX: 150, pointerId: 1 });
		expect(strip.scrollLeft).toBe(150);
		expect(strip.classList.contains('dragging')).toBe(true);
		await fireEvent.pointerUp(strip, { pointerType: 'mouse', pointerId: 1 });
		await fireEvent.click(screen.getByRole('tab', { name: 'Models' }));
		expect(onselect).not.toHaveBeenCalled();
		await tick();
	});

	it('shows the fade on the side with more tabs', async () => {
		const { strip, container } = setup();
		overflow(strip);
		await fireEvent.scroll(strip);
		const wrap = container.querySelector('.st-tabs-wrap')!;
		expect(wrap.classList.contains('more-r')).toBe(true);
		expect(wrap.classList.contains('more-l')).toBe(false);
		strip.scrollLeft = 300;
		await fireEvent.scroll(strip);
		expect(wrap.classList.contains('more-l')).toBe(true);
		expect(wrap.classList.contains('more-r')).toBe(false);
	});
});
