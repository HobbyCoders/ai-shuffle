import { describe, expect, it } from 'vitest';
import { canMoveSession, viewerIsNonOwner } from './session-move';

const saved = {
	sessionId: 'chat-1',
	row: { api_user_id: null },
	isSubagent: false,
	viewerAuthenticated: true,
	viewerIsAdmin: true
};

describe('canMoveSession', () => {
	it('lets the owner move a saved chat', () => {
		expect(canMoveSession(saved)).toBe(true);
	});

	it('refuses an unsaved chat', () => {
		expect(canMoveSession({ ...saved, sessionId: null })).toBe(false);
		expect(canMoveSession({ ...saved, row: null })).toBe(false);
	});

	it("refuses a subagent's own chat", () => {
		expect(canMoveSession({ ...saved, isSubagent: true })).toBe(false);
	});

	it('allows a chat started by an API-key user (the move carries that user over)', () => {
		expect(canMoveSession({ ...saved, row: { api_user_id: 'key-user' } })).toBe(true);
	});

	it('refuses a viewer who is not the owner', () => {
		expect(canMoveSession({ ...saved, viewerIsAdmin: false })).toBe(false);
	});

	it('keeps the control while the auth status is still loading', () => {
		expect(canMoveSession({ ...saved, viewerAuthenticated: false, viewerIsAdmin: false })).toBe(true);
	});
});

describe('viewerIsNonOwner', () => {
	it('is true only for a signed-in viewer without owner rights', () => {
		expect(viewerIsNonOwner(true, false)).toBe(true);
		expect(viewerIsNonOwner(true, true)).toBe(false);
		expect(viewerIsNonOwner(false, false)).toBe(false);
	});
});
