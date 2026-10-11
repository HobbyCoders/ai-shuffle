/**
 * Assistant-mode parity (transcript-organization spec §10.5). The assistant's
 * home renders its transcript through MessageArea's one item loop: the same
 * presentation model, built with the same `groupToolCalls` pref and lifecycle,
 * and the same WorkLogGroup folds. assistant-mode.test.ts pins that wiring in
 * MessageArea's source (mounting it pulls in the whole session graph); this
 * renders that loop (test-fixtures/TranscriptItems.svelte mirrors it) over the
 * spec's long turn and checks what an assistant-home reader sees, live and
 * settled, against an ordinary chat.
 */
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import TranscriptItems from './test-fixtures/TranscriptItems.svelte';
import { longTurn } from './test-fixtures/long-turn';
import { buildTranscriptModel } from '$lib/utils/work-log';
import { createFoldState } from '$lib/stores/session/fold-state';
import { needsYouCount, openSessionNeeds } from '$lib/components/layout/session-card-data';
import type { ChatMessage } from '$lib/stores/types';

const messageArea = readFileSync(resolve(process.cwd(), 'src/lib/components/chat/MessageArea.svelte'), 'utf8');

function memoryStorage(): Storage {
	const data = new Map<string, string>();
	return {
		get length() { return data.size; }, clear: () => data.clear(),
		getItem: (k) => data.get(k) ?? null, key: (i) => [...data.keys()][i] ?? null,
		removeItem: (k) => { data.delete(k); }, setItem: (k, v) => { data.set(k, String(v)); }
	};
}

/** What MessageArea hands the builder; `assistantMode` is not an input. */
function model(messages: ChatMessage[], live: boolean, groupingEnabled = true) {
	return buildTranscriptModel(messages, {
		isStreaming: false,
		latestTurnUnsettled: live,
		runningToolIds: new Set(live ? ['monitor-1'] : []),
		sessionId: 'assistant-home',
		swarms: [],
		groupingEnabled
	});
}

/** Row ids on screen, in order: mounted and not inside a hidden sequence row. */
function visibleRows(container: HTMLElement): string[] {
	return [...container.querySelectorAll<HTMLElement>('[data-testid="work-row"]')]
		.filter((row) => !row.closest('[hidden]'))
		.map((row) => row.dataset.id!);
}

afterEach(cleanup);

describe('assistant home transcript parity', () => {
	it('uses the ordinary transcript model and fold component', () => {
		expect(messageArea).toContain('const displayItems: DisplayItem[] = $derived(transcript.items);');
		expect(messageArea).not.toMatch(/foldAssistantTranscript|AssistantDetails/);
	});

	it('shows the live long turn as one group of runs, steers in place, failures folded', () => {
		const t = longTurn();
		const view = render(TranscriptItems, { items: model(t.messages, true).items, folds: createFoldState(memoryStorage()) });
		const groups = view.container.querySelectorAll('section.worklog');
		expect(groups).toHaveLength(1);
		expect((groups[0] as HTMLElement).dataset.workLogMode).toBe('live');
		expect(visibleRows(view.container)).toEqual([
			t.prompt, t.finding, t.interim, t.worker, t.question, t.steer, t.ack,
			t.worker2, t.steer2, t.final
		].map((m) => m.id));
		// Each earlier run behind its own live label; the failed Edit folds with
		// its run and is counted there, and the final prose ends the last run.
		expect(screen.getAllByRole('button', { name: /^Show work details: / }).map((b) => b.getAttribute('aria-label'))).toEqual([
			'Show work details: Read 1 file', 'Show work details: Searched code 1 time',
			'Show work details: Ran 1 command', 'Show work details: Ran 1 command and changed 2 files, 1 failed',
			'Show work details: Searched code 1 time and got 1 agent message'
		]);
	});

	it('settles into the same one fold, keeps every protected row, and the header collapses it', async () => {
		const t = longTurn();
		const folds = createFoldState(memoryStorage());
		const view = render(TranscriptItems, { items: model(t.messages, true).items, folds });
		const finding = view.container.querySelector(`[data-id="${t.finding.id}"]`);
		await view.rerender({ items: model(t.messages, false).items, folds });
		// One component tree: the group flips mode, its rows keep their nodes.
		expect(view.container.querySelectorAll('section.worklog')).toHaveLength(1);
		expect((view.container.querySelector('section.worklog') as HTMLElement).dataset.workLogMode).toBe('settled');
		expect(view.container.querySelector(`[data-id="${t.finding.id}"]`)).toBe(finding);
		const settled = [t.prompt, t.finding, t.interim, t.worker, t.question, t.steer, t.ack, t.worker2, t.steer2, t.final].map((m) => m.id);
		expect(visibleRows(view.container)).toEqual(settled);

		// Open: every row, under the sticky header; the fold is stored by turn.
		await fireEvent.click(screen.getByRole('button', { expanded: false }));
		expect(visibleRows(view.container)).toEqual(t.messages.map((m) => m.id));
		expect(folds.isOpen(`${t.prompt.id}:turn`)).toBe(true);
		const header = view.container.querySelector<HTMLButtonElement>('[data-fold-header]')!;
		expect(header.getAttribute('aria-label')).toMatch(/^Hide work details: Worked for 30m/);
		await fireEvent.click(header);
		expect(visibleRows(view.container)).toEqual(settled);
		expect(folds.isOpen(`${t.prompt.id}:turn`)).toBe(false);
	});

	it('shows every row flat with grouping off, as an ordinary chat does', () => {
		const t = longTurn();
		const view = render(TranscriptItems, { items: model(t.messages, false, false).items, folds: createFoldState(memoryStorage()) });
		expect(view.container.querySelector('section.worklog')).toBeNull();
		// Agent deliveries render through the row dispatch like any message.
		expect(visibleRows(view.container)).toEqual(t.messages.map((m) => m.id));
	});

	it('counts the open question for Needs you from the same messages', () => {
		const t = longTurn();
		const asking = t.messages.slice(0, t.messages.indexOf(t.question) + 1);
		const local = { status: 'active', is_remote: false, remote_runtime_state: null } as const;
		expect(needsYouCount(local, null, 0, openSessionNeeds({ messages: asking, pendingQuestions: [{ request_id: 'ask-1' }] }, true))).toBe(1);
		expect(messageArea).toMatch(/openSessionNeeds\(\s*\$activeSession,\s*chatPrefs\?\.groupToolCalls !== false,/);
	});
});

describe('assistant home approval outcomes', () => {
	// Carried over from the retired assistant-transcript.test.ts: an approval
	// outcome the reader gave in words stays in line in a folded turn, and a
	// started chat's raw report folds with the work. The shared protected-row
	// rule decides both, as it did for the old assistant fold.
	it('keeps an approved assistant_confirm outcome in line and folds a started chat report', () => {
		const sys = (id: string, patch: Partial<ChatMessage>): ChatMessage => ({ id, role: 'system', type: 'system', content: '', ...patch });
		const messages: ChatMessage[] = [
			{ id: 'u1', role: 'user', type: 'text', content: 'push it', turnId: 't-u1' },
			{ id: 't1', role: 'assistant', type: 'tool_use', toolName: 'Bash', toolId: 'tool-t1', toolInput: { command: 'git push' }, toolStatus: 'complete', toolResult: 'ok', content: '' },
			sys('o', { systemSubtype: 'assistant_confirm', content: 'Authorized: Do you authorize me to run `git push`?', systemData: { decision: 'approved' } }),
			sys('r', { systemSubtype: 'task_notification', systemData: { kind: 'session', status: 'completed', result: 'raw output' } }),
			{ id: 'a1', role: 'assistant', type: 'text', content: 'Pushed. Checks are green.' }
		];
		const view = render(TranscriptItems, { items: model(messages, false).items, folds: createFoldState(memoryStorage()) });
		expect(view.container.querySelector('section.worklog')?.getAttribute('data-work-log-mode')).toBe('settled');
		expect(visibleRows(view.container)).toEqual(['u1', 'o', 'a1']);
	});
});
