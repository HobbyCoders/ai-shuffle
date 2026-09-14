import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Pin the actual composer markup without mocking its session/store graph.
const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'), 'utf8');
const streaming = source.split('{:else if $activeSession.isStreaming}')[1].split('\n\t\t\t\t\t\t{:else}')[0];

describe('streaming composer actions', () => {
	it('renders Send and Stop in mutually exclusive draft branches', () => {
		expect(streaming).toMatch(/\{#if plainTextValue\.trim\(\)\}\s*<button[^>]*type="submit"[^>]*aria-label="Send"[\s\S]*?<\/button>\s*\{:else\}\s*<StopButton[\s\S]*?\/>\s*\{\/if\}/);
		expect(streaming.match(/<StopButton/g)).toHaveLength(1);
	});

	it('preserves task-aware stopping for an empty or whitespace-only draft', () => {
		expect(streaming).toContain('activeTasks={$activeSession.activeTasks}');
		expect(streaming).toContain('activeSession.stopGeneration()');
	});
});
