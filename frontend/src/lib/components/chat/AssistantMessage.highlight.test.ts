import { render, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi, beforeEach } from 'vitest';

// Shiki loads its core and each grammar lazily. This mock lets a test drive the
// "a grammar landed after the message already rendered" transition, which is the
// case AssistantMessage's content-keyed memo used to miss entirely.
let generation = 0;
let listeners: Array<() => void> = [];

vi.mock('$lib/utils/shiki', () => ({
	highlightGenerationForSource: (md: string) =>
		md.includes('```') || md.includes('~~~') ? generation : 0,
	highlightGeneration: () => generation,
	onHighlighterReady: (cb: () => void) => {
		listeners.push(cb);
		return () => {
			listeners = listeners.filter((l) => l !== cb);
		};
	},
	resolveShikiLang: (l: string) => l,
	warmHighlighter: vi.fn(),
	isHighlightReady: () => generation > 0,
}));

// Count how often the markdown pipeline actually re-runs for a given content.
const renderSpy = vi.fn();
vi.mock('$lib/utils/chat-markdown', () => ({
	renderChatMarkdown: (text: string) => {
		renderSpy(text);
		// Emulate the real fallback: plain text before a grammar is resident,
		// "highlighted" markup afterwards.
		return generation > 0
			? `<pre class="shiki"><code>${text}</code></pre>`
			: `<pre><code>${text}</code></pre>`;
	}
}));

import AssistantMessage from './AssistantMessage.svelte';

const WITH_CODE = 'here you go:\n\n```ts\nconst a: number = 1;\n```\n';
const NO_CODE = 'just prose, no fences at all';

function mount(content: string) {
	return render(AssistantMessage, {
		props: { content, messageIndex: 0, messageId: 'm1', streaming: false }
	});
}

describe('AssistantMessage highlight invalidation', () => {
	beforeEach(() => {
		generation = 0;
		listeners = [];
		renderSpy.mockClear();
	});

	it('re-renders a settled message when its grammar lands late', async () => {
		const { container } = mount(WITH_CODE);
		await waitFor(() => expect(container.querySelector('pre')).toBeTruthy());
		expect(container.querySelector('pre.shiki')).toBeNull();

		// The grammar arrives after the message already settled.
		generation = 1;
		listeners.forEach((l) => l());

		await waitFor(() => {
			expect(container.querySelector('pre.shiki')).toBeTruthy();
		});
	});

	it('does NOT invalidate a message with no code fences', async () => {
		const { container } = mount(NO_CODE);
		await waitFor(() => expect(container.textContent).toContain('just prose'));
		const before = renderSpy.mock.calls.length;

		generation = 1;
		listeners.forEach((l) => l());
		await Promise.resolve();

		// Generation for fence-less source stays 0, so the memo key is unchanged.
		expect(renderSpy.mock.calls.length).toBe(before);
	});

	it('stops listening for readiness once destroyed', async () => {
		const { unmount } = mount(WITH_CODE);
		expect(listeners.length).toBeGreaterThan(0);
		unmount();
		expect(listeners.length).toBe(0);
	});
});
