import { beforeEach, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';

const mocks = vi.hoisted(() => ({
	push: vi.fn(), take: vi.fn(), banner: vi.fn(), dismiss: vi.fn(), flush: vi.fn(),
	cache: new Map<string, string>(), read: vi.fn(), restore: vi.fn(), add: vi.fn(),
}));
vi.mock('$lib/stores/active-session', () => ({ activeSession: Object.assign(writable({ sessionId: 'open', draft: '' }), { setPendingRestorePrompt: mocks.restore }) }));
vi.mock('$lib/stores/session-cache', () => ({ sessionEphemeralCache: { peekDraft: (id: string) => mocks.cache.get(id) } }));
vi.mock('./draft-persistence', () => ({ flushPendingDraft: mocks.flush, readDraft: mocks.read }));
vi.mock('./restored-draft', () => ({ addRestoredWordsToDraft: mocks.add }));
vi.mock('$lib/stores/notifications', () => ({ notify: { banner: mocks.banner, dismissLive: mocks.dismiss } }));
vi.mock('./prompt-stash', async (original) => ({ ...await original<typeof import('./prompt-stash')>(), promptStash: { push: mocks.push, take: mocks.take } }));
import { activeSession } from '$lib/stores/active-session';
import { chatDraftNow, composerBaseline, composerWordsOf, rewoundWordsKeptApart } from './rewound-words';

beforeEach(() => {
	vi.clearAllMocks(); mocks.cache.clear(); mocks.read.mockReturnValue('');
	activeSession.set({ sessionId: 'open', draft: '' } as never);
	mocks.push.mockReturnValue({ durable: true, entry: { id: 'entry' } });
	mocks.take.mockReturnValue(true);
});

it('uses the open draft, otherwise memory before storage, after flushing', () => {
	activeSession.set({ sessionId: 'open', draft: 'live' } as never);
	mocks.cache.set('other', 'cached'); mocks.read.mockReturnValue('stored');
	expect(chatDraftNow('open')).toBe('live'); expect(chatDraftNow('other')).toBe('cached');
	mocks.cache.clear(); expect(chatDraftNow('other')).toBe('stored');
	expect(mocks.flush).toHaveBeenCalledTimes(3);
	expect(composerBaseline('other')).toBeNull(); expect(composerBaseline('open')).toBe('live');
});

it.each([[null, 'later', 'old'], [undefined, 'later', 'old'], ['', '', 'old'], ['same', 'same', 'old'], ['', 'later', '']])(
	'does not stash when baseline %s, draft %s, prompt %s do not require it', (baseline, draft, prompt) => {
		expect(rewoundWordsKeptApart('open', baseline, prompt, [], draft)).toBe(false);
		expect(mocks.push).not.toHaveBeenCalled();
	},
);

it('keeps an off-screen restored message apart without changing the new draft', () => {
	mocks.cache.set('other', 'new words');
	expect(rewoundWordsKeptApart('other', 'before', 'old words', ['/uploads/a.png'])).toBe(true);
	expect(mocks.cache.get('other')).toBe('new words');
	expect(mocks.push.mock.calls[0][0]).toContain('/uploads/a.png');
	const action = mocks.banner.mock.calls[0][1].action;
	action.onClick(); expect(mocks.add).toHaveBeenCalledWith('other', 'old words', ['/uploads/a.png']);
});

it('offers the same explicit add action in the open chat, once', () => {
	expect(rewoundWordsKeptApart('open', '', 'old', [], 'new')).toBe(true);
	const action = mocks.banner.mock.calls[0][1].action;
	action.onClick(); expect(mocks.restore).toHaveBeenCalledWith('old', []);
	mocks.take.mockReturnValue(false); action.onClick(); expect(mocks.restore).toHaveBeenCalledTimes(1);
});

it('warns and lets the caller recover the words if the stash is not durable', () => {
	mocks.push.mockReturnValue({ durable: false });
	expect(rewoundWordsKeptApart('open', '', 'old', [], 'new')).toBe(false);
	expect(mocks.banner.mock.calls[0][1].severity).toBe('warning');
});

it('handles attachment-only restored messages and represents draft chips as @names', () => {
	const draft = JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'fileChip', attrs: { filename: 'a.png', path: '/uploads/a.png', fullPath: '/uploads/a.png' } }] }] });
	expect(composerWordsOf(draft)).toBe('@a.png');
	expect(rewoundWordsKeptApart('open', '', '', ['/uploads/a.png'], 'new')).toBe(true);
});
