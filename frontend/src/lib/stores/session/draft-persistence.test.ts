import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { draftKey, readDraft, writeDraft, clearDraft } from './draft-persistence';

/**
 * Composer draft persistence — per-tab sessionStorage mirror of the debounced
 * chat draft, so a reload (e.g. a silent PWA update) doesn't wipe in-progress
 * text. These tests pin the key scheme, the empty-clears / migrate behaviour,
 * and that every storage failure degrades to a silent no-op.
 */

beforeEach(() => {
	sessionStorage.clear();
});

afterEach(() => {
	vi.restoreAllMocks();
});

describe('draft-persistence · draftKey', () => {
	it('uses a per-session key for a real session id', () => {
		expect(draftKey('sess-123')).toBe('__aihub_draft_sess-123');
	});

	it('uses the new-chat slot when there is no session', () => {
		expect(draftKey(null)).toBe('__aihub_draft_new');
	});
});

describe('draft-persistence · write / read', () => {
	it('persists and reads back a draft for a session', () => {
		writeDraft('sess-1', 'hello world');
		expect(sessionStorage.getItem('__aihub_draft_sess-1')).toBe('hello world');
		expect(readDraft('sess-1')).toBe('hello world');
	});

	it('persists a new-chat draft under the _new slot', () => {
		writeDraft(null, 'draft with no session yet');
		expect(sessionStorage.getItem('__aihub_draft_new')).toBe('draft with no session yet');
		expect(readDraft(null)).toBe('draft with no session yet');
	});

	it('returns null when nothing is stored', () => {
		expect(readDraft('missing')).toBeNull();
		expect(readDraft(null)).toBeNull();
	});

	it('clears the entry when written with empty text', () => {
		writeDraft('sess-2', 'something');
		writeDraft('sess-2', '');
		expect(sessionStorage.getItem('__aihub_draft_sess-2')).toBeNull();
		expect(readDraft('sess-2')).toBeNull();
	});

	it('clears the entry when written with whitespace-only text', () => {
		writeDraft('sess-3', 'something');
		writeDraft('sess-3', '   \n\t ');
		expect(readDraft('sess-3')).toBeNull();
	});

	it('migrates away from the _new slot once a real session id is used', () => {
		writeDraft(null, 'started in new chat');
		expect(readDraft(null)).toBe('started in new chat');

		writeDraft('sess-real', 'now on a real session');
		expect(readDraft('sess-real')).toBe('now on a real session');
		// The stale _new slot is removed so it can't leak back.
		expect(sessionStorage.getItem('__aihub_draft_new')).toBeNull();
	});
});

describe('draft-persistence · clearDraft', () => {
	it('removes a session draft', () => {
		writeDraft('sess-4', 'bye');
		clearDraft('sess-4');
		expect(readDraft('sess-4')).toBeNull();
	});

	it('removes the new-chat draft', () => {
		writeDraft(null, 'bye');
		clearDraft(null);
		expect(readDraft(null)).toBeNull();
	});
});

describe('draft-persistence · storage failures are silent no-ops', () => {
	it('readDraft returns null when getItem throws', () => {
		vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
			throw new DOMException('denied');
		});
		expect(readDraft('sess-5')).toBeNull();
	});

	it('writeDraft swallows a setItem quota error', () => {
		vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
			throw new DOMException('QuotaExceededError');
		});
		expect(() => writeDraft('sess-6', 'x')).not.toThrow();
	});

	it('clearDraft swallows a removeItem error', () => {
		vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => {
			throw new DOMException('denied');
		});
		expect(() => clearDraft('sess-7')).not.toThrow();
	});
});
