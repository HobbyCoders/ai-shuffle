import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Pin the actual composer markup without mocking its session/store graph.
const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'), 'utf8');
const streaming = source.split('{:else if stoppableWork}')[1].split('\n\t\t\t\t\t\t{:else}')[0];

describe('streaming composer actions', () => {
	it('routes existing remote-only attachments to session staging for picker and drop', () => {
		expect(source).toContain('`/sessions/${encodeURIComponent($activeSession.sessionId)}/upload`');
		expect(source).toContain('attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, projectLane, $activeSession.sessionId)');
		const gates = source.match(/projectLane,\r?\n\s*\$activeSession.sessionId\r?\n\s*\);/g); // \r? keeps CRLF checkouts matching
		expect(gates).toHaveLength(2);
		const dock = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ControlsDock.svelte'), 'utf8');
		expect(dock).not.toContain('isRemoteOnlyCompose');
		expect(dock).toContain('projectLane, $activeSession.sessionId');
		// The assistant's home is projectless: both components take their
		// project rules from the same lane, not from the stored session mode.
		expect(source).toContain('projectRulesLane(composerMode, assistantMode)');
		expect(dock).toContain('projectRulesLane($activeSession.mode, assistantMode)');
		const sender = readFileSync(resolve(process.cwd(), 'src/lib/stores/session/message-sender.ts'), 'utf8');
		expect(sender).toContain('upload_paths: (!remoteOnly || !!_state.sessionId)');
	});
	it('counts serialized file references and legacy uploads as sendable content', () => {
		expect(source).toContain('const hasMessageContent = $derived(hasComposerDraft(inputValue) || uploadedFiles.length > 0)');
		expect(source).toContain('hasPrompt: !!finalPrompt.trim() || uploadedFiles.length > 0');
		expect(source).not.toContain('inputValue = editorRef?.getText()');
	});

	it('uses the same content gate while idle and retains upload blocking during streaming', () => {
		// One gate (submit-guards.sendButtonState) drives both send buttons, so
		// the disabled state and its reason can never disagree.
		expect(source).toMatch(/sendButtonState\(\{\s*hasContent: hasMessageContent,\s*hasCredential: \$anyCredential,\s*isUploading,\s*isRecording,\s*isTranscribing/);
		expect(source.match(/disabled=\{sendState\.disabled\}/g)).toHaveLength(2);
		expect(streaming).toContain('disabled={sendState.disabled}');
	});

	it('keeps Stop outside the draft gate so typing never removes the control', () => {
		expect(streaming).toMatch(/\{#if hasMessageContent\}\s*<button[^>]*type="submit"[^>]*aria-label="Send"[\s\S]*?<\/button>\s*\{\/if\}\s*<StopButton[\s\S]*?\/>/);
		expect(streaming.match(/<StopButton/g)).toHaveLength(1);
	});

	it('preserves task-aware stopping for an empty or whitespace-only draft', () => {
		expect(streaming).toContain('activeTasks={$activeSession.activeTasks}');
		expect(streaming).toContain('activeSession.stopGeneration()');
	});
});
