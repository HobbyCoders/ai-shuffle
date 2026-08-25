/**
 * Behavior tests for the AskUserQuestion "dismissed" rendering branch.
 *
 * Closing the question modal ("X") resolves the pending tool call as a DENY
 * carrying the backend's dismissal text. That is the right mechanic, but the
 * card used to render it with the same red error chrome as a real permission
 * denial — so a deliberate, benign choice read as a failure. These tests pin
 * the distinct neutral state, and (just as importantly) that it stays narrow:
 * any other denial keeps the error rendering.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import ToolUseMessage from './ToolUseMessage.svelte';
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
import {
	QUESTION_DISMISS_MESSAGE,
	QUESTION_DISMISSED_LABEL,
	isDismissedQuestion
} from './question-outcome';

afterEach(() => {
	cleanup();
	chatPreferences.reset();
});

function renderTool(over: Record<string, unknown> = {}) {
	return render(ToolUseMessage, {
		props: {
			name: 'AskUserQuestion',
			input: { questions: [{ question: 'Pick one', options: [] }] },
			result: QUESTION_DISMISS_MESSAGE,
			status: 'error',
			...over
		}
	});
}

describe('ToolUseMessage — dismissed AskUserQuestion', () => {
	it('renders a dismissed label instead of the generic error chrome', () => {
		const { container } = renderTool();

		expect(screen.getByText(QUESTION_DISMISSED_LABEL)).toBeTruthy();
		expect(screen.getByText('dismissed')).toBeTruthy();
		// The neutral status mark replaces the red error X.
		expect(container.querySelector('[data-testid="tool-dismissed-icon"]')).not.toBeNull();
		expect(container.querySelector('[data-testid="tool-status-error"]')).toBeNull();
	});

	it('shows the dismissal text in the result pane, muted', async () => {
		const { container } = renderTool();

		// Rows start collapsed; the panes only exist once expanded.
		await fireEvent.click(container.querySelector('.wlrow') as HTMLElement);

		const pane = container.querySelector('[data-testid="tool-dismissed-result"]');
		expect(pane).not.toBeNull();
		expect(pane?.textContent).toContain(QUESTION_DISMISS_MESSAGE);
		expect(pane?.className).toContain('is-muted');
	});

	it('keeps error styling for an AskUserQuestion denied for another reason', () => {
		const { container } = renderTool({
			result: "Tool 'AskUserQuestion' is disabled in this profile"
		});

		expect(container.querySelector('[data-testid="tool-status-error"]')).not.toBeNull();
		expect(screen.queryByText(QUESTION_DISMISSED_LABEL)).toBeNull();
	});

	it('keeps error styling for a different tool carrying the same text', () => {
		const { container } = renderTool({ name: 'Bash', input: { command: 'ls' } });

		expect(container.querySelector('[data-testid="tool-status-error"]')).not.toBeNull();
		expect(screen.queryByText(QUESTION_DISMISSED_LABEL)).toBeNull();
	});

	it('leaves an answered (non-error) question alone', () => {
		const { container } = renderTool({
			status: 'complete',
			result: 'User skipped this question.'
		});

		expect(screen.queryByText(QUESTION_DISMISSED_LABEL)).toBeNull();
		expect(container.querySelector('[data-testid="tool-status-ok"]')).not.toBeNull();
	});
});

describe('isDismissedQuestion', () => {
	it('requires the tool, the error outcome, and the exact text', () => {
		expect(isDismissedQuestion('AskUserQuestion', 'error', QUESTION_DISMISS_MESSAGE)).toBe(true);
		expect(isDismissedQuestion('AskUser', 'error', QUESTION_DISMISS_MESSAGE)).toBe(true);
		expect(isDismissedQuestion('AskUserQuestion', 'complete', QUESTION_DISMISS_MESSAGE)).toBe(false);
		expect(isDismissedQuestion('Bash', 'error', QUESTION_DISMISS_MESSAGE)).toBe(false);
		expect(isDismissedQuestion('AskUserQuestion', 'error', 'permission denied')).toBe(false);
		expect(isDismissedQuestion('AskUserQuestion', 'error', undefined)).toBe(false);
	});

	it('tolerates the CLI wrapping the deny message in surrounding text', () => {
		expect(
			isDismissedQuestion(
				'AskUserQuestion',
				'error',
				`<tool_use_error>${QUESTION_DISMISS_MESSAGE}</tool_use_error>`
			)
		).toBe(true);
	});
});

describe('ToolUseMessage — compact live and failure states', () => {
	it('uses a concise action label for a running command', () => {
		render(ToolUseMessage, {
			props: {
				name: 'Bash',
				input: { command: 'pnpm run check' },
				status: 'running',
				streaming: true
			}
		});
		expect(screen.getByText('Running pnpm')).toBeTruthy();
		expect(screen.getByRole('button', { name: /Running pnpm: pnpm run check\. Running/ })).toBeTruthy();
	});

	it('keeps the failed state explicit in text and icon', () => {
		const { container } = render(ToolUseMessage, {
			props: { name: 'Bash', input: { command: 'false' }, result: 'exit 1', status: 'error' }
		});
		expect(screen.getByText('Failed')).toBeTruthy();
		expect(container.querySelector('[data-testid="tool-status-error"]')).not.toBeNull();
		expect(screen.getByRole('button', { name: /Bash: false\. Failed/ })).toBeTruthy();
	});
});
