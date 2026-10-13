import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { bindSessionOwner, sessionOwnerBoundary, sessionOwnerEpoch, TAB_SESSION_OWNER_KEY } from './session-owner';
import { TAB_ACTIVE_SESSION_KEY } from './device-id';
import { sessionEphemeralCache, type SessionEphemeral } from './session-cache';
import { readDraft, writeDraft } from './session/draft-persistence';

function seedPrivateState() {
 sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, JSON.stringify({ sessionId: 'alice-session' }));
 sessionEphemeralCache.save('__new_chat__', { draft: 'private prompt' } as SessionEphemeral);
 writeDraft(null, 'private new draft');
 writeDraft('alice-session', 'private session draft');
 writeDraft(null, 'private new draft');
}
beforeEach(() => { sessionStorage.clear(); sessionEphemeralCache.clear(); });
afterEach(() => vi.restoreAllMocks());

describe('session owner boundary', () => {
 it('preserves the same account across startup and reconnect, even if another tab changed accounts', () => {
  sessionStorage.setItem(TAB_SESSION_OWNER_KEY, JSON.stringify('account:alice'));
  seedPrivateState();
  const epoch = sessionOwnerEpoch();
  expect(bindSessionOwner(undefined, 'account:alice', 'account:bob')).toBe(false);
  expect(bindSessionOwner('account:alice', 'account:alice')).toBe(false);
  expect(sessionOwnerEpoch()).toBe(epoch);
  expect(readDraft(null)).toBe('private new draft');
  expect(sessionEphemeralCache.restore('__new_chat__')?.draft).toBe('private prompt');
  expect(sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY)).toContain('alice-session');
 });
 it('retires active-session and all draft caches on an account change without clearing unrelated tab data', () => {
  seedPrivateState(); sessionStorage.setItem('unrelated', 'keep');
  const remount = vi.fn(); const unsubscribe = sessionOwnerBoundary.subscribe(remount);
  remount.mockClear();
  const editorEpoch = sessionOwnerEpoch();
  expect(bindSessionOwner('account:alice', 'account:bob')).toBe(true);
  expect(sessionOwnerEpoch()).not.toBe(editorEpoch);
  expect(readDraft(null)).toBeNull(); expect(readDraft('alice-session')).toBeNull();
  expect(sessionEphemeralCache.restore('__new_chat__')).toBeNull();
  expect(sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY)).toBeNull();
  expect(sessionStorage.getItem('unrelated')).toBe('keep');
  expect(remount).toHaveBeenCalledOnce();
  bindSessionOwner('account:bob', 'account:bob');
  expect(remount).toHaveBeenCalledOnce();
  unsubscribe();
 });
 it('uses this tab ownership after a reload instead of trusting a newer browser-wide owner', () => {
  sessionStorage.setItem(TAB_SESSION_OWNER_KEY, JSON.stringify('account:alice'));
  seedPrivateState();
  expect(bindSessionOwner(undefined, 'account:bob', 'account:bob')).toBe(true);
  expect(readDraft(null)).toBeNull();
 });
 it('clears on logout and does not restore old text when another account logs in', () => {
  seedPrivateState();
  expect(bindSessionOwner('account:alice', null)).toBe(true);
  expect(bindSessionOwner(undefined, 'account:bob', 'account:alice')).toBe(true);
  expect(readDraft('alice-session')).toBeNull();
  expect(sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY)).toBeNull();
 });
 it('allows first-use legacy migration only when ownership is unknown or matches', () => {
  seedPrivateState();
  expect(bindSessionOwner(undefined, 'account:alice', 'account:alice')).toBe(false);
  expect(readDraft(null)).toBe('private new draft');
  sessionStorage.removeItem(TAB_SESSION_OWNER_KEY);
  expect(bindSessionOwner(undefined, 'account:bob', 'account:alice')).toBe(true);
  expect(readDraft(null)).toBeNull();
 });
 it('still retires memory and editor callbacks when browser storage is unavailable', () => {
  seedPrivateState();
  vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('denied'); });
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('denied'); });
  const epoch = sessionOwnerEpoch();
  expect(() => bindSessionOwner('account:alice', 'account:bob')).not.toThrow();
  expect(sessionOwnerEpoch()).not.toBe(epoch);
  expect(sessionEphemeralCache.restore('__new_chat__')).toBeNull();
 });
});
