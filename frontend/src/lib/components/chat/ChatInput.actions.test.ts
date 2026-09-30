import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Pin the actual composer markup without mocking its session/store graph.
const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'), 'utf8');
const streaming = source.split('{:else if stoppableWork}')[1].split('\n\t\t\t\t\t\t{:else}')[0];

describe('streaming composer actions', () => {
	it('routes existing remote-only attachments to session staging for picker and drop', () => {
		expect(source).toContain('`/sessions/${encodeURIComponent($activeSession.sessionId)}/upload`');
		expect(source).toContain('attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, composerMode, $activeSession.sessionId)');
		const gates = source.match(/composerMode,\n\s*\$activeSession.sessionId\n\s*\);/g);
		expect(gates).toHaveLength(2);
		const dock = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ControlsDock.svelte'), 'utf8');
		expect(dock).not.toContain('isRemoteOnlyCompose');
		expect(dock).toContain('$activeSession.mode, $activeSession.sessionId');
		const sender = readFileSync(resolve(process.cwd(), 'src/lib/stores/session/message-sender.ts'), 'utf8');
		expect(sender).toContain('upload_paths: (!remoteOnly || !!_state.sessionId)');
	});
	it('counts serialized file references and legacy uploads as sendable content', () => {
		expect(source).toContain('const hasMessageContent = $derived(hasComposerDraft(inputValue) || uploadedFiles.length > 0)');
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
