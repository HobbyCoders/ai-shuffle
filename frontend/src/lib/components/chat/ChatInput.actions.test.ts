import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Pin the actual composer markup without mocking its session/store graph.
const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'), 'utf8');
const streaming = source.split('{:else if $activeSession.isStreaming}')[1].split('\n\t\t\t\t\t\t{:else}')[0];

describe('streaming composer actions', () => {
	it('counts serialized file references and legacy uploads as sendable content', () => {
		expect(source).toContain('const hasMessageContent = $derived(!!inputValue.trim() || uploadedFiles.length > 0)');
		expect(source).toContain('hasPrompt: !!finalPrompt.trim() || uploadedFiles.length > 0');
		expect(source).not.toContain('inputValue = editorRef?.getText()');
	});

	it('uses the same content gate while idle and retains upload blocking during streaming', () => {
		expect(source).toContain('disabled={!hasMessageContent || !$anyCredential || isUploading || isRecording || isTranscribing}');
		expect(streaming).toContain('disabled={!$anyCredential || isUploading || isRecording || isTranscribing}');
	});

	it('renders Send and Stop in mutually exclusive draft branches', () => {
		expect(streaming).toMatch(/\{#if hasMessageContent\}\s*<button[^>]*type="submit"[^>]*aria-label="Send"[\s\S]*?<\/button>\s*\{:else\}\s*<StopButton[\s\S]*?\/>\s*\{\/if\}/);
		expect(streaming.match(/<StopButton/g)).toHaveLength(1);
	});

	it('preserves task-aware stopping for an empty or whitespace-only draft', () => {
		expect(streaming).toContain('activeTasks={$activeSession.activeTasks}');
		expect(streaming).toContain('activeSession.stopGeneration()');
	});
});
