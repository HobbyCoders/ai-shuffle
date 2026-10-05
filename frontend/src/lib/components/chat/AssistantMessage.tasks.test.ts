import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import AssistantMessage from './AssistantMessage.svelte';
import { taskChecks, taskMessageKey } from '$lib/stores/task-checks';

const content = 'Checklist:\n\n- [ ] first\n- [x] second\n- [ ] third';

beforeEach(() => taskChecks.store.set({}));
afterEach(cleanup);

function boxes(container: HTMLElement): HTMLInputElement[] {
	return Array.from(container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
}

describe('task-list checkboxes in a chat message (#1813)', () => {
	it('can be ticked and unticked, and the tick is stored for the message', async () => {
		const view = render(AssistantMessage, { content, streaming: false, sessionId: 'chat-1' });
		await waitFor(() => expect(boxes(view.container)).toHaveLength(3));
		const [first, second] = boxes(view.container);
		expect(first.disabled).toBe(false);

		await fireEvent.click(first);
		await fireEvent.click(second);
		expect(first.checked).toBe(true);
		expect(second.checked).toBe(false);
		const key = taskMessageKey('chat-1', content);
		expect(taskChecks.store.current[key]).toEqual({ 0: true, 1: false });
	});

	it('shows stored ticks when the message renders again', async () => {
		taskChecks.setChecked(taskMessageKey('chat-1', content), 2, true);
		taskChecks.setChecked(taskMessageKey('chat-1', content), 1, false);
		const view = render(AssistantMessage, { content, streaming: false, sessionId: 'chat-1' });
		await waitFor(() => expect(boxes(view.container).map((box) => box.checked)).toEqual([false, false, true]));
	});

	it('stays read-only while the message is still streaming', async () => {
		const view = render(AssistantMessage, { content, streaming: true, sessionId: 'chat-1' });
		await waitFor(() => expect(boxes(view.container).length).toBeGreaterThan(0));
		expect(boxes(view.container).every((box) => box.disabled)).toBe(true);
	});
});
