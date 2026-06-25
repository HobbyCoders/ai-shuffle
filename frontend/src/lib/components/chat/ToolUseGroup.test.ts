/**
 * Behavior tests for ToolUseGroup — specifically that tool-result screenshots
 * are HOISTED OUT of the collapsible group and rendered below it.
 *
 * The bug: when 2+ tools are grouped, the group's <details> is collapsed by
 * default; a screenshot rendered inside an inner ToolUseMessage was therefore
 * trapped inside the collapsed group where the user never sees it. The fix
 * renders each screenshot as a sibling AFTER the <details>, so it's visible
 * regardless of the group's expand state.
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup } from '@testing-library/svelte';
import ToolUseGroup from './ToolUseGroup.svelte';
import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
import type { ChatMessage } from '$lib/stores/types';

const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

function makeTool(over: Partial<ChatMessage> = {}): ChatMessage {
	return {
		id: 'tool-1',
		role: 'assistant',
		content: '',
		type: 'tool_use',
		toolName: 'Bash',
		toolStatus: 'complete',
		...over
	};
}

afterEach(() => {
	cleanup();
	chatPreferences.reset();
});

describe('ToolUseGroup screenshot hoisting', () => {
	it('renders a tool-result screenshot OUTSIDE the collapsible <details>', () => {
		const tools = [
			makeTool({ id: 't1', toolName: 'Read' }),
			makeTool({ id: 't2', toolName: 'mcp__claude-in-chrome__computer', toolScreenshot: PNG })
		];
		const { container } = render(ToolUseGroup, { props: { tools } });

		// The screenshot image is rendered...
		const shot = container.querySelector('.tool-screenshot img');
		expect(shot).not.toBeNull();

		// ...and it is NOT a descendant of the group's <details> — i.e. it stays
		// visible even while the group is collapsed (the whole point of the fix).
		expect(container.querySelector('details .tool-screenshot')).toBeNull();
		expect((shot as HTMLElement).closest('details')).toBeNull();
	});

	it('renders each screenshot exactly once (no inside+outside duplication)', () => {
		const tools = [
			makeTool({ id: 't1', toolName: 'Read', toolScreenshot: PNG }),
			makeTool({ id: 't2', toolName: 'Bash' })
		];
		const { container } = render(ToolUseGroup, { props: { tools } });

		expect(container.querySelectorAll('.tool-screenshot').length).toBe(1);
	});

	it('hoists multiple screenshots, one per tool that has one', () => {
		const tools = [
			makeTool({ id: 't1', toolName: 'computer', toolScreenshot: PNG }),
			makeTool({ id: 't2', toolName: 'Bash' }),
			makeTool({ id: 't3', toolName: 'computer', toolScreenshot: PNG })
		];
		const { container } = render(ToolUseGroup, { props: { tools } });

		expect(container.querySelectorAll('.tool-screenshot').length).toBe(2);
		// Both hoisted out of the details.
		expect(container.querySelector('details .tool-screenshot')).toBeNull();
	});

	it('renders no screenshot block when no tool carries one', () => {
		const tools = [
			makeTool({ id: 't1', toolName: 'Read' }),
			makeTool({ id: 't2', toolName: 'Bash' })
		];
		const { container } = render(ToolUseGroup, { props: { tools } });

		expect(container.querySelector('.tool-screenshot')).toBeNull();
	});
});
