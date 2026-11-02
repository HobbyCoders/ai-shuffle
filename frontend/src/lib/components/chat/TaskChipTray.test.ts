import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import TaskChipTray from './TaskChipTray.svelte';
import type { TaskChip } from '$lib/api/sessions';

const chip = (id: string, patch: Partial<TaskChip> = {}): TaskChip => ({
	id, session_id: 'root', origin_session_id: 'root', origin_label: null, title: `Chip ${id}`, summary: `Summary ${id}`,
	prompt: `Full brief ${id}`, model: 'prov:x:sonnet', provider_id: 'x', model_label: 'Sonnet', effort: null, base: 'default',
	state: 'pending', error: null, retryable: false, started_session_id: null, started_session_title: null,
	created_at: '2026-11-02T00:00:00Z', updated_at: '2026-11-02T00:00:00Z', ...patch
});

function show(chips: TaskChip[], props: Record<string, unknown> = {}) {
	const handlers = { onStart: vi.fn(), onDismiss: vi.fn(), onClear: vi.fn(), onOpenChat: vi.fn() };
	const view = render(TaskChipTray, { props: { sessionId: 'root', chips, hrefFor: (id: string) => `/?session=${id}`, ...handlers, ...props } });
	return { ...view, ...handlers };
}

afterEach(cleanup);

describe('TaskChipTray', () => {
	it('renders nothing without chips or a source', () => {
		const { container } = show([]);
		expect(container.querySelector('[data-ui-state="task-chips"]')).toBeNull();
		expect(container.querySelector('.started-from')).toBeNull();
	});

	it('shows the title, summary and model, with a plain Start and a dismiss', () => {
		show([chip('a', { effort: 'high' })]);
		expect(screen.getByText('Chip a')).toBeTruthy();
		expect(screen.getByText('Summary a')).toBeTruthy();
		expect(screen.getByText('Sonnet · high')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Start Chip a' }).textContent?.trim()).toBe('Start');
		expect(screen.getByRole('button', { name: 'Dismiss Chip a' })).toBeTruthy();
	});

	it('names the agent that raised it only when it was not the main chat', () => {
		show([chip('a'), chip('b', { origin_label: 'Task · Dev helper' })]);
		expect(screen.getAllByText(/^From /)).toHaveLength(1);
		expect(screen.getByText('From Task · Dev helper')).toBeTruthy();
	});

	it('starts and dismisses the chip it belongs to', async () => {
		const { onStart, onDismiss } = show([chip('a'), chip('b')]);
		await fireEvent.click(screen.getByRole('button', { name: 'Start Chip b' }));
		expect(onStart).toHaveBeenCalledWith(expect.objectContaining({ id: 'b' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss Chip a' }));
		expect(onDismiss).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }));
	});

	it('expands to the full brief and where it branches from', async () => {
		show([chip('a', { base: 'current' })]);
		const about = screen.getByRole('button', { name: /Chip a/, expanded: false });
		const brief = screen.getByText('Full brief a').closest<HTMLElement>('.brief')!;
		expect(brief.hidden).toBe(true);
		await fireEvent.click(about);
		expect(about.getAttribute('aria-expanded')).toBe('true');
		expect(brief.hidden).toBe(false);
		expect(screen.getByText('Branches from the current work')).toBeTruthy();
	});

	it('says the default branch when that is the base', async () => {
		show([chip('a')]);
		expect(screen.getByText('Branches from the default branch')).toBeTruthy();
	});

	it('shows three chips, then "and N more", counting what still waits', async () => {
		show([chip('1'), chip('2'), chip('3'), chip('4'), chip('5', { state: 'started', started_session_id: 'n' })]);
		expect(screen.getAllByRole('listitem')).toHaveLength(3);
		const more = screen.getByRole('button', { name: 'and 2 more, 1 to start' });
		await fireEvent.click(more);
		expect(screen.getAllByRole('listitem')).toHaveLength(5);
		expect(screen.getByRole('button', { name: 'Show fewer' })).toBeTruthy();
	});

	it('shows progress while starting and ignores more clicks', async () => {
		const { onStart } = show([chip('a', { state: 'starting' })]);
		const start = screen.getByRole('button', { name: 'Starting… Chip a' });
		expect(start.getAttribute('aria-disabled')).toBe('true');
		await fireEvent.click(start);
		expect(onStart).not.toHaveBeenCalled();
		expect(screen.queryByRole('button', { name: 'Dismiss Chip a' })).toBeNull();
	});

	it('blocks a second click while the first is on its way', async () => {
		const { onStart } = show([chip('a')], { busy: { a: 'start' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Starting… Chip a' }));
		expect(onStart).not.toHaveBeenCalled();
	});

	it('shows a failed start with its reason and Retry', async () => {
		const { onStart } = show([chip('a', { state: 'failed', error: 'No checkout could be made.', retryable: true })]);
		expect(screen.getByRole('alert').textContent).toBe('No checkout could be made.');
		await fireEvent.click(screen.getByRole('button', { name: 'Retry Chip a' }));
		expect(onStart).toHaveBeenCalledTimes(1);
		expect(screen.getByRole('button', { name: 'Dismiss Chip a' })).toBeTruthy();
	});

	it('offers no Retry when the failure cannot be retried, and still says why', () => {
		show([chip('a', { state: 'failed', error: 'Gone for good.', retryable: false })]);
		expect(screen.getByRole('alert').textContent).toBe('Gone for good.');
		expect(screen.queryByRole('button', { name: /^Retry/ })).toBeNull();
		expect(screen.getByRole('button', { name: 'Dismiss Chip a' })).toBeTruthy();
	});

	it('still says something when a failed chip has no reason', () => {
		show([chip('a', { state: 'failed', retryable: true })]);
		expect(screen.getByRole('alert').textContent).toBe('This could not start.');
	});

	it('shows a click that failed, leaving Start in place', () => {
		show([chip('a')], { errors: { a: 'Could not reach the app to start this.' } });
		expect(screen.getByRole('alert').textContent).toBe('Could not reach the app to start this.');
		expect(screen.getByRole('button', { name: 'Start Chip a' })).toBeTruthy();
	});

	it('links a started chip to its chat in place of the buttons', async () => {
		const { onOpenChat, onClear } = show([chip('a', { state: 'started', started_session_id: 'new-chat' })]);
		expect(screen.getByText('Started')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Start Chip a' })).toBeNull();
		const link = screen.getByRole('link', { name: 'Open chat: Chip a' });
		expect(link.getAttribute('href')).toBe('/?session=new-chat');
		await fireEvent.click(link);
		expect(onOpenChat).toHaveBeenCalledWith(expect.anything(), 'new-chat', expect.objectContaining({ id: 'a' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Clear Chip a' }));
		expect(onClear).toHaveBeenCalledWith(expect.objectContaining({ id: 'a' }));
	});

	it('links back to the chat a started chat came from', async () => {
		const { onOpenChat } = show([], { source: { chip_id: 'a', session_id: 'source', title: 'The source chat' } });
		const link = screen.getByRole('link', { name: 'The source chat' });
		expect(link.getAttribute('href')).toBe('/?session=source');
		expect(link.parentElement?.textContent).toMatch(/Started from\s+The source chat/);
		await fireEvent.click(link);
		expect(onOpenChat).toHaveBeenCalledWith(expect.anything(), 'source');
	});

	it('leaves focus alone when a button is pressed with a pointer', async () => {
		show([chip('a')]);
		const start = screen.getByRole('button', { name: 'Start Chip a' });
		const pressed = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
		start.dispatchEvent(pressed);
		expect(pressed.defaultPrevented).toBe(true);
	});

	it('leaves selecting text in the brief alone', async () => {
		show([chip('a')]);
		const pressed = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
		screen.getByText('Full brief a').dispatchEvent(pressed);
		expect(pressed.defaultPrevented).toBe(false);
	});

	it('moves keyboard focus to the next chip when the focused one goes', async () => {
		const { rerender } = show([chip('a'), chip('b')]);
		screen.getByRole('button', { name: 'Dismiss Chip a' }).focus();
		await rerender({ chips: [chip('b')] });
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Start Chip b' })));
	});

	it('moves keyboard focus to the open-chat link when the focused chip starts', async () => {
		const { rerender } = show([chip('a')]);
		screen.getByRole('button', { name: 'Start Chip a' }).focus();
		await rerender({ chips: [chip('a', { state: 'started', started_session_id: 'n' })] });
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('link', { name: 'Open chat: Chip a' })));
	});

	it('announces a new suggestion, but not the ones already there when a chat opens', async () => {
		const { rerender, container } = show([], { loaded: false });
		const status = () => container.querySelector('[role="status"]')?.textContent;
		await rerender({ chips: [chip('a')], loaded: true });
		await waitFor(() => expect(screen.getByText('Chip a')).toBeTruthy());
		expect(status()).toBe('');
		await rerender({ chips: [chip('a'), chip('b')] });
		await waitFor(() => expect(status()).toBe('Suggested task: Chip b'));
	});

	it('announces the first chip of a chat that had none', async () => {
		const { rerender, container } = show([]);
		await rerender({ chips: [chip('a')] });
		await waitFor(() => expect(container.querySelector('[role="status"]')?.textContent).toBe('Suggested task: Chip a'));
	});

	it('forgets expansion and what it has announced when the chat changes', async () => {
		const { rerender, container } = show([chip('a')]);
		await fireEvent.click(screen.getByRole('button', { name: /Chip a/, expanded: false }));
		await rerender({ sessionId: 'other', chips: [chip('c')] });
		await waitFor(() => expect(screen.getByRole('button', { name: /Chip c/, expanded: false })).toBeTruthy());
		expect(screen.getByText('Full brief c').closest<HTMLElement>('.brief')!.hidden).toBe(true);
		expect(container.querySelector('[role="status"]')?.textContent).toBe('');
	});

	it('takes less room when told something else is waiting beside the composer', () => {
		const { container } = show([chip('a')], { crowded: true });
		expect(container.querySelector('.chip-tray')?.classList.contains('crowded')).toBe(true);
	});

	it('marks itself so tapping a chip leaves the composer and phone keyboard alone', () => {
		const { container } = show([chip('a')]);
		expect(container.querySelector('.chip-tray')?.hasAttribute('data-keeps-composer')).toBe(true);
	});

	it('carries the session its chips belong to for the flight recorder', () => {
		const { container } = show([chip('a'), chip('b', { state: 'failed' }), chip('c'), chip('d')], { errors: { c: 'No connection.' }, busy: { d: 'start' } });
		const tray = container.querySelector<HTMLElement>('[data-ui-state="task-chips"]')!;
		expect(tray.dataset.uiChips).toBe('pending,failed,not-sent,starting');
		expect(tray.dataset.uiChipSession).toMatch(/^s:/);
	});
});
