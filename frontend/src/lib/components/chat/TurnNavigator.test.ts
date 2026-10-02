import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TurnNavigator from './TurnNavigator.svelte';

const items = [
	{ id: 'one', label: 'Set up the project', title: 'Set up the project', preview: '' },
	{
		id: 'two',
		label: 'Fix the transcript',
		title: 'Fix the transcript',
		preview: 'Details about the bug'
	},
	{ id: 'three', label: 'Run verification', title: 'Run verification', preview: '' }
];

afterEach(cleanup);

describe('TurnNavigator', () => {
	it('renders the same rail of ticks for long histories, with no second control set', async () => {
		const onSelect = vi.fn();
		const many = Array.from({ length: 1000 }, (_, i) => ({ id: String(i), label: `Turn ${i}`, title: `Turn ${i}`, preview: '' }));
		const { container } = render(TurnNavigator, { props: { items: many, currentIndex: 500, onSelect } });
		const ticks = screen.getAllByRole('button');
		expect(ticks).toHaveLength(1000);
		expect(ticks.every((tick) => tick.classList.contains('tick'))).toBe(true);
		expect(screen.queryByRole('searchbox')).toBeNull();
		expect(screen.queryByRole('button', { name: /First user turn|Browse session timeline/ })).toBeNull();
		expect(container.querySelector('.long-history')).toBeNull();
		expect(screen.getByRole('button', { name: 'User message 501 of 1000: Turn 500' }).getAttribute('aria-current')).toBe('true');
		await fireEvent.click(screen.getByRole('button', { name: 'User message 1 of 1000: Turn 0' }));
		expect(onSelect).toHaveBeenLastCalledWith(0);
		await fireEvent.click(screen.getByRole('button', { name: 'User message 1000 of 1000: Turn 999' }));
		expect(onSelect).toHaveBeenLastCalledWith(999);
	});

	it('moves focus with Home, End and arrows so keyboard users stay on a visible tick', async () => {
		const onSelect = vi.fn();
		const many = Array.from({ length: 45 }, (_, i) => ({ id: String(i), label: `Prompt ${i}`, title: `Prompt ${i}`, preview: '' }));
		render(TurnNavigator, { props: { items: many, currentIndex: 20, onSelect } });
		const tickNamed = (n: number) => screen.getByRole('button', { name: `User message ${n} of 45: Prompt ${n - 1}` });
		tickNamed(21).focus();
		await fireEvent.keyDown(tickNamed(21), { key: 'End' });
		expect(onSelect).toHaveBeenLastCalledWith(44);
		expect(document.activeElement).toBe(tickNamed(45));
		await fireEvent.keyDown(tickNamed(45), { key: 'ArrowUp' });
		expect(onSelect).toHaveBeenLastCalledWith(43);
		expect(document.activeElement).toBe(tickNamed(44));
		await fireEvent.keyDown(tickNamed(44), { key: 'Home' });
		expect(onSelect).toHaveBeenLastCalledWith(0);
		expect(document.activeElement).toBe(tickNamed(1));
	});

	it('puts one tick in the Tab order: the focused tick, else the current turn', async () => {
		const many = Array.from({ length: 300 }, (_, i) => ({ id: String(i), label: `Prompt ${i}`, title: `Prompt ${i}`, preview: '' }));
		const view = render(TurnNavigator, { props: { items: many, currentIndex: 120, onSelect: vi.fn() } });
		const tabStops = () => screen.getAllByRole('button').filter((tick) => tick.tabIndex === 0);
		const tickNamed = (n: number) => screen.getByRole('button', { name: `User message ${n} of 300: Prompt ${n - 1}` });
		expect(tabStops()).toEqual([tickNamed(121)]);
		await fireEvent.focus(tickNamed(10));
		expect(tabStops()).toEqual([tickNamed(10)]);
		await fireEvent.blur(tickNamed(10));
		await view.rerender({ items: many, currentIndex: 299, onSelect: vi.fn() });
		expect(tabStops()).toEqual([tickNamed(300)]);
	});

	it('drops focus after a touch tap so the tapped tick does not stay lit', async () => {
		const onSelect = vi.fn();
		render(TurnNavigator, { props: { items, currentIndex: 0, onSelect } });
		const tapped = screen.getByRole('button', { name: 'User message 2 of 3: Fix the transcript' });
		tapped.focus();
		await fireEvent.pointerDown(tapped, { pointerType: 'touch' });
		await fireEvent.click(tapped);
		expect(onSelect).toHaveBeenLastCalledWith(1);
		expect(document.activeElement).not.toBe(tapped);
		expect(tapped.classList.contains('is-hovered')).toBe(false);

		const clicked = screen.getByRole('button', { name: 'User message 3 of 3: Run verification' });
		clicked.focus();
		await fireEvent.pointerDown(clicked, { pointerType: 'mouse' });
		await fireEvent.click(clicked);
		expect(document.activeElement).toBe(clicked);
	});

	it('scrolls only the rail to keep the current turn in view, without undoing browsing', async () => {
		const many = Array.from({ length: 80 }, (_, i) => ({ id: String(i), label: `Prompt ${i}`, title: `Prompt ${i}`, preview: '' }));
		const view = render(TurnNavigator, { props: { items: many, currentIndex: 0, onSelect: vi.fn() } });
		const rail = view.container.querySelector('.turn-rail') as HTMLElement;
		// jsdom has no layout: give the rail a 100px viewport over 13px-pitch ticks.
		Object.defineProperty(rail, 'clientHeight', { value: 100 });
		Object.defineProperty(rail, 'scrollHeight', { value: 80 * 13 + 20 });
		Object.defineProperty(rail, 'scrollTop', { value: 0, writable: true });
		Array.from(rail.children).forEach((tick, index) => {
			Object.defineProperty(tick, 'offsetTop', { value: 10 + index * 13 });
			Object.defineProperty(tick, 'offsetHeight', { value: 10 });
		});
		await view.rerender({ items: many, currentIndex: 60, onSelect: vi.fn() });
		const tickTop = 10 + 60 * 13;
		expect(rail.scrollTop).toBeLessThanOrEqual(tickTop);
		expect(rail.scrollTop + 100).toBeGreaterThanOrEqual(tickTop + 10);

		// The reader scrolls the rail elsewhere; a new turn arriving keeps it there.
		rail.scrollTop = 0;
		await view.rerender({ items: [...many, { id: '80', label: 'New', title: 'New', preview: '' }], currentIndex: 60, onSelect: vi.fn() });
		expect(rail.scrollTop).toBe(0);
		await view.rerender({ items: many, currentIndex: 2, onSelect: vi.fn() });
		expect(rail.scrollTop).toBe(0);
	});

	it('selects a turn from the timeline', async () => {
		const onSelect = vi.fn();
		render(TurnNavigator, { props: { items, currentIndex: 1, onSelect } });

		await fireEvent.click(screen.getByRole('button', { name: 'User message 3 of 3: Run verification' }));
		expect(onSelect).toHaveBeenCalledWith(2);
	});

	it('marks the current turn and exposes the hovered preview outside the scrolling rail', async () => {
		const { container } = render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		expect(
			screen.getByRole('button', { name: 'User message 1 of 3: Set up the project' }).getAttribute('aria-current')
		).toBe('true');
		await fireEvent.mouseEnter(screen.getByRole('button', { name: 'User message 2 of 3: Fix the transcript' }));
		const preview = screen.getByText('Details about the bug').closest('.preview') as HTMLElement;
		expect(preview.classList.contains('is-visible')).toBe(true);
		expect(screen.getByText('Fix the transcript')).toBeTruthy();
		// The rail scrolls (and clips); the preview must not be inside it.
		expect(container.querySelector('.turn-rail')?.contains(preview)).toBe(false);
		await fireEvent.mouseLeave(screen.getByRole('navigation'));
		expect(preview.classList.contains('is-visible')).toBe(false);
	});

	it('moves a ripple between neighboring lines and resets when the cursor leaves', async () => {
		render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		const ticks = screen.getAllByRole('button');
		const proximity = () => ticks.map((tick) => Number(tick.style.getPropertyValue('--proximity')));
		expect(proximity()).toEqual([0, 0, 0]);

		await fireEvent.mouseEnter(ticks[1]);
		expect(proximity()[1]).toBe(1);
		expect(proximity()[0]).toBeCloseTo(2 / 3);
		expect(proximity()[2]).toBeCloseTo(2 / 3);

		// Crossing the gap should not snap the ripple back to its resting position.
		await fireEvent.mouseLeave(ticks[1]);
		expect(proximity()[1]).toBe(1);
		await fireEvent.mouseEnter(ticks[2]);
		expect(proximity()[2]).toBe(1);
		expect(proximity()[0]).toBeCloseTo(1 / 3);

		await fireEvent.mouseLeave(screen.getByRole('navigation'));
		expect(proximity()).toEqual([0, 0, 0]);
	});

	it('shows the same ripple and preview for keyboard focus', async () => {
		render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		const tick = screen.getAllByRole('button')[1];
		await fireEvent.focus(tick);
		expect(tick.classList.contains('is-hovered')).toBe(true);
		expect(tick.style.getPropertyValue('--proximity')).toBe('1');
		await fireEvent.blur(tick);
		expect(tick.classList.contains('is-hovered')).toBe(false);
		expect(tick.style.getPropertyValue('--proximity')).toBe('0');
	});

	it('moves with the arrow keys while focused', async () => {
		const onSelect = vi.fn();
		render(TurnNavigator, { props: { items, currentIndex: 1, onSelect } });
		const current = screen.getByRole('button', { name: 'User message 2 of 3: Fix the transcript' });
		await fireEvent.keyDown(current, { key: 'ArrowDown' });
		expect(onSelect).toHaveBeenCalledWith(2);
		await fireEvent.keyDown(current, { key: 'ArrowUp' });
		expect(onSelect).toHaveBeenCalledWith(0);
	});
});
