import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import MonitorWatch from './MonitorWatch.svelte';

class Socket {
	static instances: Socket[] = [];
	onmessage: ((event: { data: string }) => void) | null = null;
	onclose: ((event: { code: number }) => void) | null = null;
	onerror: (() => void) | null = null;
	send = vi.fn();
	close = vi.fn();
	constructor(public url: string) { Socket.instances.push(this); }
	frame(lines: string[], extra = {}) { this.onmessage?.({ data: JSON.stringify({ type: 'output', readonly: true, lines, task: { id: 'task-a', status: 'running', exit_code: null }, ...extra }) }); }
}
const tasks = [
	{ id: 'task-a', subject: 'Build app', status: 'running', exit_code: null },
	{ id: 'task-b', subject: 'Run checks', status: 'completed', exit_code: 0 },
];
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
beforeEach(() => {
	Socket.instances = [];
	vi.stubGlobal('WebSocket', Socket);
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ tasks })));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function mounted() {
	const result = render(MonitorWatch, { sessionId: 'session/one' });
	await waitFor(() => expect(Socket.instances).toHaveLength(1));
	return result;
}

describe('read-only Monitor viewer', () => {
	it('replaces retained snapshots, escapes markup and never sends input or resize', async () => {
		await mounted();
		const socket = Socket.instances[0];
		expect(socket.url).toContain('/terminals/monitor/task-a?session_id=session%2Fone');
		socket.frame(['first']);
		const output = await screen.findByRole('region', { name: 'Read-only command output' });
		await waitFor(() => expect(output.textContent).toBe('first'));
		socket.frame(['<script>bad()</script>', 'second'], { truncated: true });
		await waitFor(() => expect(output.textContent).toBe('<script>bad()</script>\nsecond'));
		expect(output.querySelector('script')).toBeNull();
		expect(screen.getByText(/Earlier output was trimmed/)).toBeTruthy();
		await fireEvent.keyDown(output, { key: 'a' });
		await fireEvent.paste(output);
		await fireEvent(window, new Event('resize'));
		expect(socket.send).not.toHaveBeenCalled();
		expect(screen.queryByRole('textbox')).toBeNull();
		expect(screen.getByText('Read-only')).toBeTruthy();
	});

	it('detaches the old stream on selection and on unmount, without stopping commands', async () => {
		const view = await mounted();
		const first = Socket.instances[0];
		await fireEvent.change(screen.getByLabelText('Background command'), { target: { value: 'task-b' } });
		await waitFor(() => expect(Socket.instances).toHaveLength(2));
		expect(first.close).toHaveBeenCalledOnce();
		first.frame(['stale output']);
		expect(screen.queryByText('stale output')).toBeNull();
		const second = Socket.instances[1];
		second.frame(['done'], { finished: true, task: tasks[1] });
		second.onclose?.({ code: 1000 });
		expect(await screen.findByText(/Output complete/)).toBeTruthy();
		expect(screen.getByText('completed · exit 0')).toBeTruthy();
		view.unmount();
		expect(second.close).toHaveBeenCalledOnce();
		expect(first.send).not.toHaveBeenCalled();
		expect(second.send).not.toHaveBeenCalled();
		expect(vi.mocked(fetch).mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
	});

	it('shows owner refusal and reconnects only on an explicit retry', async () => {
		await mounted();
		Socket.instances[0].onclose?.({ code: 4001 });
		expect(await screen.findByText(/Only the owner can watch/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Reconnect' }));
		await waitFor(() => expect(Socket.instances).toHaveLength(2));
	});

	it('shows empty and refused listing states without a socket', async () => {
		vi.mocked(fetch).mockResolvedValueOnce(json({ tasks: [] }));
		const view = render(MonitorWatch, { sessionId: 'empty' });
		expect(await screen.findByText('No background commands in this chat')).toBeTruthy();
		expect(Socket.instances).toHaveLength(0);
		view.unmount();
		vi.mocked(fetch).mockResolvedValueOnce(json({ detail: 'Only the owner may watch.' }, 403));
		render(MonitorWatch, { sessionId: 'denied' });
		expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Only the owner may watch.Try again');
		expect(Socket.instances).toHaveLength(0);
	});
});
