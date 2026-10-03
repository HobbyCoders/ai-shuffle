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
