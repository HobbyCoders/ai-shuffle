import { beforeEach, describe, expect, it, vi } from 'vitest';

const cached = vi.hoisted(() => new Map<string, string>());
vi.mock('$lib/stores/session-cache', () => ({
	sessionEphemeralCache: {
		peekDraft: (id: string) => cached.get(id),
		patchDraft: (id: string, draft: string) => { if (cached.has(id)) cached.set(id, draft); },
	},
}));

import { addRestoredWordsToDraft, noteRestoreFinished, noteRestoreStarted, takeUnfinishedRestores } from './restored-draft';
import { readDraft, writeDraft } from './draft-persistence';

const doc = (text: string) => JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text }] }] });
const words = (draft: string | null) => JSON.stringify(JSON.parse(draft ?? '{}'));

describe('addRestoredWordsToDraft', () => {
	beforeEach(() => { cached.clear(); sessionStorage.clear(); });

	it('fills an empty draft so reopening the chat finds the words', () => {
		addRestoredWordsToDraft('s1', 'kept words');
		expect(readDraft('s1')).toContain('kept words');
	});

	it('goes in front of a draft held in memory and updates both copies', () => {
		cached.set('s1', doc('typed later'));
		addRestoredWordsToDraft('s1', 'kept words');
		const draft = cached.get('s1')!;
		expect(draft.indexOf('kept words')).toBeLessThan(draft.indexOf('typed later'));
		expect(draft.indexOf('kept words')).toBeGreaterThan(-1);
		expect(words(readDraft('s1'))).toBe(words(draft));
	});

	it('merges with the reload copy when the chat is not in memory', () => {
		writeDraft('s1', doc('typed before a reload'));
		addRestoredWordsToDraft('s1', 'kept words');
		const draft = readDraft('s1')!;
		expect(draft).toContain('kept words');
		expect(draft).toContain('typed before a reload');
	});

	it('replaces a draft that holds only empty paragraphs', () => {
		cached.set('s1', JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] }));
		addRestoredWordsToDraft('s1', 'kept words');
		expect(JSON.parse(cached.get('s1')!).content).toHaveLength(1);
	});

	it('keeps an attachment even when the message had no words', () => {
		addRestoredWordsToDraft('s1', '', ['/uploads/proof.png']);
		expect(readDraft('s1')).toContain('/uploads/proof.png');
	});

	it('leaves the draft alone when there is nothing to restore', () => {
		addRestoredWordsToDraft('s1', '');
		expect(readDraft('s1')).toBeNull();
	});
});

describe('restores a reload cut off', () => {
	const LATER_PAGE = 'a-later-page';
	beforeEach(() => sessionStorage.clear());

	it('hands back the words once, when an earlier page noted them and the row has gone', () => {
		noteRestoreStarted('s1', 'row-1', 'kept words', ['/uploads/proof.png']);
		expect(takeUnfinishedRestores('s1', new Set(), LATER_PAGE)).toMatchObject([{ rowId: 'row-1', prompt: 'kept words', uploadPaths: ['/uploads/proof.png'] }]);
		expect(takeUnfinishedRestores('s1', new Set(), LATER_PAGE)).toEqual([]);
	});

	it('keeps the note while the row is still on screen, in case the request lands late', () => {
		noteRestoreStarted('s1', 'row-1', 'kept words');
		expect(takeUnfinishedRestores('s1', new Set(['row-1']), LATER_PAGE)).toEqual([]);
		expect(takeUnfinishedRestores('s1', new Set(), LATER_PAGE)).toHaveLength(1);
	});

	it('leaves this page\'s own restores alone: their answers are still coming', () => {
		noteRestoreStarted('s1', 'row-1', 'kept words');
		expect(takeUnfinishedRestores('s1', new Set())).toEqual([]);
		expect(takeUnfinishedRestores('s1', new Set(), LATER_PAGE)).toHaveLength(1);
	});

	it('forgets a finished restore, and never reads another chat\'s note', () => {
		noteRestoreStarted('s1', 'row-1', 'kept words');
		noteRestoreStarted('s2', 'row-2', 'other words');
		noteRestoreFinished('s1', 'row-1');
		expect(takeUnfinishedRestores('s1', new Set(), LATER_PAGE)).toEqual([]);
		expect(takeUnfinishedRestores('s2', new Set(), LATER_PAGE)).toMatchObject([{ rowId: 'row-2' }]);
	});
});
