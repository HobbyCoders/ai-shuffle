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
