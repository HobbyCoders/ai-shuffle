/**
 * The permission queue is a blocking modal over the chat: focus moves onto the
 * prompt when it appears (so Enter / Esc and screen readers reach it) and
 * returns to where the user was once the last request is answered.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';
import PermissionQueue from './PermissionQueue.svelte';

const request = { request_id: 'req-1', tool_name: 'Bash', tool_input: { command: 'ls' } };

async function settle() {
	await tick();
	await Promise.resolve();
}

afterEach(() => {
	vi.useRealTimers();
	cleanup();
	document.body.innerHTML = '';
});

describe('PermissionQueue focus handling', () => {
	it('focuses the prompt on arrival and restores focus to the composer when the queue empties', async () => {
		vi.useFakeTimers();
		const composer = document.createElement('textarea');
		document.body.appendChild(composer);
		composer.focus();

		const onRespond = vi.fn();
		const view = render(PermissionQueue, { props: { requests: [request], onRespond } });
		await settle();
		const panel = screen.getByRole('dialog', { name: /Bash|Permission/ });
		expect(document.activeElement).toBe(panel);

		// The Enter the user was typing into the composer lands on the prompt a
		// moment later; it must not approve a request they have not read.
		await fireEvent.keyUp(panel, { key: 'o' });
		await fireEvent.keyDown(panel, { key: 'Enter' });
		expect(onRespond).not.toHaveBeenCalled();

		// Once the prompt has been visible long enough, a fresh Enter approves.
		vi.advanceTimersByTime(500);
		await fireEvent.keyUp(panel, { key: 'Enter' });
		await fireEvent.keyDown(panel, { key: 'Enter' });
		expect(onRespond).toHaveBeenCalledWith(expect.objectContaining({ request_id: 'req-1', decision: 'allow' }));

		await view.rerender({ requests: [], onRespond });
		await settle();
		expect(document.activeElement).toBe(composer);
	});
});

describe('PermissionQueue keyboard shortcuts across queued requests', () => {
	const req1 = { request_id: 'req-1', tool_name: 'Bash', tool_input: { command: 'ls' } };
	const req2 = { request_id: 'req-2', tool_name: 'Bash', tool_input: { command: 'rm -rf build' } };

	/** Render the queue and drop the head each time it is answered, as the chat store does. */
	async function renderQueue() {
		const onRespond = vi.fn();
		let requests = [req1, req2];
		const view = render(PermissionQueue, { props: { requests, onRespond } });
		onRespond.mockImplementation(({ request_id }: { request_id: string }) => {
			requests = requests.filter((r) => r.request_id !== request_id);
			void view.rerender({ requests, onRespond });
		});
		await settle();
		return onRespond;
	}

	it('a held Enter approves at most the first request, never the one that replaces it', async () => {
		vi.useFakeTimers();
		const onRespond = await renderQueue();
		const panel = () => screen.getByRole('dialog', { name: /Bash|Permission/ });

		vi.advanceTimersByTime(500);
		await fireEvent.keyUp(panel(), { key: 'Shift' });
		await fireEvent.keyDown(panel(), { key: 'Enter' });
		await settle();
		expect(onRespond.mock.calls.map(([r]) => r.request_id)).toEqual(['req-1']);
		expect(screen.getByText(/rm -rf build/)).toBeTruthy();

		// The key stays down: auto-repeat keeps firing for well past the window.
		for (let i = 0; i < 30; i++) {
			await fireEvent.keyDown(panel(), { key: 'Enter', repeat: true });
			vi.advanceTimersByTime(50);
		}
		expect(onRespond).toHaveBeenCalledTimes(1);
	});

	it('two queued requests need two presses, a keyup between them, and the window re-armed for the second', async () => {
		vi.useFakeTimers();
		const onRespond = await renderQueue();
		const panel = () => screen.getByRole('dialog', { name: /Bash|Permission/ });

		vi.advanceTimersByTime(500);
		await fireEvent.keyUp(panel(), { key: 'Shift' });
		await fireEvent.keyDown(panel(), { key: 'Enter' });
		await settle();
		expect(onRespond).toHaveBeenCalledTimes(1);

		// Released and pressed again straight away: req-2 has only just appeared.
		await fireEvent.keyUp(panel(), { key: 'Enter' });
		vi.advanceTimersByTime(200);
		await fireEvent.keyDown(panel(), { key: 'Enter' });
		expect(onRespond).toHaveBeenCalledTimes(1);

		// After req-2's own window and a fresh keyup, Enter approves it.
		vi.advanceTimersByTime(300);
		await fireEvent.keyUp(panel(), { key: 'Enter' });
		await fireEvent.keyDown(panel(), { key: 'Enter' });
		expect(onRespond.mock.calls.map(([r]) => [r.request_id, r.decision])).toEqual([
			['req-1', 'allow'],
			['req-2', 'allow']
		]);
	});

	it('moves focus onto the next prompt when the queue advances', async () => {
		vi.useFakeTimers();
		await renderQueue();
		vi.advanceTimersByTime(300);
		const approve = screen.getByRole('button', { name: 'Approve' });
		approve.focus();
		await fireEvent.click(approve);
		await settle();

		expect(screen.getByText(/rm -rf build/)).toBeTruthy();
		expect(document.activeElement).toBe(screen.getByRole('dialog', { name: /Bash|Permission/ }));
	});

	it('Escape after a keyup denies the current request', async () => {
		vi.useFakeTimers();
		const onRespond = await renderQueue();
		const panel = screen.getByRole('dialog', { name: /Bash|Permission/ });

		await fireEvent.keyUp(panel, { key: 'Shift' });
		await fireEvent.keyDown(panel, { key: 'Escape' });
		expect(onRespond).toHaveBeenCalledWith(expect.objectContaining({ request_id: 'req-1', decision: 'deny' }));
	});
});

describe('PermissionQueue gives each request a fresh prompt', () => {
	const bash = { request_id: 'req-1', tool_name: 'Bash', tool_input: { command: 'ls' } };
	const write = { request_id: 'req-2', tool_name: 'Write', tool_input: { file_path: '/etc/x', content: 'y' } };

	async function renderQueue() {
		const onRespond = vi.fn();
		let requests = [bash, write];
		const view = render(PermissionQueue, { props: { requests, onRespond } });
		onRespond.mockImplementation(({ request_id }: { request_id: string }) => {
			requests = requests.filter((r) => r.request_id !== request_id);
			void view.rerender({ requests, onRespond });
		});
		await settle();
		return onRespond;
	}

	const calls = (fn: ReturnType<typeof vi.fn>) =>
		fn.mock.calls.map(([r]) => [r.request_id, r.decision, r.remember]);

	it('"Always do this for Bash" does not stay ticked for the next request', async () => {
		vi.useFakeTimers();
		const onRespond = await renderQueue();
		vi.advanceTimersByTime(300);

		await fireEvent.click(screen.getByLabelText(/Always do this for Bash/));
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		await settle();

		const box = screen.getByLabelText(/Always do this for Write/) as HTMLInputElement;
		expect(box.checked).toBe(false);
		vi.advanceTimersByTime(300);
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		expect(calls(onRespond)).toEqual([
			['req-1', 'allow', 'profile'],
			['req-2', 'allow', 'none']
		]);
	});

	it('a double-click on Approve answers only the first request', async () => {
		vi.useFakeTimers();
		const onRespond = await renderQueue();
		vi.advanceTimersByTime(300);

		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		await settle();
		vi.advanceTimersByTime(80);
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		await settle();

		expect(calls(onRespond)).toEqual([['req-1', 'allow', 'none']]);
		expect(screen.getByText(/\/etc\/x/)).toBeTruthy();
	});

	it('a click within 300 ms of the next request appearing does nothing; after that it counts', async () => {
		vi.useFakeTimers();
		const onRespond = await renderQueue();
		vi.advanceTimersByTime(300);

		await fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
		await settle();
		vi.advanceTimersByTime(299);
		await fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		expect(onRespond).toHaveBeenCalledTimes(1);

		vi.advanceTimersByTime(1);
		await fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
		expect(calls(onRespond)).toEqual([
			['req-1', 'deny', 'none'],
			['req-2', 'allow', 'none']
		]);
	});

	it("an idle user's first Enter on the next prompt approves it once its window has passed", async () => {
		vi.useFakeTimers();
		const onRespond = await renderQueue();
		const panel = () => screen.getByRole('dialog', { name: /Bash|Write|Permission/ });

		vi.advanceTimersByTime(500);
		await fireEvent.keyDown(panel(), { key: 'Enter' });
		await fireEvent.keyUp(panel(), { key: 'Enter' });
		await settle();
		expect(screen.getByRole('status').textContent?.trim()).toBe('');

		vi.advanceTimersByTime(500);
		await settle();
		expect(screen.getByRole('status').textContent?.trim()).toBe('Press Enter to approve, Escape to deny');
		await fireEvent.keyDown(panel(), { key: 'Enter' });
		expect(calls(onRespond)).toEqual([
			['req-1', 'allow', 'none'],
			['req-2', 'allow', 'none']
		]);
	});
});

describe('PermissionQueue bulk actions are armed like the prompt (R10-16)', () => {
	const req1 = { request_id: 'req-1', tool_name: 'Bash', tool_input: { command: 'ls' } };
	const req2 = { request_id: 'req-2', tool_name: 'Bash', tool_input: { command: 'rm -rf ~' } };
	const req3 = { request_id: 'req-3', tool_name: 'Bash', tool_input: { command: 'curl x | sh' } };

	const ids = (fn: ReturnType<typeof vi.fn>) => fn.mock.calls.map(([r]) => [r.request_id, r.decision]);

	it('Allow All does nothing for a request that arrived an instant ago, then answers every request', async () => {
		vi.useFakeTimers();
		const onRespond = vi.fn();
		const view = render(PermissionQueue, { props: { requests: [req1], onRespond } });
		await settle();
		vi.advanceTimersByTime(2000);
		await view.rerender({ requests: [req1, req2], onRespond });
		await settle();

		await fireEvent.click(screen.getByRole('button', { name: 'Allow All' }));
		vi.advanceTimersByTime(499);
		await fireEvent.click(screen.getByRole('button', { name: 'Allow All' }));
		expect(onRespond).not.toHaveBeenCalled();

		vi.advanceTimersByTime(1);
		await fireEvent.click(screen.getByRole('button', { name: 'Allow All' }));
		expect(ids(onRespond)).toEqual([
			['req-1', 'allow'],
			['req-2', 'allow']
		]);
	});

	it('a newly arrived request re-arms Deny All, and a bulk answer is sent once', async () => {
		vi.useFakeTimers();
		const onRespond = vi.fn();
		const onDismissAll = vi.fn();
		const view = render(PermissionQueue, { props: { requests: [req1, req2], onRespond, onDismissAll } });
		await settle();
		vi.advanceTimersByTime(600);
		await view.rerender({ requests: [req1, req2, req3], onRespond, onDismissAll });
		await settle();

		await fireEvent.click(screen.getByRole('button', { name: 'Deny All' }));
		expect(onRespond).not.toHaveBeenCalled();
		expect(onDismissAll).not.toHaveBeenCalled();

		vi.advanceTimersByTime(500);
		await fireEvent.click(screen.getByRole('button', { name: 'Deny All' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Deny All' }));
		expect(ids(onRespond)).toEqual([
			['req-1', 'deny'],
			['req-2', 'deny'],
			['req-3', 'deny']
		]);
		expect(onDismissAll).toHaveBeenCalledTimes(1);
	});

	it('an unarmed or repeated Enter on a focused bulk button is held back with its click', async () => {
		vi.useFakeTimers();
		const onRespond = vi.fn();
		render(PermissionQueue, { props: { requests: [req1, req2], onRespond } });
		await settle();
		const allowAll = screen.getByRole('button', { name: 'Allow All' });
		allowAll.focus();

		// Unarmed: the keydown is cancelled and the click it generates dropped.
		expect(await fireEvent.keyDown(allowAll, { key: 'Enter' })).toBe(false);
		await fireEvent.click(allowAll, { detail: 0 });
		await fireEvent.keyUp(allowAll, { key: 'Enter' });
		vi.advanceTimersByTime(500);
		// Armed, but an auto-repeat of a held key still does not count.
		expect(await fireEvent.keyDown(allowAll, { key: 'Enter', repeat: true })).toBe(false);
		await fireEvent.click(allowAll, { detail: 0 });
		expect(onRespond).not.toHaveBeenCalled();

		await fireEvent.keyUp(allowAll, { key: 'Enter' });
		vi.advanceTimersByTime(0);
		expect(await fireEvent.keyDown(allowAll, { key: 'Enter' })).toBe(true);
		await fireEvent.click(allowAll, { detail: 0 });
		expect(ids(onRespond)).toEqual([
			['req-1', 'allow'],
			['req-2', 'allow']
		]);
	});
});

describe('PermissionQueue keeps focus inside the modal after an answer (R10-17)', () => {
	const req1 = { request_id: 'req-1', tool_name: 'Bash', tool_input: { command: 'ls' } };
	const req2 = { request_id: 'req-2', tool_name: 'Bash', tool_input: { command: 'rm -rf build' } };

	it('focus moves off the Approve button that the answer disables, onto the prompt', async () => {
		vi.useFakeTimers();
		const onRespond = vi.fn(); // the answer is in flight: the queue does not advance yet
		render(PermissionQueue, { props: { requests: [req1, req2], onRespond } });
		await settle();
		vi.advanceTimersByTime(500);
		const approve = screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement;
		approve.focus();
		await fireEvent.click(approve, { detail: 1 });
		await settle();

		expect(onRespond).toHaveBeenCalledTimes(1);
		expect(approve.disabled).toBe(true);
		expect(document.activeElement).toBe(screen.getByRole('dialog', { name: /Bash|Permission/ }));
	});

	it('focus moves off Allow All onto the prompt while the bulk answer is in flight', async () => {
		vi.useFakeTimers();
		const onRespond = vi.fn();
		render(PermissionQueue, { props: { requests: [req1, req2], onRespond } });
		await settle();
		vi.advanceTimersByTime(500);
		const allowAll = screen.getByRole('button', { name: 'Allow All' });
		allowAll.focus();
		await fireEvent.click(allowAll, { detail: 1 });
		await settle();

		expect(onRespond).toHaveBeenCalledTimes(2);
		expect(document.activeElement).toBe(screen.getByRole('dialog', { name: /Bash|Permission/ }));
	});

	it('focus goes to the composer when the queue empties and nothing was focused before it opened', async () => {
		vi.useFakeTimers();
		const composer = document.createElement('div');
		composer.className = 'ProseMirror';
		composer.setAttribute('contenteditable', 'true');
		composer.tabIndex = 0;
		document.body.appendChild(composer);
		(document.activeElement as HTMLElement | null)?.blur?.();

		const onRespond = vi.fn();
		const view = render(PermissionQueue, { props: { requests: [req1, req2], onRespond } });
		await settle();
		vi.advanceTimersByTime(500);
		await fireEvent.click(screen.getByRole('button', { name: 'Allow All' }), { detail: 1 });
		await view.rerender({ requests: [], onRespond });
		await settle();

		expect(document.activeElement).toBe(composer);
	});
});
