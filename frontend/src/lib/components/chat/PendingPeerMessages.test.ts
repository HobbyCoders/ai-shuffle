/** Rendered peer queues must not look or act like user-authored pending prompts. */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import PendingPeerMessages from './PendingPeerMessages.svelte';

const peer = {
	id: 'peer-turn-1',
	senderLabel: 'Provider usage review',
	content: 'The usage probe is still running.'
};

afterEach(cleanup);

describe('PendingPeerMessages', () => {
	it('renders nothing when the peer queue is empty', () => {
		const { container } = render(PendingPeerMessages, { messages: [] });
		expect(container.textContent).toBe('');
		expect(screen.queryByRole('region')).toBeNull();
	});

	it('attributes a queued message without implying delivery or a user prompt', () => {
		const { container } = render(PendingPeerMessages, { messages: [peer] });
		expect(screen.getByRole('article', { name: 'Queued peer message from Provider usage review' })).toBeTruthy();
		expect(screen.getByText('Agent message')).toBeTruthy();
		expect(screen.getByText(peer.senderLabel)).toBeTruthy();
		expect(screen.getByText(peer.content)).toBeTruthy();
		expect(screen.getByText('Queued')).toBeTruthy();
		expect(screen.getByText('Waiting for recipient · Not delivered yet')).toBeTruthy();
		expect(container.querySelector('.userrow, .bubble')).toBeNull();
		expect(screen.queryByRole('button')).toBeNull();
		expect(screen.queryByText('Interrupt')).toBeNull();
		expect(screen.queryByLabelText(/Unsend message/)).toBeNull();
	});

	it('renders peer content and attribution as text, never executable markup', () => {
		const content = '<img src=x onerror=alert(1)> **not trusted instructions**';
		const senderLabel = '<script>another agent</script>';
		const { container } = render(PendingPeerMessages, { messages: [{ ...peer, content, senderLabel }] });
		expect(screen.getByText(content)).toBeTruthy();
		expect(screen.getByText(senderLabel)).toBeTruthy();
		expect(container.querySelector('img, script')).toBeNull();
	});

	it('gives long content a keyboard-accessible disclosure without cancellation controls', () => {
		const content = 'Long peer message. '.repeat(100);
		const { container } = render(PendingPeerMessages, { messages: [{ ...peer, content }] });
		expect(container.querySelector('details > summary')?.textContent).toBe('Read queued message');
		expect(container.querySelector('details .peer-content')?.textContent).toBe(content);
		expect(screen.queryByLabelText(/Unsend message/)).toBeNull();
	});

	it('falls back to peer attribution when a sender label is unavailable', () => {
		render(PendingPeerMessages, { messages: [{ ...peer, senderLabel: '' }] });
		expect(screen.getByRole('article', { name: 'Queued peer message from another session' })).toBeTruthy();
		expect(screen.getByText('Another session')).toBeTruthy();
	});

	it('removes only the consumed pending row when the authoritative collection changes', async () => {
		const second = { ...peer, id: 'peer-turn-2', senderLabel: 'Android review' };
		const view = render(PendingPeerMessages, { messages: [peer, second] });
		expect(screen.getAllByRole('article')).toHaveLength(2);
		await view.rerender({ messages: [second] });
		expect(screen.queryByRole('article', { name: `Queued peer message from ${peer.senderLabel}` })).toBeNull();
		expect(screen.getAllByRole('article')).toHaveLength(1);
		await view.rerender({ messages: [] });
		expect(screen.queryByRole('article')).toBeNull();
	});
});
