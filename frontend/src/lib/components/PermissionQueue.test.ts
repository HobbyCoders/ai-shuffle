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
