/** The detail tab strip: keyboard selection, keeping the active tab in view, wheel and drag scrolling. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import DetailTabs, { partialTabs, revealScrollLeft, snapRevealScrollLeft, tabGap, wholeTabsEnd, TAB_GAP, TAB_GAP_TIGHT } from './DetailTabs.svelte';

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

describe('counts', () => {
	it('renders a count as its own badge after the label, and none for an empty count', () => {
		render(DetailTabs, {
			tabs: [{ id: 'tools', label: 'Tools', count: 121 }, { id: 'models', label: 'Models', count: '' }],
			current: 'tools',
			onselect: vi.fn(),
			label: 'Agent sections',
			idPrefix: 't'
		});
		const badge = screen.getByRole('tab', { name: /Tools/ }).querySelector('.n');
		expect(badge?.textContent).toBe('121');
		expect(badge?.previousSibling?.textContent).toBe('Tools');
		expect(screen.getByRole('tab', { name: 'Models' }).querySelector('.n')).toBeNull();
	});
});

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

describe('disabled tabs', () => {
	it('keeps locked project tabs visible but skips them with the keyboard', async () => {
		const onselect = vi.fn();
		render(DetailTabs, { tabs: [{ id: 'general', label: 'General', disabled: true }, { id: 'workers', label: 'Workers' }], current: 'workers', onselect, label: 'Project settings', idPrefix: 'p' });
		expect((screen.getByRole('tab', { name: 'General' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.keyDown(screen.getByRole('tab', { name: 'Workers' }), { key: 'Home' });
		expect(onselect).toHaveBeenLastCalledWith('workers');
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

	describe('lands on a snap position, so the mandatory snap cannot pull the tab back under the fade', () => {
		// Tabs 0..50, 70..140, 160..230, 250..340, 360..420 in a 300px strip; snap inset 24 (the fade).
		const tabs = [{ left: 0, width: 50 }, { left: 70, width: 70 }, { left: 160, width: 70 }, { left: 250, width: 90 }, { left: 360, width: 60 }];
		const max = 420 - 300;

		it('moving right, picks the first snap at or past the room the tab needs', () => {
			// The half-visible fourth tab needs scrollLeft >= 72 (340 + 32 - 300). The
			// nearest snap (46, the second tab's) would leave it under the fade; the
			// next one is the end of the strip.
			expect(snapRevealScrollLeft(tabs, 3, { scrollLeft: 0, width: 300 }, max, 24)).toBe(120);
			// The tab then ends inside the strip with room to spare.
			expect(340 - 120).toBeLessThanOrEqual(300 - 32);
			// A tab that needs only a little room takes the first snap that gives it.
			expect(snapRevealScrollLeft(tabs, 2, { scrollLeft: 0, width: 230 }, 420 - 230, 24)).toBe(46);
		});

		it('moving left, puts the tab just clear of the leading fade (its own snap)', () => {
			expect(snapRevealScrollLeft(tabs, 1, { scrollLeft: 120, width: 300 }, max, 24)).toBe(46);
			expect(snapRevealScrollLeft(tabs, 0, { scrollLeft: 120, width: 300 }, max, 24)).toBe(0);
		});

		it('leaves a tab that already fits alone, and keeps a tab wider than the strip starting in view', () => {
			expect(snapRevealScrollLeft(tabs, 1, { scrollLeft: 0, width: 300 }, max, 24)).toBeNull();
			const wide = [{ left: 0, width: 80 }, { left: 100, width: 250 }];
			expect(snapRevealScrollLeft(wide, 1, { scrollLeft: 0, width: 200 }, 150, 24)).toBe(76);
		});
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
		expect(container.querySelector('.tabs-more svg')?.getAttribute('width')).toBe('12');
		expect(wrap.classList.contains('more-l')).toBe(false);
		strip.scrollLeft = 300;
		await fireEvent.scroll(strip);
		expect(wrap.classList.contains('more-l')).toBe(true);
		expect(wrap.classList.contains('more-r')).toBe(false);
		expect(container.querySelector('.tabs-more')).toBeNull();
	});
});

describe('never a cut label', () => {
	it('keeps the wide gap while the tabs fit, and tightens it only when they would not', () => {
		const widths = [40, 70, 32, 62, 44, 58];
		const natural = widths.reduce((a, b) => a + b, 0) + 5 * TAB_GAP;
		expect(tabGap(widths, natural)).toBe(TAB_GAP);
		expect(tabGap(widths, natural - 1)).toBe(TAB_GAP_TIGHT);
	});

	it('finds the tabs that are only partly inside the strip', () => {
		const tabs = [{ left: 0, width: 50 }, { left: 70, width: 60 }, { left: 150, width: 80 }, { left: 250, width: 40 }];
		// Visible 0..200: the third tab (150..230) is cut, the fourth is fully past the edge.
		expect(partialTabs(tabs, { scrollLeft: 0, width: 200 })).toEqual([2]);
		// Visible 100..300: the second tab (70..130) is cut on the left.
		expect(partialTabs(tabs, { scrollLeft: 100, width: 200 })).toEqual([1]);
		expect(partialTabs(tabs, { scrollLeft: 0, width: 400 })).toEqual([]);
	});

	it('hides a cut tab at rest but never the selected one', async () => {
		const { strip } = setup('subagents');
		overflow(strip);
		const els = Array.from(strip.querySelectorAll<HTMLElement>('[data-tab-id]'));
		els.forEach((el, i) => {
			Object.defineProperty(el, 'offsetLeft', { configurable: true, value: i * 100 });
			Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 80 });
		});
		// Visible 0..250: Tools (200..280) is cut; Subagents (300..380) is selected.
		await fireEvent.scroll(strip);
		await new Promise((r) => setTimeout(r, 200));
		await tick();
		expect(screen.getByRole('tab', { name: 'Tools' }).hasAttribute('data-cut')).toBe(false);
		Object.defineProperty(strip, 'clientWidth', { configurable: true, value: 250 });
		strip.scrollLeft = 0;
		await fireEvent.scroll(strip);
		// While moving, every tab shows.
		expect(screen.getByRole('tab', { name: 'Tools' }).hasAttribute('data-cut')).toBe(false);
		await new Promise((r) => setTimeout(r, 200));
		await tick();
		expect(screen.getByRole('tab', { name: 'Tools' }).hasAttribute('data-cut')).toBe(true);
		expect(screen.getByRole('tab', { name: 'Profile' }).hasAttribute('data-cut')).toBe(false);
		// The selected tab is never marked cut.
		expect(screen.getByRole('tab', { name: 'Subagents' }).hasAttribute('data-cut')).toBe(false);
	});
});

describe('chevron right after the last whole tab', () => {
	it('finds where the last whole tab ends in the visible strip', () => {
		const tabs = [{ left: 0, width: 50 }, { left: 70, width: 60 }, { left: 150, width: 80 }, { left: 250, width: 40 }];
		// Visible 0..200: the second tab ends at 130; the cut third tab does not count.
		expect(wholeTabsEnd(tabs, { scrollLeft: 0, width: 200 })).toBe(130);
		// Scrolled to 70: the visible strip starts at the second tab; the third ends at 230 - 70.
		expect(wholeTabsEnd(tabs, { scrollLeft: 70, width: 200 })).toBe(160);
		expect(wholeTabsEnd(tabs, { scrollLeft: 0, width: 40 })).toBeNull();
	});

	it('puts the fade and chevron where the whole tabs end, not at the far edge', async () => {
		const { strip, container } = setup();
		overflow(strip, 600, 250);
		Array.from(strip.querySelectorAll<HTMLElement>('[data-tab-id]')).forEach((el, i) => {
			Object.defineProperty(el, 'offsetLeft', { configurable: true, value: i * 100 });
			Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 80 });
		});
		await fireEvent.scroll(strip);
		// Visible 0..250: Instructions (100..180) is the last whole tab, Tools (200..280) is cut.
		const wrap = container.querySelector<HTMLElement>('.st-tabs-wrap')!;
		expect(wrap.style.getPropertyValue('--tab-cut')).toBe('180px');
		expect(container.querySelector('.tabs-more')).not.toBeNull();
	});
});
