import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
const source = (path: string) => readFileSync(`src/lib/${path}`, 'utf8');

describe('voice composer parity', () => {
	it('keeps one composer mounted before and during calls', () => {
		const view = source('components/layout/ChatView.svelte');
		expect(view.match(/<ChatInput /g)).toHaveLength(1);
		expect(view).toContain('const showComposer = true;');
		expect(view).toMatch(/<VoiceCallPanel[^>]+compact \/>\s*\{\/if\}\s*\{#key \$sessionOwnerBoundary\}\s*<ChatInput/);
	});
	it('uses code context and permission controls for voice on desktop and mobile', () => {
		expect(source('components/chat/ChatInput.svelte')).toContain("{#if composerMode !== 'chat'}");
		const dock = source('components/chat/ControlsDock.svelte');
		expect(dock).toContain("$activeSession.mode !== 'chat'");
		expect(dock).toContain('showCodeControls={isCodeLane}');
		expect(dock).toContain('const showAttach = true;');
	});
	it('restores failed sends only to the originating draft', () => {
		const input = source('components/chat/ChatInput.svelte');
		expect(input).toContain('$activeSession.sessionId !== sendingSessionId');
		expect(input).toContain('writeDraft(sendingSessionId, priorDraftJSON)');
		expect(input).toContain('editorRef?.setContent(priorDraftJSON)');
	});
});
