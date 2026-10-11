/**
 * Assistant mode is one flag, decided where the home is identified (MainPanel)
 * and passed down. Ordinary chats take none of these branches.
 *
 * MainPanel, ChatView, ChatInput and MessageArea pull in the whole session
 * graph when mounted (see chatview-mode-lanes.test.ts), so the gates are pinned
 * at the source level; ControlsDock and the strip are mounted in their own
 * tests, the shared transcript in assistant-transcript-parity.test.ts, and the
 * integrated view is checked in a browser.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { isAssistantHome } from '$lib/stores/assistant';
import type { AssistantState } from '$lib/api/assistant';

// HTML comments only: a block-comment pattern would also eat `accept="image/*"`.
const read = (rel: string) => readFileSync(resolve(process.cwd(), 'src/lib', rel), 'utf8')
	.replace(/<!--[\s\S]*?-->/g, '');

const mainPanel = read('components/layout/MainPanel.svelte');
const chatView = read('components/layout/ChatView.svelte');
const chatInput = read('components/chat/ChatInput.svelte');
const messageArea = read('components/chat/MessageArea.svelte');

describe('isAssistantHome', () => {
	const state = { home_session_id: 'home' } as AssistantState;
	it('matches only the home session', () => {
		expect(isAssistantHome('home', state)).toBe(true);
		expect(isAssistantHome('other', state)).toBe(false);
		expect(isAssistantHome(null, state)).toBe(false);
		expect(isAssistantHome('home', null)).toBe(false);
		expect(isAssistantHome('', { home_session_id: '' } as unknown as AssistantState)).toBe(false);
	});
});

describe('assistant mode flag', () => {
	it('is decided once in MainPanel and passed down as a prop', () => {
		expect(mainPanel).toContain('const assistantMode = $derived(isAssistantHome(activeTab?.sessionId, $assistantState));');
		expect(mainPanel).toMatch(/<ChatView\s+\{assistantMode\}/);
		expect(chatView).toContain('<MessageArea {assistantMode} />');
		expect(chatView).toMatch(/<ChatInput[^>]*\{assistantMode\}/);
		expect(chatInput).toMatch(/<ControlsDock[\s\S]*?\{assistantMode\}/);
		for (const source of [chatView, chatInput, messageArea]) expect(source).not.toContain('home_session_id');
	});

	it('loads the assistant state for an open chat even before any sidebar mounts', () => {
		// The phone shell mounts its sidebar drawer on first open only.
		expect(mainPanel).toContain('if ($isAdmin && activeTab?.sessionId && !$assistantState) void refreshAssistant().catch(() => {});');
	});

	it('hides the header download and Git in the home only', () => {
		expect(mainPanel).toContain("{#if !assistantMode && $activeView?.type === 'session' && activeTab?.sessionId && activeTab.messages.length > 0}");
		expect(mainPanel).toContain('{#if activeTab && !assistantMode && shouldShowHeaderGitActions({');
		// Swarms moved to the running-tasks list (#1816): no header swarm control on any chat.
		expect(mainPanel).not.toContain('SwarmDropdown');
	});

	it('drops the context chips and the browser toggle from the composer in the home', () => {
		expect(chatInput).toMatch(/\{#if !assistantMode\}\s*<div class="context-pill-row"><ContextPillBar \/><\/div>\s*\{\/if\}/);
		expect(chatInput).toContain('onBrowserToggle={assistantMode ? undefined : onBrowserToggle}');
	});

	it('replaces the running-tasks tree with the background work strip', () => {
		expect(messageArea).toMatch(/\{#if !assistantMode\}\s*<RunningTasksList /);
		expect(chatView).toMatch(/\{#if assistantMode\}\s*<BackgroundWorkStrip /);
	});

	it('organizes the home transcript exactly like any chat (spec §10.5)', () => {
		// One presentation model and one fold component: no second fold path.
		expect(messageArea).toContain('const displayItems: DisplayItem[] = $derived(transcript.items);');
		expect(messageArea).toContain('const itemIndexByTurnId: ReadonlyMap<string, number> = $derived(transcript.itemIndexByTurnId);');
		expect(messageArea).not.toMatch(/foldAssistantTranscript|AssistantDetails|item\.kind === 'details'/);
		// The model is built from the same pref and lifecycle in both modes.
		const build = messageArea.slice(messageArea.indexOf('const transcript: TranscriptModel'), messageArea.indexOf('const displayItems'));
		expect(build).toContain('const groupingEnabled = chatPrefs?.groupToolCalls !== false;');
		expect(build).not.toContain('assistantMode');
		const loop = messageArea.slice(messageArea.indexOf('{#each visibleItems as item'), messageArea.indexOf('{/each}', messageArea.indexOf('{#each visibleItems as item')));
		expect(loop).toContain('<WorkLogGroup');
		expect(loop).not.toContain('assistantMode');
	});

	it('asks marked approvals in words and keeps the dialog for every other request', () => {
		expect(chatView).toContain('const dialogPermissions = $derived(($activeSession.pendingPermissions ?? []).filter((request) => !isAssistantConfirm(request)));');
		expect(chatView).toContain('const showPermissionModal = $derived(!voiceCallActive && dialogPermissions.length > 0);');
		expect(chatView).toContain('<PermissionQueue requests={dialogPermissions}');
		expect(messageArea).toContain('const assistantQuestion = $derived(currentAssistantQuestion($activeSession.pendingPermissions ?? []));');
		// No approve or deny buttons ride along with the question.
		const start = messageArea.indexOf('class="assistant-ask"');
		const ask = messageArea.slice(start, messageArea.indexOf('{/if}', start));
		expect(ask).toContain('content={assistantQuestion.text}');
		expect(ask).not.toMatch(/<button|onRespond|sendPermissionResponse/);
	});
});
