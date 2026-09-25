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
	cleanup();
	document.body.innerHTML = '';
});

describe('PermissionQueue focus handling', () => {
	it('focuses the prompt on arrival and restores focus to the composer when the queue empties', async () => {
		const composer = document.createElement('textarea');
		document.body.appendChild(composer);
		composer.focus();

		const onRespond = vi.fn();
		const view = render(PermissionQueue, { props: { requests: [request], onRespond } });
		await settle();
		expect(document.activeElement).toBe(screen.getByRole('dialog', { name: /Bash|Permission/ }));

		// With focus on the prompt, the advertised Enter shortcut approves.
		await fireEvent.keyDown(document.activeElement!, { key: 'Enter' });
		expect(onRespond).toHaveBeenCalledWith(expect.objectContaining({ request_id: 'req-1', decision: 'allow' }));

		await view.rerender({ requests: [], onRespond });
		await settle();
		expect(document.activeElement).toBe(composer);
	});
});
