/** Peer messages keep the received work-log presentation throughout delivery. */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import AgentMessage from './AgentMessage.svelte';
import ToolUseMessage from './ToolUseMessage.svelte';
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';

const peer = {
	sender: 'Provider usage review',
	senderId: 'review-session',
	content: 'The usage probe is still running.'
};

afterEach(() => {
	cleanup();
	chatPreferences.reset();
});

describe('AgentMessage', () => {
	it('uses the existing delivered ToolUseMessage without changing its markup', () => {
		const expected = render(ToolUseMessage, { name: 'Agent message',
			input: { label: peer.sender, session: peer.senderId },
			previewTitle: `${peer.sender} (session ${peer.senderId})`,
			result: peer.content, status: 'complete' });
		const delivered = render(AgentMessage, peer);
		expect(delivered.container.querySelector('.wl-item')?.outerHTML)
			.toBe(expected.container.querySelector('.wl-item')?.outerHTML);
	});

	it('shows a truthful queued state, not a completion or running spinner', async () => {
		const { container } = render(AgentMessage, { ...peer, queued: true });
		const row = screen.getByRole('button', { name: /Agent message: Provider usage review. Queued/ });
		expect(screen.getByText('Queued')).toBeTruthy();
		expect(container.querySelector('[data-testid="tool-status-queued"]')).toBeTruthy();
		expect(container.querySelector('[data-testid="tool-status-ok"], [data-testid="tool-status-running"]')).toBeNull();
		expect(container.querySelector('.userrow, .bubble, .peer-body')).toBeNull();
		expect(screen.queryByLabelText(/Unsend|Interrupt/)).toBeNull();
		await fireEvent.click(row);
		expect(screen.getByText(peer.content)).toBeTruthy();
		expect(container.querySelector('.wl-pane pre')?.textContent)
			.toBe(JSON.stringify({ label: peer.sender, session: peer.senderId }, null, 2));
	});

	it('updates only status on receipt, retaining the focused disclosure and expanded body', async () => {
		const view = render(AgentMessage, { ...peer, queued: true });
		const row = screen.getByRole('button');
		row.focus();
		await fireEvent.click(row);
		const body = view.container.querySelector('.wl-body');
		const result = screen.getByText(peer.content);
		await view.rerender({ ...peer, queued: false });
		expect(screen.getByRole('button')).toBe(row);
		expect(row.getAttribute('aria-expanded')).toBe('true');
		expect(document.activeElement).toBe(row);
		expect(view.container.querySelector('.wl-body')).toBe(body);
		expect(screen.getByText(peer.content)).toBe(result);
		expect(screen.queryByText('Queued')).toBeNull();
		expect(row.getAttribute('aria-label')).toContain('Complete');
		expect(view.container.querySelector('[data-testid="tool-status-ok"]')).toBeTruthy();
		await fireEvent.click(row);
		expect(row.getAttribute('aria-expanded')).toBe('false');
	});

	it('renders long untrusted bodies and sender names as text in the same disclosure', async () => {
		const content = '<img src=x onerror=alert(1)> **untrusted** '.repeat(100);
		const sender = '<script>another agent</script>';
		const { container } = render(AgentMessage, { sender, content, queued: true });
		await fireEvent.click(screen.getByRole('button'));
		expect(container.querySelectorAll('.wl-pane pre')[1]?.textContent).toBe(content);
		expect(container.querySelector('.wtxt')?.textContent).toBe(sender);
		expect(container.querySelector('img, script')).toBeNull();
	});

	it('keeps identical-text messages independent when just one is received', async () => {
		const first = render(AgentMessage, { ...peer, queued: true });
		const second = render(AgentMessage, { ...peer, queued: true });
		const rows = screen.getAllByRole('button');
		await fireEvent.click(rows[0]);
		await first.rerender({ ...peer, queued: false });
		expect(screen.getAllByRole('button')).toHaveLength(2);
		expect(rows[0].getAttribute('aria-expanded')).toBe('true');
		expect(rows[1].getAttribute('aria-expanded')).toBe('false');
		expect(first.container.querySelector('[data-testid="tool-status-ok"]')).toBeTruthy();
		expect(second.container.querySelector('[data-testid="tool-status-queued"]')).toBeTruthy();
	});
});
