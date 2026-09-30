import { describe, it, expect, vi } from 'vitest';
import { get } from 'svelte/store';
import { createComposerSuggestion, suggestionKey, hasComposerDraft, type SuggestionContext } from './composer-suggestion';
const idle: SuggestionContext = { sessionId: 's', mode: 'code', enabled: true, turnId: null, streaming: false, blocked: false };
const frame = { session_id: 's', turn_id: 't', suggestion: '<img src=x> **literal**' };
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

describe('session-retained composer suggestions', () => {
	it('recognizes empty saved documents but blocks text and chips', () => {
		expect(hasComposerDraft('')).toBe(false);
		expect(hasComposerDraft('{"type":"doc","content":[{"type":"paragraph"}]}')).toBe(false);
		expect(hasComposerDraft('draft')).toBe(true);
		expect(hasComposerDraft('{"type":"doc","content":[{"type":"fileChip"}]}')).toBe(true);
	});
	it('loads a completed background turn without observing its live turn', async () => {
		const load = vi.fn().mockResolvedValue(frame);
		const s = createComposerSuggestion(load);
		s.observe(idle); await flush();
		expect(get(s)).toBe(frame.suggestion);
		expect(load).toHaveBeenCalledOnce();
		s.observe({ ...idle }); await flush();
		expect(load).toHaveBeenCalledOnce();
	});
	it('restores per-session hints across switches and composer remounts', async () => {
		const s = createComposerSuggestion(async id => ({ ...frame, session_id: id, suggestion: id }));
		s.observe(idle); await flush(); expect(get(s)).toBe('s');
		s.observe({ ...idle, sessionId: 'other' }); expect(get(s)).toBe('');
		await flush(); expect(get(s)).toBe('other');
		s.detach(); s.observe(idle); await flush(); expect(get(s)).toBe('s');
	});
	it('fetches on live notification, idle completion and reconnect', async () => {
		const load = vi.fn().mockResolvedValue(null);
		const s = createComposerSuggestion(load);
		s.observe({ ...idle, streaming: true, turnId: 't' });
		s.receive(frame); await flush(); expect(load).not.toHaveBeenCalled();
		s.observe(idle); await flush(); expect(get(s)).toBe('');
		load.mockResolvedValue(frame); s.receive(frame); await flush();
		expect(get(s)).toBe(frame.suggestion);
		s.observe({ ...idle, connected: true }); await flush(); expect(load).toHaveBeenCalledTimes(3);
	});
	it('does not accept stale reads after switch, disable, draft, new turn or detach', async () => {
		for (const change of [ { ...idle, sessionId: 'other' }, { ...idle, enabled: false },
			{ ...idle, blocked: true }, { ...idle, streaming: true, turnId: 'new' }, null ]) {
			let resolve!: (hint: typeof frame) => void;
			const s = createComposerSuggestion(() => new Promise(r => { resolve = r; }));
			s.observe(idle); const first = resolve;
			if (change) s.observe(change); else s.detach();
			first(frame); await flush(); expect(get(s)).toBe(''); expect(s.accept()).toBe('');
		}
	});
	it.each(['accept', 'invalidate'] as const)('retains %s dismissal across switches but permits a newer turn', async action => {
		let hint = frame;
		const s = createComposerSuggestion(async () => hint);
		s.observe(idle); await flush(); s[action](); expect(get(s)).toBe('');
		s.detach(); s.observe(idle); await flush(); expect(get(s)).toBe('');
		hint = { ...frame, turn_id: 'new' }; s.receive(hint); await flush();
		expect(get(s)).toBe(frame.suggestion);
	});
	it('hides behind drafts and when disabled; restores without generation', async () => {
		const load = vi.fn().mockResolvedValue(frame);
		const s = createComposerSuggestion(load);
		s.observe({ ...idle, blocked: true }); await flush(); expect(load).not.toHaveBeenCalled();
		s.observe(idle); await flush(); expect(get(s)).toBe(frame.suggestion);
		s.observe({ ...idle, enabled: false }); expect(get(s)).toBe('');
		s.observe(idle); await flush(); expect(get(s)).toBe(frame.suggestion);
	});
	it('ignores other-session notifications and invalid snapshots, and handles read failure', async () => {
		const load = vi.fn().mockResolvedValue({ ...frame, session_id: 'other' });
		const s = createComposerSuggestion(load);
		s.observe(idle); await flush(); expect(get(s)).toBe('');
		s.receive({ session_id: 'other' }); expect(load).toHaveBeenCalledOnce();
		load.mockRejectedValue(new Error('offline')); await s.refresh(); expect(get(s)).toBe('');
		load.mockResolvedValue(frame); await s.refresh(); expect(get(s)).toBe(frame.suggestion);
	});
});

describe('suggestion keyboard decisions', () => {
	const key = (key: string, extra = {}) => ({
		key,
		shiftKey: false,
		ctrlKey: false,
		altKey: false,
		metaKey: false,
		isComposing: false,
		repeat: false,
		keyCode: 0,
		...extra
	});
	it.each(['Tab'])('%s accepts, not submits', k =>
		expect(suggestionKey(key(k))).toBe('accept')
	);
	it('Enter does not accept', () => expect(suggestionKey(key('Enter'))).toBe(null));
		it('Escape dismisses', () => expect(suggestionKey(key('Escape'))).toBe('dismiss'));
	it.each(['shiftKey', 'ctrlKey', 'altKey', 'metaKey', 'isComposing', 'repeat'])(
		'preserves %s',
		modifier => expect(suggestionKey(key('Enter', { [modifier]: true }))).toBe(null)
	);
	it('preserves IME keycode', () =>
		expect(suggestionKey(key('Enter', { keyCode: 229 }))).toBe(null));
});