/**
 * "Message all active chats": who starts ticked, how a chip leaves or rejoins
 * the list, what one send posts, and the per-chat receipts it shows after.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';

const broadcastMessage = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/broadcast', () => ({ broadcastMessage }));

import BroadcastDialog, { type BroadcastCandidate } from './BroadcastDialog.svelte';

const chats: BroadcastCandidate[] = [
	{ id: 'needs', title: 'Fix flaky emulator job', state: 'needs', worker: null },
	{ id: 'work', title: 'Review harness', state: 'working', worker: null },
	{ id: 'park', title: 'Project setup', state: 'parked', worker: null },
	{ id: 'idle', title: 'Memory system', state: 'idle', worker: 'Home server' },
	{ id: 'off', title: 'Shader lag', state: 'offline', worker: 'Laptop' }
];

async function settle() {
	await tick();
	await Promise.resolve();
	await tick();
}

const chip = (title: string) => screen.getByRole('button', { name: new RegExp(`^${title}`) });

beforeEach(() => {
	broadcastMessage.mockReset();
});
afterEach(() => {
	cleanup();
	document.body.innerHTML = '';
});

describe('BroadcastDialog', () => {
	it('ticks every chat except those waiting on you or on an offline worker', async () => {
		render(BroadcastDialog, { props: { open: true, candidates: chats, onClose: vi.fn() } });
		await settle();

		expect(chip('Review harness').getAttribute('aria-pressed')).toBe('true');
		expect(chip('Project setup').getAttribute('aria-pressed')).toBe('true');
		expect(chip('Memory system').getAttribute('aria-pressed')).toBe('true');
		expect(chip('Fix flaky emulator job').getAttribute('aria-pressed')).toBe('false');
		expect(chip('Shader lag').getAttribute('aria-pressed')).toBe('false');
		expect(screen.getByText(/5 chats · 1 working · 1 parked · 1 waiting on you · 2 on other workers · 1 offline/)).toBeTruthy();
		// Nothing to send until there is a message.
		expect((screen.getByRole('button', { name: 'Send to 3 chats' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('sends one message to the ticked chats and shows what each did', async () => {
		broadcastMessage.mockResolvedValue({
			sent: 3,
			results: [
				{ session_id: 'work', status: 'sent' },
				{ session_id: 'park', status: 'sent' },
				{ session_id: 'needs', status: 'failed', detail: 'This chat cannot take messages.' }
			]
		});
		render(BroadcastDialog, { props: { open: true, candidates: chats, onClose: vi.fn() } });
		await settle();

		await fireEvent.click(chip('Memory system')); // leave one out
		await fireEvent.click(chip('Fix flaky emulator job')); // add one back
		await fireEvent.input(screen.getByRole('textbox', { name: 'Message' }), { target: { value: '  Wrap up and report.  ' } });
		await settle();
		await fireEvent.click(screen.getByRole('button', { name: 'Send to 3 chats' }));
		await settle();

		expect(broadcastMessage).toHaveBeenCalledWith(['needs', 'work', 'park'], 'Wrap up and report.');
		const receipts = screen.getByRole('list', { name: 'What each chat did' });
		expect(receipts.textContent).toContain('Sent mid-turn');
		expect(receipts.textContent).toContain('Woke it');
		expect(receipts.textContent).toContain('Not sent');
		expect(receipts.textContent).toContain('This chat cannot take messages.');
		expect(screen.getByText('Sent to 2 of 3 chats')).toBeTruthy();
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Done' }));
	});

	it('keeps the message when the request fails, so it can be sent again', async () => {
		broadcastMessage.mockRejectedValue({ detail: 'Network down', status: 0 });
		render(BroadcastDialog, { props: { open: true, candidates: chats, onClose: vi.fn() } });
		await settle();

		const field = screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
		await fireEvent.input(field, { target: { value: 'hello' } });
		await settle();
		await fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
		await settle();

		expect(screen.getByRole('alert').textContent).toBe('Network down');
		expect(field.value).toBe('hello');
		expect((screen.getByRole('button', { name: 'Send to 3 chats' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('Escape closes it', async () => {
		const onClose = vi.fn();
		render(BroadcastDialog, { props: { open: true, candidates: chats, onClose } });
		await settle();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledOnce();
	});
});
