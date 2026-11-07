/**
 * Keyboard/AT contract of the notification bell: Escape closes the panel and
 * hands focus back to the bell, and each item names its severity (which is
 * otherwise only icon shape and colour).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(() => Promise.resolve({})), delete: vi.fn(() => Promise.resolve({})), patch: vi.fn(() => Promise.resolve({})) } }));
vi.mock('$lib/stores/auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { notify } from '$lib/stores/notifications';
import NotificationCenter from './NotificationCenter.svelte';

beforeEach(() => notify.clearAll());
afterEach(() => {
	cleanup();
	notify.clearAll();
});

describe('NotificationCenter', () => {
	it('Escape closes the panel and returns focus to the bell', async () => {
		notify.error('Build failed');
		render(NotificationCenter);
		const bell = screen.getByRole('button', { name: /Notifications/ });
		await fireEvent.click(bell);

		const dismiss = screen.getByRole('button', { name: 'Dismiss notification' });
		dismiss.focus();
		await fireEvent.keyDown(dismiss, { key: 'Escape' });

		expect(screen.queryByRole('dialog', { name: 'Notifications' })).toBeNull();
		expect(document.activeElement).toBe(bell);
	});

	it('keeps focus in the panel after a dismiss: next row, then previous row, then the panel', async () => {
		notify.info('Third');
		notify.info('Second');
		notify.info('First');
		render(NotificationCenter);
		await fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
		const panel = screen.getByRole('dialog', { name: 'Notifications' });
		const rowOf = (el: Element | null) => el?.closest('.notif-item')?.querySelector('.item-message')?.textContent;
		const dismissFor = (text: string) => [...panel.querySelectorAll<HTMLButtonElement>('.item-dismiss')].find(b => rowOf(b) === text)!;
		expect([...panel.querySelectorAll('.item-message')].map(e => e.textContent)).toEqual(['First', 'Second', 'Third']);

		// Middle row: focus moves to the next row's dismiss.
		dismissFor('Second').focus();
		await fireEvent.click(dismissFor('Second'));
		await vi.waitFor(() => expect(rowOf(document.activeElement)).toBe('Third'));
		expect(document.activeElement?.classList.contains('item-dismiss')).toBe(true);
		// Last row: no next one, so the previous row's dismiss.
		await fireEvent.click(dismissFor('Third'));
		await vi.waitFor(() => expect(rowOf(document.activeElement)).toBe('First'));
		// The only row: the panel itself, never the page body.
		await fireEvent.click(dismissFor('First'));
		await vi.waitFor(() => expect(document.activeElement).toBe(panel));
		expect(screen.getByText('No notifications')).toBeTruthy();
	});

	describe('rail popover scrim follows the visible list edge', () => {
		// The 300px panel lands at x8–308 here (jsdom's bell sits at x0).
		const observers: Array<() => void> = [];
		const original = globalThis.ResizeObserver;
		let observed: Element[] = [];
		beforeEach(() => {
			observers.length = 0;
			observed = [];
			globalThis.ResizeObserver = class {
				constructor(private cb: () => void) {}
				observe(el: Element) { observed.push(el); if (!observers.includes(this.cb)) observers.push(this.cb); }
				unobserve() {}
				disconnect() { const i = observers.indexOf(this.cb); if (i >= 0) observers.splice(i, 1); }
			} as unknown as typeof ResizeObserver;
		});
		afterEach(() => {
			globalThis.ResizeObserver = original;
			document.body.replaceChildren();
		});

		/** The real sidebar shape: aside > (rail with the bell) + .panel-plane > .col. */
		function mountSidebar(geometry: { aside: number; col: number }) {
			const aside = document.body.appendChild(document.createElement('aside'));
			const nav = aside.appendChild(document.createElement('div'));
			const plane = aside.appendChild(document.createElement('div'));
			plane.className = 'panel-plane';
			const col = plane.appendChild(document.createElement('div'));
			col.className = 'col';
			aside.getBoundingClientRect = () => ({ right: geometry.aside } as DOMRect);
			// A collapsed column stops rendering: a zero box.
			col.getBoundingClientRect = () => (geometry.col > 0
				? { right: geometry.col, width: geometry.col - 56, height: 800 }
				: { right: 0, width: 0, height: 0 }) as DOMRect;
			notify.info('Deploy finished');
			render(NotificationCenter, { props: { rail: true }, target: nav });
			return { col };
		}
		const reMeasure = () => observers.forEach((cb) => cb());
		const scrim = () => screen.queryByTestId('notif-scrim');

		it('re-measures when a synced width or collapse changes the list while the panel is open', async () => {
			const geometry = { aside: 440, col: 440 };
			const { col } = mountSidebar(geometry);
			await fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
			expect(scrim()).toBeNull(); // ends inside a 440px list
			expect(observed).toContain(col);

			geometry.aside = geometry.col = 200;
			reMeasure();
			await vi.waitFor(() => expect(scrim()).not.toBeNull());

			geometry.aside = geometry.col = 440;
			reMeasure();
			await vi.waitFor(() => expect(scrim()).toBeNull());

			// Collapsed to the rail: only the rail is left, so the panel is on the page.
			geometry.aside = 56;
			geometry.col = 0;
			reMeasure();
			await vi.waitFor(() => expect(scrim()).not.toBeNull());

			await fireEvent.keyDown(window, { key: 'Escape' });
			await vi.waitFor(() => expect(observers).toHaveLength(0));
		});

		it('measures the floating list, not the rail-wide aside, in a narrow window', async () => {
			// Narrow window: the aside stays 56px and the list plane floats out to 440.
			const geometry = { aside: 56, col: 440 };
			mountSidebar(geometry);
			await fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
			expect(scrim()).toBeNull();

			// A synced width shrinks only the floating column.
			geometry.col = 200;
			reMeasure();
			await vi.waitFor(() => expect(scrim()).not.toBeNull());
		});
	});

	it('names each item severity for assistive tech', async () => {
		notify.warning('Disk almost full');
		render(NotificationCenter);
		await fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));

		expect(screen.getByRole('img', { name: 'Warning' })).toBeTruthy();
	});

	it('renders both an action and a second action on one item', async () => {
		const report = vi.fn();
		const fix = vi.fn();
		notify.ingest(
			{ id: 'srv-1', severity: 'error', category: 'api_error', message: 'API error: HTTP 500' },
			{ label: 'Send report to support', onClick: report },
			{ label: 'Fix with AI', onClick: fix }
		);
		render(NotificationCenter);
		await fireEvent.click(screen.getByRole('button', { name: /Notifications/ }));
		await fireEvent.click(screen.getByRole('button', { name: 'Send report to support' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Fix with AI' }));
		expect(report).toHaveBeenCalledTimes(1);
		expect(fix).toHaveBeenCalledTimes(1);
	});
});
