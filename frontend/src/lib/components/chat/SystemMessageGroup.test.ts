/**
 * Behavior tests for SystemMessageGroup's collapsed label. When every grouped
 * message is a background-task subtype the group reads "N task updates"; the
 * existing "N hooks" and generic "N system messages" labels are unchanged.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/svelte';
import SystemMessageGroup from './SystemMessageGroup.svelte';
import type { ChatMessage } from '$lib/stores/types';

let seq = 0;
function makeMessage(subtype: string, over: Partial<ChatMessage> = {}): ChatMessage {
	return {
		id: `sys-${seq++}`,
		role: 'system',
		content: '',
		systemSubtype: subtype,
		...over
	};
}

afterEach(() => {
	cleanup();
});

describe('SystemMessageGroup — label', () => {
	it('labels an all-task group "N task updates"', () => {
		const messages = [
			makeMessage('task_started'),
			makeMessage('task_progress'),
			makeMessage('task_updated', { systemData: { task_id: 'x', patch: { status: 'running' } } })
		];
		render(SystemMessageGroup, { props: { messages } });
		expect(screen.getByText('3 task updates')).toBeTruthy();
	});

	it('uses the singular "1 task update" for a single-task group', () => {
		const messages = [
			makeMessage('task_notification', { systemData: { task_id: 'x', patch: { status: 'completed' } } })
		];
		render(SystemMessageGroup, { props: { messages } });
		expect(screen.getByText('1 task update')).toBeTruthy();
	});

	it('keeps the "N hooks" label for an all-hook group', () => {
		const messages = [makeMessage('hook_started'), makeMessage('hook_response')];
		render(SystemMessageGroup, { props: { messages } });
		expect(screen.getByText('2 hooks')).toBeTruthy();
	});

	it('falls back to "N system messages" for a mixed group', () => {
		const messages = [makeMessage('task_updated'), makeMessage('hook_started')];
		render(SystemMessageGroup, { props: { messages } });
		expect(screen.getByText('2 system messages')).toBeTruthy();
	});
});
