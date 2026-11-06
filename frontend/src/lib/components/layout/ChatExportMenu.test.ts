import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({ exportSession: vi.fn(async () => undefined) }));
vi.mock('$lib/api/sessions', () => ({ exportSession: mocks.exportSession }));
vi.mock('$lib/stores/notifications', () => ({ notify: vi.fn() }));
import ChatExportMenu from './ChatExportMenu.svelte';

afterEach(cleanup);

describe('the chat header Export menu', () => {
	const trigger = () => screen.getByRole('button', { name: 'Export chat' });

	it('shows one button until opened, then both exports', async () => {
		render(ChatExportMenu, { sessionId: 's1' });
		expect(screen.getAllByRole('button')).toHaveLength(1);
		expect(trigger().getAttribute('aria-expanded')).toBe('false');
		await fireEvent.click(trigger());
		expect(trigger().getAttribute('aria-expanded')).toBe('true');
		expect(screen.getByRole('button', { name: 'Download private backup (.jsonl)' })).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Export for sharing (.md)' })).toBeTruthy();
	});

	it('runs an export and closes', async () => {
		render(ChatExportMenu, { sessionId: 's1' });
		await fireEvent.click(trigger());
		await fireEvent.click(screen.getByRole('button', { name: 'Export for sharing (.md)' }));
		expect(mocks.exportSession).toHaveBeenCalledWith('s1', 'markdown', { shareSafe: true });
		expect(screen.queryByRole('button', { name: 'Export for sharing (.md)' })).toBeNull();
	});

	it('closes on Escape and returns focus, and on a press outside', async () => {
		render(ChatExportMenu, { sessionId: 's1' });
		await fireEvent.click(trigger());
		await fireEvent.keyDown(document, { key: 'Escape' });
		expect(screen.queryByRole('button', { name: 'Export for sharing (.md)' })).toBeNull();
		expect(document.activeElement).toBe(trigger());
		await fireEvent.click(trigger());
		await fireEvent.pointerDown(document.body);
		expect(screen.queryByRole('button', { name: 'Export for sharing (.md)' })).toBeNull();
	});
});
