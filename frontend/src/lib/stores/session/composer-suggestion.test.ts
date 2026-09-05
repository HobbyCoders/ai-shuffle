import { describe, it, expect } from 'vitest';
import { get } from 'svelte/store';
import {
	createComposerSuggestion,
	suggestionKey,
	hasComposerDraft,
	type SuggestionContext
} from './composer-suggestion';
const idle: SuggestionContext = {
	sessionId: 's',
	mode: 'code',
	enabled: true,
	turnId: null,
	streaming: false,
	blocked: false
};
const frame = { session_id: 's', turn_id: 't', suggestion: '<img src=x> **literal**' };
function setup() {
	const store = createComposerSuggestion();
	store.observe(idle);
	store.observe({ ...idle, turnId: 't', streaming: true });
	return store;
}
describe('ephemeral composer suggestions', () => {
	it('recognizes empty saved editor documents but blocks text and chips', () => {
		expect(hasComposerDraft('')).toBe(false);
		expect(hasComposerDraft('{"type":"doc","content":[{"type":"paragraph"}]}')).toBe(false);
		expect(hasComposerDraft('draft')).toBe(true);
		expect(
			hasComposerDraft(
				'{"type":"doc","content":[{"type":"paragraph","content":[{"type":"fileChip"}]}]}'
			)
		).toBe(true);
	});
	it.each([true, false])('keeps legitimate completion ordering (suggestion first: %s)', before => {
		const s = setup();
		if (before) {
			s.receive(frame);
			expect(get(s)).toBe('');
		}
		s.observe(idle);
		if (!before) s.receive(frame);
		expect(get(s)).toBe(frame.suggestion);
		expect(s.accept()).toBe(frame.suggestion);
		expect(get(s)).toBe('');
		s.receive(frame);
		expect(get(s)).toBe('');
	});
	it('rejects wrong-session, wrong-turn, invalid and duplicate frames', () => {
		const s = setup();
		s.observe(idle);
		for (const f of [
			{ ...frame, session_id: 'other' },
			{ ...frame, turn_id: 'old' },
			{ ...frame, suggestion: 42 },
			{ ...frame, suggestion: '  ' }
		])
			s.receive(f);
		expect(get(s)).toBe('');
		s.receive(frame);
		s.receive({ ...frame, suggestion: 'overwrite' });
		expect(get(s)).toBe(frame.suggestion);
	});
	it('typing then erasing invalidates a pending generation', () => {
		const s = setup();
		s.invalidate();
		s.observe({ ...idle, blocked: true });
		s.observe(idle);
		s.receive(frame);
		expect(get(s)).toBe('');
	});
	it.each(['switch', 'mode', 'disable', 'attachment', 'stop', 'reset'])(
		'invalidates on %s',
		reason => {
			const s = setup();
			s.receive(frame);
			if (reason === 'switch') {
				s.observe({ ...idle, sessionId: 'other' });
				s.observe(idle);
			}
			if (reason === 'mode') {
				s.observe({ ...idle, mode: 'chat' });
				s.observe(idle);
			}
			if (reason === 'disable') {
				s.observe({ ...idle, enabled: false });
				s.observe(idle);
			}
			if (reason === 'attachment') {
				s.observe({ ...idle, blocked: true });
				s.observe(idle);
			}
			if (reason === 'stop') {
				s.invalidate();
				s.observe(idle);
			}
			if (reason === 'reset') {
				s.reset();
				s.observe(idle);
			}
			s.receive(frame);
			expect(get(s)).toBe('');
		}
	);
	it('an optimistic new turn cannot restore the previous suggestion', () => {
		const s = setup();
		s.observe(idle);
		s.receive(frame);
		s.observe({ ...idle, streaming: true });
		s.observe(idle);
		s.receive(frame);
		expect(get(s)).toBe('');
	});
	it('new turn rejects the previous reply and can receive its own', () => {
		const s = setup();
		s.observe(idle);
		s.receive(frame);
		s.observe({ ...idle, turnId: 'new', streaming: true });
		expect(get(s)).toBe('');
		s.receive(frame);
		s.observe(idle);
		expect(get(s)).toBe('');
		s.receive({ ...frame, turn_id: 'new' });
		expect(get(s)).toBe(frame.suggestion);
	});
	it('does not restore on mount or enabling an idle session', () => {
		const s = createComposerSuggestion();
		s.observe({ ...idle, turnId: 't' });
		s.receive(frame);
		expect(get(s)).toBe('');
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
	it.each(['Tab', 'Enter'])('%s accepts, not submits', k =>
		expect(suggestionKey(key(k))).toBe('accept')
	);
	it('Escape dismisses', () => expect(suggestionKey(key('Escape'))).toBe('dismiss'));
	it.each(['shiftKey', 'ctrlKey', 'altKey', 'metaKey', 'isComposing', 'repeat'])(
		'preserves %s',
		modifier => expect(suggestionKey(key('Enter', { [modifier]: true }))).toBe(null)
	);
	it('preserves IME keycode', () =>
		expect(suggestionKey(key('Enter', { keyCode: 229 }))).toBe(null));
});
