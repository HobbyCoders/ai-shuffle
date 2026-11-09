import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TurnNavigator from './TurnNavigator.svelte';
import type { ChatMessage } from '$lib/stores/types';
import { deriveUserTurnNavigationItems } from '$lib/utils/transcript-navigation';

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
	// #2268: the rail counts turns. A steer carries its turn's id and has no
	// rewind checkpoint of its own, so it is not a stop.
	it('reads a steered single turn as turn 1 of 1, and two turns as 1 of 2', async () => {
		const row = (id: string, role: 'user' | 'assistant', content: string, turnId?: string) =>
			({ id, role, content, turnId }) as ChatMessage;
		const steered = [
			row('u1', 'user', 'Run the checks', 't1'),
			row('a1', 'assistant', 'Starting.'),
			row('u2', 'user', 'Reply with steered', 't1'),
			row('a2', 'assistant', 'steered')
		];
		const view = render(TurnNavigator, {
			props: { items: deriveUserTurnNavigationItems(steered), currentIndex: 0, onSelect: vi.fn() }
		});
		expect(screen.getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
			'Turn 1 of 1: Run the checks'
		]);
		const twoTurns = [...steered, row('u3', 'user', 'Ship it', 't2'), row('a3', 'assistant', 'Shipped.')];
		await view.rerender({ items: deriveUserTurnNavigationItems(twoTurns), currentIndex: 0, onSelect: vi.fn() });
		expect(screen.getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
			'Turn 1 of 2: Run the checks',
			'Turn 2 of 2: Ship it'
		]);
	});

	it('puts the amber Needs-you dot on the waiting turn only (spec §5)', async () => {
		const view = render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn(), needsYouIds: ['three'] } });
		const dots = () => view.container.querySelectorAll('[data-needs-you]');
		expect(dots()).toHaveLength(1);
		const waiting = screen.getByRole('button', { name: 'Turn 3 of 3: Run verification (needs you)' });
		expect(waiting.querySelector('[data-needs-you]')?.classList.contains('bg-warning')).toBe(true);
		expect(screen.getByRole('button', { name: 'Turn 1 of 3: Set up the project' }).querySelector('[data-needs-you]')).toBeNull();
		expect(screen.getByRole('button', { name: 'Turn 2 of 3: Fix the transcript' }).querySelector('[data-needs-you]')).toBeNull();
		await view.rerender({ items, currentIndex: 0, onSelect: vi.fn(), needsYouIds: [] });
		expect(dots()).toHaveLength(0);
	});

	it('renders the same rail of ticks for long histories, with no second control set', async () => {
		const onSelect = vi.fn();
		const many = Array.from({ length: 1000 }, (_, i) => ({ id: String(i), label: `Turn ${i}`, title: `Turn ${i}`, preview: '' }));
		const { container } = render(TurnNavigator, { props: { items: many, currentIndex: 500, onSelect } });
		const ticks = screen.getAllByRole('button');
		expect(ticks).toHaveLength(1000);
		expect(ticks.every((tick) => tick.classList.contains('tick'))).toBe(true);
		// Every button is a tick named by its aria-label. Look names up in that
		// list: a role+name query recomputes all 1000 accessible names each time,
		// which alone outran the 5 s test timeout on a slower Windows machine.
		const named = (label: string) => ticks.filter((tick) => tick.getAttribute('aria-label') === label);
		expect(screen.queryByRole('searchbox')).toBeNull();
		expect(ticks.some((tick) => /First user turn|Browse session timeline/.test(tick.getAttribute('aria-label') ?? ''))).toBe(false);
		expect(container.querySelector('.long-history')).toBeNull();
		const [current] = named('Turn 501 of 1000: Turn 500');
		expect(named('Turn 501 of 1000: Turn 500')).toHaveLength(1);
		expect(current.getAttribute('aria-current')).toBe('true');
		await fireEvent.click(named('Turn 1 of 1000: Turn 0')[0]);
		expect(onSelect).toHaveBeenLastCalledWith(0);
		await fireEvent.click(named('Turn 1000 of 1000: Turn 999')[0]);
		expect(onSelect).toHaveBeenLastCalledWith(999);
	}, 20_000); // 1000 rendered ticks: the default 5 s is tight on a slow machine

	it('moves focus with Home, End and arrows so keyboard users stay on a visible tick', async () => {
		const onSelect = vi.fn();
		const many = Array.from({ length: 45 }, (_, i) => ({ id: String(i), label: `Prompt ${i}`, title: `Prompt ${i}`, preview: '' }));
		render(TurnNavigator, { props: { items: many, currentIndex: 20, onSelect } });
		const tickNamed = (n: number) => screen.getByRole('button', { name: `Turn ${n} of 45: Prompt ${n - 1}` });
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
		const tickNamed = (n: number) => screen.getByRole('button', { name: `Turn ${n} of 300: Prompt ${n - 1}` });
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
		const tapped = screen.getByRole('button', { name: 'Turn 2 of 3: Fix the transcript' });
		tapped.focus();
		await fireEvent.pointerDown(tapped, { pointerType: 'touch' });
		await fireEvent.click(tapped);
		expect(onSelect).toHaveBeenLastCalledWith(1);
		expect(document.activeElement).not.toBe(tapped);
		expect(tapped.classList.contains('is-hovered')).toBe(false);

		const clicked = screen.getByRole('button', { name: 'Turn 3 of 3: Run verification' });
		clicked.focus();
		await fireEvent.pointerDown(clicked, { pointerType: 'mouse' });
		await fireEvent.click(clicked);
		expect(document.activeElement).toBe(clicked);
	});

	describe('touch scrub', () => {
		const many = Array.from({ length: 6 }, (_, i) => ({ id: String(i), label: `Prompt ${i}`, title: `Prompt ${i}`, preview: `Body ${i}` }));
		/** jsdom has no layout: a rail at viewport y 0..120 with 16px ticks from y 10. */
		function layOut(container: HTMLElement) {
			const rail = container.querySelector('.turn-rail') as HTMLElement;
			rail.getBoundingClientRect = () => ({ top: 0, bottom: 120, left: 0, right: 26, width: 26, height: 120, x: 0, y: 0, toJSON() {} });
			Array.from(rail.children).forEach((tick, index) => {
				Object.defineProperty(tick, 'offsetTop', { value: 10 + index * 16 });
				Object.defineProperty(tick, 'offsetHeight', { value: 16 });
			});
			return rail;
		}
		const centre = (index: number) => 10 + index * 16 + 8;

		afterEach(() => vi.useRealTimers());

		it('previews the tick under a sliding finger and selects it when the finger rests', async () => {
			vi.useFakeTimers();
			const onSelect = vi.fn();
			const { container } = render(TurnNavigator, { props: { items: many, currentIndex: 0, onSelect } });
			const rail = layOut(container);
			const ticks = screen.getAllByRole('button');
			const preview = container.querySelector('.preview') as HTMLElement;

			await fireEvent.pointerDown(ticks[0], { pointerType: 'touch', isPrimary: true, pointerId: 7, clientY: centre(0) });
			// Under the slop it is still a tap: nothing lights up.
			await fireEvent.pointerMove(rail, { pointerType: 'touch', isPrimary: true, pointerId: 7, clientY: centre(0) + 2 });
			expect(container.querySelector('.is-scrubbing')).toBeNull();

			// Sliding past ticks lights each one like a hover, without navigating.
			await fireEvent.pointerMove(rail, { pointerType: 'touch', isPrimary: true, pointerId: 7, clientY: centre(2) });
			expect(container.querySelector('nav')?.classList.contains('is-scrubbing')).toBe(true);
			expect(ticks[2].classList.contains('is-hovered')).toBe(true);
			expect(preview.classList.contains('is-visible')).toBe(true);
			expect(preview.classList.contains('is-above')).toBe(true);
			expect(preview.textContent).toContain('Prompt 2');
			vi.advanceTimersByTime(100);
			await fireEvent.pointerMove(rail, { pointerType: 'touch', isPrimary: true, pointerId: 7, clientY: centre(4) });
			expect(ticks[4].classList.contains('is-hovered')).toBe(true);
			vi.advanceTimersByTime(200);
			expect(onSelect).not.toHaveBeenCalled();

			// Resting on a tick selects it while the finger is still down.
			vi.advanceTimersByTime(150);
			expect(onSelect).toHaveBeenCalledTimes(1);
			expect(onSelect).toHaveBeenLastCalledWith(4);

			// Lifting on the same tick does not select it again, and the gesture's
			// trailing click on the first tick is ignored.
			await fireEvent.pointerUp(rail, { pointerType: 'touch', isPrimary: true, pointerId: 7, clientY: centre(4) });
			await fireEvent.click(ticks[0]);
			expect(onSelect).toHaveBeenCalledTimes(1);
			expect(container.querySelector('.is-scrubbing')).toBeNull();
			expect(container.querySelector('.is-hovered')).toBeNull();
			expect(preview.classList.contains('is-visible')).toBe(false);

			// A quick tap on another tick right after the scrub still selects it.
			await fireEvent.click(ticks[3]);
			expect(onSelect).toHaveBeenCalledTimes(2);
			expect(onSelect).toHaveBeenLastCalledWith(3);
		});

		it('selects the tick under the finger when it lifts before resting', async () => {
			vi.useFakeTimers();
			const onSelect = vi.fn();
			const { container } = render(TurnNavigator, { props: { items: many, currentIndex: 0, onSelect } });
			const rail = layOut(container);
			const ticks = screen.getAllByRole('button');
			await fireEvent.pointerDown(ticks[5], { pointerType: 'touch', isPrimary: true, pointerId: 3, clientY: centre(5) });
			await fireEvent.pointerMove(rail, { pointerType: 'touch', isPrimary: true, pointerId: 3, clientY: centre(3) });
			// Past the rail's top the finger reads as the first tick.
			await fireEvent.pointerMove(rail, { pointerType: 'touch', isPrimary: true, pointerId: 3, clientY: -40 });
			await fireEvent.pointerUp(rail, { pointerType: 'touch', isPrimary: true, pointerId: 3, clientY: -40 });
			expect(onSelect).toHaveBeenCalledTimes(1);
			expect(onSelect).toHaveBeenLastCalledWith(0);
			vi.advanceTimersByTime(1000);
			expect(onSelect).toHaveBeenCalledTimes(1);
		});

		it('selects nothing when the system cancels the touch', async () => {
			vi.useFakeTimers();
			const onSelect = vi.fn();
			const { container } = render(TurnNavigator, { props: { items: many, currentIndex: 0, onSelect } });
			const rail = layOut(container);
			await fireEvent.pointerDown(screen.getAllByRole('button')[0], { pointerType: 'touch', isPrimary: true, pointerId: 1, clientY: centre(0) });
			await fireEvent.pointerMove(rail, { pointerType: 'touch', isPrimary: true, pointerId: 1, clientY: centre(3) });
			await fireEvent.pointerCancel(rail, { pointerType: 'touch', isPrimary: true, pointerId: 1 });
			vi.advanceTimersByTime(1000);
			expect(onSelect).not.toHaveBeenCalled();
			expect(container.querySelector('.is-hovered')).toBeNull();
		});

		describe('with a sheet or picker open', () => {
			let trigger: HTMLButtonElement;
			beforeEach(() => {
				trigger = document.createElement('button');
				trigger.setAttribute('aria-haspopup', 'dialog');
				trigger.setAttribute('aria-expanded', 'true');
				document.body.append(trigger);
			});
			afterEach(() => trigger.remove());
			const touch = { pointerType: 'touch', isPrimary: true, pointerId: 2 };
			const closeTrigger = () => trigger.setAttribute('aria-expanded', 'false');

			/** A touch tap in the order a browser sends it, then the wait for the
			 *  rail's decision. `detail` is what tells a real press from a keyboard. */
			async function tap(tick: HTMLElement, index: number) {
				await fireEvent.pointerDown(tick, { ...touch, clientY: centre(index) });
				await fireEvent.pointerUp(tick, { ...touch, clientY: centre(index) });
				await fireEvent.mouseDown(tick);
				await fireEvent.mouseUp(tick);
				await fireEvent.click(tick, { detail: 1 });
				await new Promise((resolve) => setTimeout(resolve, 0));
			}
			function renderRail() {
				const onSelect = vi.fn();
				const view = render(TurnNavigator, { props: { items: many, currentIndex: 0, onSelect } });
				const tick = screen.getAllByRole('button', { name: /^Turn/ })[3];
				return { ...view, onSelect, tick };
			}

			// Every way a popup in the app closes on an outside press. The click cases
			// run after the tick's own handler, which is why the rail has to wait.
			const closesOn: [string, string, EventTarget][] = [
				['pointerdown at the document, in capture', 'pointerdown', document],
				['pointerdown at the window', 'pointerdown', window],
				['the mousedown a touch sends before its click', 'mousedown', document],
				['click at the document', 'click', document],
				['click at the window', 'click', window]
			];
			for (const [path, type, target] of closesOn) {
				it(`a tap that closes it on ${path} only closes it`, async () => {
					const capture = path.includes('capture');
					target.addEventListener(type, closeTrigger, capture);
					const { onSelect, tick } = renderRail();
					tick.focus();
					await tap(tick, 3);
					target.removeEventListener(type, closeTrigger, capture);
					expect(trigger.getAttribute('aria-expanded')).toBe('false');
					expect(onSelect).not.toHaveBeenCalled();
					expect(document.activeElement).not.toBe(tick);
				});
			}

			it('a tap that removes the trigger with its popup only closes it', async () => {
				const remove = () => trigger.remove();
				document.addEventListener('click', remove);
				const { onSelect, tick } = renderRail();
				await tap(tick, 3);
				document.removeEventListener('click', remove);
				expect(onSelect).not.toHaveBeenCalled();
			});

			it('the usage sheet with its ring hidden: the first tap closes, the second selects', async () => {
				trigger.style.display = 'none';
				document.addEventListener('click', closeTrigger);
				const { onSelect, tick } = renderRail();
				await tap(tick, 3);
				document.removeEventListener('click', closeTrigger);
				expect(onSelect).not.toHaveBeenCalled();
				await tap(tick, 3);
				expect(onSelect).toHaveBeenCalledTimes(1);
				expect(onSelect).toHaveBeenLastCalledWith(3);
			});

			it('a filter the rail cannot close, left behind a collapsed sidebar, never blocks a tap', async () => {
				const column = document.createElement('div');
				column.style.display = 'none';
				column.append(trigger);
				document.body.append(column);
				const { onSelect, tick } = renderRail();
				await tap(tick, 3);
				expect(onSelect).toHaveBeenCalledTimes(1);
				await tap(tick, 3);
				expect(onSelect).toHaveBeenCalledTimes(2);
				expect(trigger.getAttribute('aria-expanded')).toBe('true');
				column.remove();
			});

			it('Settings kept alive behind the chat closes its picker on the press, unseen, and the tap still selects', async () => {
				const panel = document.createElement('div');
				panel.setAttribute('inert', '');
				panel.append(trigger);
				document.body.append(panel);
				document.addEventListener('mousedown', closeTrigger);
				const { onSelect, tick } = renderRail();
				await tap(tick, 3);
				document.removeEventListener('mousedown', closeTrigger);
				expect(trigger.getAttribute('aria-expanded')).toBe('false');
				expect(onSelect).toHaveBeenCalledTimes(1);
				panel.remove();
			});

			it('a slide that closes it on pointerdown neither scrubs nor selects', async () => {
				vi.useFakeTimers();
				document.addEventListener('pointerdown', closeTrigger);
				const { container, onSelect } = renderRail();
				const rail = layOut(container);
				const first = screen.getAllByRole('button', { name: /^Turn/ })[0];
				await fireEvent.pointerDown(first, { ...touch, clientY: centre(0) });
				document.removeEventListener('pointerdown', closeTrigger);
				await fireEvent.pointerMove(rail, { ...touch, clientY: centre(3) });
				expect(container.querySelector('.is-scrubbing')).toBeNull();
				vi.advanceTimersByTime(1000);
				await fireEvent.pointerUp(rail, { ...touch, clientY: centre(3) });
				expect(onSelect).not.toHaveBeenCalled();
			});

			it('a slide over a popup it does not close scrubs at once', async () => {
				vi.useFakeTimers();
				const { container, onSelect } = renderRail();
				const rail = layOut(container);
				const first = screen.getAllByRole('button', { name: /^Turn/ })[0];
				await fireEvent.pointerDown(first, { ...touch, clientY: centre(0) });
				await fireEvent.pointerMove(rail, { ...touch, clientY: centre(3) });
				expect(container.querySelector('.is-scrubbing')).not.toBeNull();
				await fireEvent.pointerUp(rail, { ...touch, clientY: centre(3) });
				expect(onSelect).toHaveBeenCalledWith(3);
			});

			describe('the press the rail acts on keeps its own reading', () => {
				const mouse = { pointerType: 'mouse', isPrimary: true, pointerId: 1 };
				const secondFinger = { pointerType: 'touch', isPrimary: false, pointerId: 9 };
				/** A finger lands on the rail and that press closes the popup. */
				async function pressThatCloses(ticks: HTMLElement[]) {
					document.addEventListener('pointerdown', closeTrigger, true);
					await fireEvent.pointerDown(ticks[0], { ...touch, clientY: centre(0) });
					document.removeEventListener('pointerdown', closeTrigger, true);
					expect(trigger.getAttribute('aria-expanded')).toBe('false');
				}
				function railWithTicks() {
					const view = renderRail();
					return { ...view, rail: layOut(view.container), ticks: screen.getAllByRole('button', { name: /^Turn/ }) };
				}

				it('a second finger does not undo the first finger having closed it', async () => {
					const { onSelect, rail, ticks } = railWithTicks();
					await pressThatCloses(ticks);
					await fireEvent.pointerDown(ticks[2], { ...secondFinger, clientY: centre(2) });
					await fireEvent.pointerMove(rail, { ...touch, clientY: centre(3) });
					await fireEvent.pointerUp(rail, { ...touch, clientY: centre(3) });
					expect(onSelect).not.toHaveBeenCalled();
				});

				it('a second finger landing as the first lifts does not free its click', async () => {
					const { onSelect, ticks } = railWithTicks();
					await pressThatCloses(ticks);
					await fireEvent.pointerUp(ticks[0], { ...touch, clientY: centre(0) });
					await fireEvent.pointerDown(ticks[2], { ...secondFinger, clientY: centre(2) });
					await fireEvent.click(ticks[0], { detail: 1 });
					await new Promise((resolve) => setTimeout(resolve, 0));
					expect(onSelect).not.toHaveBeenCalled();
				});

				it('a mouse press during the finger does not free its slide', async () => {
					const { onSelect, rail, ticks } = railWithTicks();
					await pressThatCloses(ticks);
					await fireEvent.pointerDown(document.body, mouse);
					await fireEvent.pointerMove(rail, { ...touch, clientY: centre(3) });
					await fireEvent.pointerUp(rail, { ...touch, clientY: centre(3) });
					expect(onSelect).not.toHaveBeenCalled();
				});

				it('a mouse press during the finger does not free its tap', async () => {
					const { onSelect, ticks } = railWithTicks();
					await pressThatCloses(ticks);
					await fireEvent.pointerDown(document.body, mouse);
					await fireEvent.pointerUp(ticks[0], { ...touch, clientY: centre(0) });
					await fireEvent.click(ticks[0], { detail: 1 });
					await new Promise((resolve) => setTimeout(resolve, 0));
					expect(onSelect).not.toHaveBeenCalled();
				});

				it('a finger whose lift was lost leaves nothing for a later mouse click or tap', async () => {
					const { onSelect, ticks } = railWithTicks();
					await pressThatCloses(ticks);
					await fireEvent.pointerDown(ticks[1], mouse);
					await fireEvent.click(ticks[1], { detail: 1 });
					expect(onSelect).toHaveBeenCalledTimes(1);
					expect(onSelect).toHaveBeenLastCalledWith(1);
					await tap(ticks[3], 3);
					expect(onSelect).toHaveBeenCalledTimes(2);
					expect(onSelect).toHaveBeenLastCalledWith(3);
				});

				it('a second finger on the rail while the first is elsewhere does nothing and leaves nothing', async () => {
					const { onSelect, rail, ticks } = railWithTicks();
					await fireEvent.pointerDown(document.body, { ...touch, clientY: 400 });
					await fireEvent.pointerDown(ticks[2], { ...secondFinger, clientY: centre(2) });
					await fireEvent.pointerMove(rail, { ...secondFinger, clientY: centre(4) });
					await fireEvent.pointerUp(ticks[2], { ...secondFinger, clientY: centre(4) });
					await fireEvent.pointerUp(document.body, { ...touch, clientY: 400 });
					expect(onSelect).not.toHaveBeenCalled();
					expect(rail.closest('nav')?.classList.contains('is-scrubbing')).toBe(false);
					await tap(ticks[3], 3);
					expect(onSelect).toHaveBeenCalledTimes(1);
					expect(onSelect).toHaveBeenLastCalledWith(3);
				});

				it('a newer press during the wait does not change what the tap closed', async () => {
					document.addEventListener('click', closeTrigger);
					const { onSelect, ticks } = railWithTicks();
					vi.useFakeTimers();
					await fireEvent.pointerDown(ticks[3], { ...touch, clientY: centre(3) });
					await fireEvent.pointerUp(ticks[3], { ...touch, clientY: centre(3) });
					await fireEvent.click(ticks[3], { detail: 1 });
					document.removeEventListener('click', closeTrigger);
					await fireEvent.pointerDown(document.body, { ...touch, pointerId: 4, clientY: 400 });
					vi.advanceTimersByTime(1);
					expect(onSelect).not.toHaveBeenCalled();
				});
			});

			it('does not select after the rail has gone away', async () => {
				const { onSelect, tick, unmount } = renderRail();
				await fireEvent.pointerDown(tick, { ...touch, clientY: centre(3) });
				await fireEvent.pointerUp(tick, { ...touch, clientY: centre(3) });
				await fireEvent.click(tick, { detail: 1 });
				unmount();
				await new Promise((resolve) => setTimeout(resolve, 0));
				expect(onSelect).not.toHaveBeenCalled();
			});

			it('leaves a mouse click alone, and selects without waiting', async () => {
				document.addEventListener('click', closeTrigger);
				const { onSelect, tick } = renderRail();
				await fireEvent.pointerDown(tick, { pointerType: 'mouse', isPrimary: true, pointerId: 1 });
				await fireEvent.click(tick, { detail: 1 });
				document.removeEventListener('click', closeTrigger);
				expect(onSelect).toHaveBeenLastCalledWith(3);
			});

			it('a keyboard press after a touch selects without reading that touch', async () => {
				document.addEventListener('pointerdown', closeTrigger);
				const { container, onSelect, tick } = renderRail();
				const rail = layOut(container);
				await fireEvent.pointerDown(tick, { ...touch, clientY: centre(3) });
				document.removeEventListener('pointerdown', closeTrigger);
				await fireEvent.pointerCancel(rail, { ...touch, clientY: centre(3) });
				await fireEvent.click(tick);
				expect(onSelect).toHaveBeenLastCalledWith(3);
			});

			it('with nothing expanded, a tap selects without waiting', async () => {
				closeTrigger();
				const { onSelect, tick } = renderRail();
				await fireEvent.pointerDown(tick, { ...touch, clientY: centre(3) });
				await fireEvent.pointerUp(tick, { ...touch, clientY: centre(3) });
				await fireEvent.click(tick, { detail: 1 });
				expect(onSelect).toHaveBeenLastCalledWith(3);
			});
		});

		it('leaves a mouse drag to hover and click', async () => {
			const onSelect = vi.fn();
			const { container } = render(TurnNavigator, { props: { items: many, currentIndex: 0, onSelect } });
			const rail = layOut(container);
			await fireEvent.pointerDown(screen.getAllByRole('button')[0], { pointerType: 'mouse', isPrimary: true, pointerId: 1, clientY: centre(0) });
			await fireEvent.pointerMove(rail, { pointerType: 'mouse', isPrimary: true, pointerId: 1, clientY: centre(3) });
			await fireEvent.pointerUp(rail, { pointerType: 'mouse', isPrimary: true, pointerId: 1, clientY: centre(3) });
			expect(container.querySelector('.is-scrubbing')).toBeNull();
			expect(onSelect).not.toHaveBeenCalled();
		});
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

		await fireEvent.click(screen.getByRole('button', { name: 'Turn 3 of 3: Run verification' }));
		expect(onSelect).toHaveBeenCalledWith(2);
	});

	it('marks the current turn and exposes the hovered preview outside the scrolling rail', async () => {
		const { container } = render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		expect(
			screen.getByRole('button', { name: 'Turn 1 of 3: Set up the project' }).getAttribute('aria-current')
		).toBe('true');
		await fireEvent.mouseEnter(screen.getByRole('button', { name: 'Turn 2 of 3: Fix the transcript' }));
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
		const current = screen.getByRole('button', { name: 'Turn 2 of 3: Fix the transcript' });
		await fireEvent.keyDown(current, { key: 'ArrowDown' });
		expect(onSelect).toHaveBeenCalledWith(2);
		await fireEvent.keyDown(current, { key: 'ArrowUp' });
		expect(onSelect).toHaveBeenCalledWith(0);
	});
});
