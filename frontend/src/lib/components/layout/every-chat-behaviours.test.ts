/**
 * The behaviours that only the retired assistant home had now run in every
 * chat (#371 step 5): the background-work strip beside the composer, approvals
 * asked in words, and a call at any time (VoiceCallButton.test.ts mounts that
 * gate). No `assistantMode` flag remains to switch any of them off.
 *
 * MainPanel, ChatView, ChatInput and MessageArea pull in the whole session
 * graph when mounted (see chatview-mode-lanes.test.ts), so these are pinned at
 * the source level; ControlsDock and the strip are mounted in their own tests,
 * and the integrated view is checked in a browser.
 */
import { describe, expect, it } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// HTML comments only: a block-comment pattern would also eat `accept="image/*"`.
const read = (rel: string) => readFileSync(resolve(process.cwd(), rel), 'utf8')
	.replace(/<!--[\s\S]*?-->/g, '');
const lib = (rel: string) => read(`src/lib/${rel}`);

const mainPanel = lib('components/layout/MainPanel.svelte');
const chatView = lib('components/layout/ChatView.svelte');
const chatInput = lib('components/chat/ChatInput.svelte');
const messageArea = lib('components/chat/MessageArea.svelte');
const approvalCard = lib('components/chat/ApprovalRequestCard.svelte');

describe('no assistant mode anywhere', () => {
	it('leaves no flag in the chat surface, composer or phone controls', () => {
		for (const rel of [
			'src/lib/components/layout/MainPanel.svelte',
			'src/lib/components/layout/ChatView.svelte',
			'src/lib/components/chat/ChatInput.svelte',
			'src/lib/components/chat/ControlsDock.svelte',
			'src/lib/components/chat/MessageArea.svelte',
			'src/lib/components/chat/composer-shell.ts',
			'src/lib/components/layout/NoProjectPill.svelte',
			'../mobile/src/shell/MobileComposerControlsSheet.svelte'
		]) {
			expect(read(rel), rel).not.toMatch(/assistantMode|isAssistantHome|home_session_id/);
		}
	});

	it('has no Assistant page, sidebar row or assistant store', () => {
		expect(existsSync(resolve(process.cwd(), 'src/lib/components/assistant'))).toBe(false);
		expect(existsSync(resolve(process.cwd(), 'src/lib/stores/assistant.ts'))).toBe(false);
		expect(mainPanel).not.toContain('AssistantHome');
		expect(lib('components/layout/Sidebar.svelte')).not.toMatch(/Open your assistant|openAssistant/);
		expect(lib('components/layout/AppPage.svelte')).not.toContain("openFeaturePage('assistant')");
		expect(lib('stores/layout.svelte.ts')).not.toMatch(/\| 'assistant'/);
	});

	it('keeps the header download and Git on every chat', () => {
		expect(mainPanel).toContain("{#if $activeView?.type === 'session' && activeTab?.sessionId && activeTab.messages.length > 0}");
		expect(mainPanel).toContain('{#if activeTab && shouldShowHeaderGitActions({');
	});

	it('keeps the context chips and the browser toggle in every composer', () => {
		// A new session shows the hero sentence in their place until its first message.
		expect(chatInput).toMatch(/\{#if !mobileViewport && !newSession\}\s*<div class="context-pill-row"><ContextPillBar \/><\/div>\s*\{\/if\}/);
		expect(chatInput).toMatch(/<ControlsDock[\s\S]*?\{onBrowserToggle\}/);
	});
});

describe('background work beside the composer, in every chat', () => {
	it('renders the strip unconditionally from the chats this one started', () => {
		expect(chatView).toMatch(/<BackgroundWorkStrip jobs=\{backgroundJobs\} onOpen=\{openJob\} onStop=\{stopJob\} \/>/);
		expect(chatView).not.toMatch(/\{#if [^}]*\}\s*<BackgroundWorkStrip/);
		// Its own tasks stay in the transcript's running-tasks list, never twice.
		const build = chatView.slice(chatView.indexOf('const backgroundJobs'), chatView.indexOf('function openJob'));
		expect(build).toContain('tasks: []');
		expect(messageArea).toMatch(/<RunningTasksList sessionId=\{\$activeSession\.sessionId\} variant="inline" \/>/);
		expect(messageArea).not.toMatch(/\{#if [^}]*\}\s*<RunningTasksList/);
	});
});

describe('approvals asked in words, in every chat', () => {
	it('keeps marked approvals out of the dialog and asks them in the transcript', () => {
		expect(chatView).toContain('const dialogPermissions = $derived(($activeSession.pendingPermissions ?? []).filter((request) => !isAssistantConfirm(request)));');
		expect(chatView).toContain('<PermissionQueue requests={dialogPermissions}');
		expect(messageArea).toContain('const assistantQuestion = $derived(currentAssistantQuestion($activeSession.pendingPermissions ?? []));');
		// The question stands in for the working dots in any chat.
		expect(messageArea).toMatch(/const showGapIndicator = \$derived\(\s*!assistantQuestion\s*&&/);
		// #483: the request is a card in the transcript, keyed by request id,
		// never a second copy floating after it.
		expect(messageArea).not.toContain('class="assistant-ask"');
		expect(messageArea).toContain('<ApprovalRequestCard data={approval} active={approval.request_id === activeApprovalId} />');
		expect(approvalCard).not.toMatch(/<button|onRespond|sendPermissionResponse/);
	});

	it('organizes every transcript with one presentation model', () => {
		expect(messageArea).toContain('const displayItems: DisplayItem[] = $derived(transcript.items);');
		const loop = messageArea.slice(messageArea.indexOf('{#each visibleItems as item'), messageArea.indexOf('{/each}', messageArea.indexOf('{#each visibleItems as item')));
		expect(loop).toContain('<WorkLogGroup');
	});
});
