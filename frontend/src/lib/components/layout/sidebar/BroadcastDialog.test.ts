/**
 * "Message all active chats": who starts ticked, how a chip leaves or rejoins
 * the list, what one send posts, and the per-chat receipts it shows after.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';

const broadcastMessage = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/broadcast', () => ({ broadcastMessage, MAX_BROADCAST_CHATS: 100 }));

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
		const word = (title: string) =>
			[...receipts.querySelectorAll('li')].find((li) => li.textContent?.includes(title))?.querySelector('.r-word')?.textContent;
		// Admission only: the intake does not say whether a working chat steered it in.
		expect(word('Review harness')).toBe('Sent');
		expect(word('Project setup')).toBe('Sent');
		expect(word('Fix flaky emulator job')).toBe('Not sent');
		expect(receipts.textContent).toContain('This chat cannot take messages.');
		expect(screen.getByText('Sent to 2 of 3 chats')).toBeTruthy();
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Done' }));
	});

	async function sendFailing(rejection: unknown) {
		broadcastMessage.mockRejectedValue(rejection);
		render(BroadcastDialog, { props: { open: true, candidates: chats, onClose: vi.fn() } });
		await settle();
		const field = screen.getByRole('textbox', { name: 'Message' }) as HTMLTextAreaElement;
		await fireEvent.input(field, { target: { value: 'hello' } });
		await settle();
		await fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
		await settle();
		return field;
	}

	it('keeps the message when the server refuses the request, so it can be sent again', async () => {
		const field = await sendFailing({ detail: 'Write a message first.', status: 422 });

		expect(screen.getByRole('alert').textContent).toBe('Write a message first.');
		expect(field.value).toBe('hello');
		expect((screen.getByRole('button', { name: 'Send to 3 chats' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('does not claim nothing was sent when the answer is lost', async () => {
		// fetch rejects with a bare TypeError when the connection drops mid-request.
		const field = await sendFailing(new TypeError('Failed to fetch'));

		expect(screen.getByRole('alert').textContent).toMatch(/Some chats may already have it: check them before sending again/);
		expect(field.value).toBe('hello');
	});

	it('will not send to more chats than the server takes', async () => {
		const many: BroadcastCandidate[] = Array.from({ length: 102 }, (_, i) => ({
			id: `c${i}`, title: `Chat ${i}`, state: 'idle', worker: null
		}));
		render(BroadcastDialog, { props: { open: true, candidates: many, onClose: vi.fn() } });
		await settle();
		await fireEvent.input(screen.getByRole('textbox', { name: 'Message' }), { target: { value: 'hello' } });
		await settle();

		expect((screen.getByRole('button', { name: 'Send to 102 chats' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByText('One message can go to 100 chats at most. Untick 2.')).toBeTruthy();
		await fireEvent.click(chip('Chat 0'));
		await fireEvent.click(chip('Chat 101'));
		await settle();
		expect((screen.getByRole('button', { name: 'Send to 100 chats' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('Escape closes it', async () => {
		const onClose = vi.fn();
		render(BroadcastDialog, { props: { open: true, candidates: chats, onClose } });
		await settle();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(onClose).toHaveBeenCalledOnce();
	});
});
