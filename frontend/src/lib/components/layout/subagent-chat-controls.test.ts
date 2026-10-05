/**
 * A subagent's own chat (a Task or Swarm child) is for reading the transcript
 * and messaging the subagent: no Git rail and no private access control. Its
 * private access requests show in its main chat instead (ChatAccess.test.ts).
 *
 * MainPanel pulls in the whole session/store graph, so, as in
 * chatview-mode-lanes.test.ts, these pin its source; the gates themselves are
 * rendered in git-header-visibility.test.ts, ContextPillBar.test.ts and
 * ChatAccess.test.ts.
 */
import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { subagentChats } from '$lib/stores/subagent-chats';

const read = (rel: string) => readFileSync(resolve(process.cwd(), 'src/lib', rel), 'utf8');
const stripComments = (source: string) =>
	source.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const mainPanel = stripComments(read('components/layout/MainPanel.svelte'));

describe('subagent chat controls in MainPanel', () => {
	it('decides subagent-ness from the shared marker, not a branch-name heuristic', () => {
		expect(mainPanel).toMatch(/const activeIsSubagent = \$derived\(!!activeSessionId && \$subagentChats\.has\(activeSessionId\)\)/);
		expect(mainPanel).not.toMatch(/startsWith\(['"]task\//);
	});

	it('mounts the private access control only outside a subagent chat', () => {
		const mount = mainPanel.match(/\{#if ([^}]*)\}\s*\{#key activeTab\.sessionId\}\s*<ChatAccess /);
		expect(mount?.[1]).toContain('!activeIsSubagent');
		expect(mainPanel.match(/<ChatAccess /g)).toHaveLength(1);
	});

	it('hides the Git rail and stops loading Git for a subagent chat', () => {
		const gates = [...mainPanel.matchAll(/shouldShowHeaderGitActions\(\{([\s\S]*?)\}\)/g)].map((m) => m[1]);
		expect(gates.length).toBeGreaterThanOrEqual(1);
		for (const gate of gates) expect(gate).toContain('isSubagent: activeIsSubagent');
		expect(mainPanel).toMatch(/headerGitInput = \$derived\(\{[\s\S]*?isSubagent: activeIsSubagent/);
		expect(mainPanel).toMatch(/if \(project && !subagent\) \{\s*git\.setWorkerTarget/);
		expect(mainPanel.match(/<GitHeaderActions\b/g)).toHaveLength(1);
	});

	it('tells a shell header (the phone app) that the chat is a subagent’s, so it draws no empty action row', () => {
		expect(mainPanel).toMatch(/headerActions,\s*subagentChat: activeIsSubagent\s*\}\)/);
	});

	it('closes a Changes panel left open in a subagent chat', () => {
		expect(mainPanel).toMatch(/if \(activeIsSubagent && activeSidePanel\.active === 'changes'\)/);
	});
});

describe('what marks a chat as a subagent’s', () => {
	it('is fed by /live metadata, the opened row and the Task and Swarm transcript links', () => {
		expect(read('stores/session/session-switcher.ts')).toMatch(/if \(meta\.is_worker !== undefined\) subagentChats\.mark\(sessionId, meta\.is_worker\)/);
		const navigation = read('services/session-navigation.ts');
		expect(navigation).toMatch(/subagentChats\.mark\(sessionId, session\.is_worker\)/);
		expect(navigation).toMatch(/export function openSubagentLink[\s\S]*?subagentChats\.mark\(sessionId, true\)/);
		expect(read('components/SubagentMessage.svelte')).toMatch(/openSubagentLink\(event, message\.agentSessionId!\)/);
		expect(read('components/chat/SwarmCard.svelte')).toMatch(/openSubagentLink\(event, task\.worker_session_id!\)/);
	});

	it('records and clears the marker per session', () => {
		subagentChats.mark('child', true);
		const first = get(subagentChats);
		expect(first.has('child')).toBe(true);
		subagentChats.mark('child', true);
		expect(get(subagentChats)).toBe(first); // no-op keeps the same set
		subagentChats.mark('child', false);
		expect(get(subagentChats).has('child')).toBe(false);
	});
});
