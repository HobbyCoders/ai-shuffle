import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// Pin the actual composer markup without mocking its session/store graph.
const source = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ChatInput.svelte'), 'utf8');
// The circles right of the text: the send slot (Send, Talk or the spinner), then Stop.
const circles = source.split('<div class="send-cluster"')[1].split('\n\t\t\t\t\t</div>')[0];

describe('streaming composer actions', () => {
	it('routes existing remote-only attachments to session staging for picker and drop', () => {
		expect(source).toContain('`/sessions/${encodeURIComponent($activeSession.sessionId)}/upload`');
		expect(source).toContain('attachmentBlockReason($activeSession.project, $activeSession.projectV2Id, projectLane, $activeSession.sessionId)');
		const gates = source.match(/projectLane,\r?\n\s*\$activeSession.sessionId\r?\n\s*\);/g); // \r? keeps CRLF checkouts matching
		expect(gates).toHaveLength(2);
		// The attach circle is ChatInput's; the controls row carries no attach gate.
		const dock = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/ControlsDock.svelte'), 'utf8');
		expect(dock).not.toContain('isRemoteOnlyCompose');
		expect(dock).not.toContain('attachmentBlockReason');
		expect(source).toContain('projectRulesLane(composerMode, projectlessSubagent)');
		const sender = readFileSync(resolve(process.cwd(), 'src/lib/stores/session/message-sender.ts'), 'utf8');
		expect(sender).toContain('upload_paths: (!remoteOnly || !!_state.sessionId)');
	});
	it('lets the owner message a subagent that has no project', () => {
		// A worker started from the owner's no-project chat is stored with no
		// mode (read as code) and no project. Its chat must follow the chat
		// lane's project rules, or every send says "Select a project first."
		expect(source).toMatch(/const projectlessSubagent = \$derived\(\s*!!\$activeSession\.sessionId && \$subagentChats\.has\(\$activeSession\.sessionId\)\s*&& !\$activeSession\.project && !\$activeSession\.projectV2Id\s*\);/);
		// The send guard and the attachment gates all read that one lane.
		expect(source).toContain('mode: projectLane');
		expect(source).not.toMatch(/projectRulesLane\(composerMode\)/);
	});

	it('counts serialized file references and legacy uploads as sendable content', () => {
		expect(source).toContain('const hasMessageContent = $derived(hasComposerDraft(inputValue) || uploadedFiles.length > 0)');
		expect(source).toContain('hasPrompt: !!finalPrompt.trim() || uploadedFiles.length > 0');
		expect(source).not.toContain('inputValue = editorRef?.getText()');
	});

	it('uses the same content gate while idle and retains upload blocking during streaming', () => {
		// One gate (submit-guards.sendButtonState) drives the one Send circle,
		// idle or streaming, so the disabled state and its reason never disagree.
		expect(source).toMatch(/sendButtonState\(\{\s*hasContent: hasMessageContent,\s*hasCredential: \$anyCredential,\s*isUploading,\s*isRecording,\s*isTranscribing/);
		expect(source.match(/disabled=\{sendState\.disabled\}/g)).toHaveLength(1);
		expect(circles).toContain('disabled={sendState.disabled}');
	});

	it('keeps Stop outside the draft gate so typing never removes the control', () => {
		// Send (or Talk) holds the slot; Stop is its own circle after it, shown
		// whenever work runs (composer-wrap.composerCircles keeps it with a draft).
		expect(circles).toMatch(/\{:else if circles\.includes\('send'\)\}\s*<button[\s\S]*?type="submit"[\s\S]*?aria-label="Send"[\s\S]*?<\/button>\s*\{\/if\}[\s\S]*?\{#if circles\.includes\('stop'\)\}\s*<StopButton[\s\S]*?\/>/);
		expect(circles.match(/<StopButton/g)).toHaveLength(1);
	});

	it('preserves task-aware stopping for an empty or whitespace-only draft', () => {
		expect(circles).toContain('activeTasks={$activeSession.activeTasks}');
		expect(circles).toContain('activeSession.stopGeneration()');
	});
});
