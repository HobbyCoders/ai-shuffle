import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';
const mocks = vi.hoisted(() => ({ isVoiceEnabled: vi.fn(), startCallForSession: vi.fn(), startSessionWithoutPrompt: vi.fn(), confirmCallReplacement: vi.fn(), banner: vi.fn(() => 'banner-id'), dismissLive: vi.fn() }));
vi.mock('$lib/stores/notifications', () => ({ notify: { banner: mocks.banner, dismissLive: mocks.dismissLive } }));
vi.mock('$lib/api/voice', () => ({ isVoiceEnabled: mocks.isVoiceEnabled }));
vi.mock('$lib/stores/active-session', async () => ({ activeSession: Object.assign((await import('svelte/store')).writable({}), { startSessionWithoutPrompt: mocks.startSessionWithoutPrompt }) }));
vi.mock('$lib/stores/app-data.svelte', async () => ({ sessions: (await import('svelte/store')).writable([]) }));
vi.mock('$lib/services/voiceCall', async () => ({ startCallForSession: mocks.startCallForSession, confirmCallReplacement: mocks.confirmCallReplacement, voiceCallState: (await import('svelte/store')).writable({ status: 'idle' }), isCallSurfaceActive: () => false }));
import { activeSession } from '$lib/stores/active-session';
import { sessions } from '$lib/stores/app-data.svelte';
import VoiceCallButton from './VoiceCallButton.svelte';
import { assistantState } from '$lib/stores/assistant';
import type { AssistantState } from '$lib/api/assistant';
type State = { sessionId: string | null; mode: string; isStreaming: boolean; isCreatingWorktree?: boolean; deviceIntent?: { id: string; name: string } | null; activeTasks: { status: string }[]; pendingPermissions: unknown[]; pendingQuestions: unknown[] };
const active = activeSession as unknown as Writable<State>;
const rows = sessions as unknown as Writable<Record<string, unknown>[]>;
const ready: State = { sessionId: 'home', mode: 'code', isStreaming: false, activeTasks: [], pendingPermissions: [], pendingQuestions: [] };
const callButton = () => screen.findByRole('button', { name: 'Start a call in this conversation' });
beforeEach(() => { vi.clearAllMocks(); mocks.isVoiceEnabled.mockResolvedValue(true); mocks.confirmCallReplacement.mockResolvedValue(true); active.set(ready); rows.set([]); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); assistantState.set(null); });
/** A phone or tablet: no hover, so a tooltip never shows. */
const touchScreen = () => vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(hover: none)', media: query, addEventListener() {}, removeEventListener() {} }));
describe('call action on a conversation', () => {
 it.each(['code', 'chat'])('offers explicit call attachment for %s without changing context', async mode => {
  active.set({ ...ready, mode }); render(VoiceCallButton);
  const button = await callButton();
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
  await fireEvent.click(button);
  expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true });
 });
 it('retains secret reconnect for a historical voice session', async () => {
  active.set({ ...ready, sessionId: 'legacy', mode: 'voice' }); render(VoiceCallButton);
  await fireEvent.click(await callButton());
  expect(mocks.startCallForSession).toHaveBeenCalledWith('legacy', { attach: false });
 });
 it('is absent when the configured voice surface is disabled', async () => {
  mocks.isVoiceEnabled.mockResolvedValue(false); render(VoiceCallButton);
  await Promise.resolve(); expect(screen.queryByRole('button')).toBeNull();
 });
 it.each([
  ['archived', { status: 'archived' }],
  ['remote', { is_remote: true }],
  ['subagent', { parent_session_id: 'parent' }],
 ])('stays hidden on a %s conversation', async (_label, row) => {
  rows.set([{ id: 'home', status: 'active' }]); render(VoiceCallButton);
  await callButton();
  rows.set([{ id: 'home', status: 'active', ...row }]);
  await vi.waitFor(() => expect(screen.queryByRole('button')).toBeNull());
 });
});
describe('the assistant home takes a call at any time', () => {
 const home = () => assistantState.set({ home_session_id: 'home' } as AssistantState);
 it.each([
  ['streaming', { isStreaming: true }, []],
  ['running background tasks', { activeTasks: [{ status: 'running' }] }, []],
  ['waiting on a permission prompt', { pendingPermissions: [{ request_id: 'p' }] }, []],
  ['waiting on a question', { pendingQuestions: [{ request_id: 'q' }] }, []],
  ['reporting running tasks on its row', {}, [{ id: 'home', status: 'active', has_running_tasks: true }]],
  ['parked on background work', {}, [{ id: 'home', status: 'active', parked_task_kind: 'agent' }]],
 ])('stays enabled while %s', async (_label, state, sessionRows) => {
  home(); active.set({ ...ready, ...state }); rows.set(sessionRows); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  expect(button.title).toBe('Start a call in this conversation');
  await fireEvent.click(button);
  expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true });
 });
 it('keeps the ready-or-finished gate on every other conversation', async () => {
  assistantState.set({ home_session_id: 'another-home' } as AssistantState);
  active.set({ ...ready, isStreaming: true }); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(button.title).toMatch(/finishes working/);
 });
 it('follows the home when the assistant state arrives after the composer', async () => {
  active.set({ ...ready, isStreaming: true }); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  home();
  await vi.waitFor(() => expect(button.disabled).toBe(false));
 });
});
describe('call start gate while the session is busy', () => {
 it.each([
  ['streaming', { isStreaming: true }, [], /finishes working/],
  ['running background tasks', { activeTasks: [{ status: 'running' }] }, [], /finishes working/],
  ['waiting on a permission prompt', { pendingPermissions: [{ request_id: 'p' }] }, [], /finishes working/],
  ['waiting on a question', { pendingQuestions: [{ request_id: 'q' }] }, [], /finishes working/],
  ['reporting running tasks on its row', {}, [{ id: 'home', status: 'active', has_running_tasks: true }], /finishes working/],
  ['parked on background work', {}, [{ id: 'home', status: 'active', parked_task_kind: 'agent' }], /background tasks finish/],
 ])('is disabled with a reason while %s', async (_label, state, sessionRows, reason) => {
  active.set({ ...ready, ...state }); rows.set(sessionRows); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(button.title).toMatch(reason);
  await fireEvent.click(button);
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
 });
 it('re-enables once the session completes', async () => {
  active.set({ ...ready, isStreaming: true }); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  active.set({ ...ready });
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  expect(button.title).toBe('Start a call in this conversation');
  await fireEvent.click(button);
  expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true });
 });
 it('keeps the composer upload/recording gate and names it', async () => {
  render(VoiceCallButton, { disabled: true, disabledReason: 'Recording...' });
  const button = await callButton() as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(button.title).toBe('Recording...');
 });
});
describe('a blocked call button explains itself on touch screens', () => {
 it.each([
  ['a working session', { isStreaming: true }, /finishes working/, []],
  ['a parked session', {}, /background tasks finish/, [{ id: 'home', status: 'active', parked_task_kind: 'agent' }]],
 ])('a tap on %s shows the tooltip reason as a brief banner', async (_label, state, reason, sessionRows) => {
  touchScreen(); active.set({ ...ready, ...state }); rows.set(sessionRows); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  // Still tappable so the tap can explain, but announced as unavailable.
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  expect(button.getAttribute('aria-disabled')).toBe('true');
  await fireEvent.click(button);
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
  expect(mocks.banner).toHaveBeenCalledTimes(1);
  const [message, options] = mocks.banner.mock.calls[0] as unknown as [string, Record<string, unknown>];
  expect(message).toBe(button.title);
  expect(message).toMatch(reason);
  expect(options).toMatchObject({ persist: false });
  expect(options.duration_ms as number).toBeGreaterThan(0);
 });
 it('replaces the previous explanation instead of stacking banners', async () => {
  touchScreen(); active.set({ ...ready, isStreaming: true }); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  await fireEvent.click(button);
  await fireEvent.click(button);
  expect(mocks.banner).toHaveBeenCalledTimes(2);
  expect(mocks.dismissLive).toHaveBeenCalledWith('banner-id');
 });
 it('starts the call normally on touch once the session is ready', async () => {
  touchScreen(); render(VoiceCallButton);
  const button = await callButton();
  expect(button.getAttribute('aria-disabled')).toBeNull();
  await fireEvent.click(button);
  expect(mocks.banner).not.toHaveBeenCalled();
  expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true });
 });
 it('keeps the native disabled button, and no banner, with a mouse', async () => {
  active.set({ ...ready, isStreaming: true }); render(VoiceCallButton);
  const button = await callButton() as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  await fireEvent.click(button);
  expect(mocks.banner).not.toHaveBeenCalled();
 });
});
describe('call button on the new-session composer (#1616)', () => {
 const fresh: State = { ...ready, sessionId: null, isCreatingWorktree: false, deviceIntent: null };
 const freshButton = () => screen.findByRole('button', { name: 'Start a call in a new conversation' });
 it.each(['code', 'chat'])('is offered in the %s lane and creates the conversation, then calls it', async mode => {
  mocks.startSessionWithoutPrompt.mockResolvedValue('created');
  active.set({ ...fresh, mode }); render(VoiceCallButton);
  const button = await freshButton() as HTMLButtonElement;
  expect(button.disabled).toBe(false);
  await fireEvent.click(button);
  await vi.waitFor(() => expect(mocks.startCallForSession).toHaveBeenCalledWith('created', { attach: true }));
  expect(mocks.startSessionWithoutPrompt).toHaveBeenCalledTimes(1);
  // A call running elsewhere is settled before anything is created.
  expect(mocks.confirmCallReplacement).toHaveBeenCalledWith(null);
  expect(mocks.confirmCallReplacement.mock.invocationCallOrder[0])
   .toBeLessThan(mocks.startSessionWithoutPrompt.mock.invocationCallOrder[0]);
 });
 it('starts no call when the session could not be created', async () => {
  mocks.startSessionWithoutPrompt.mockResolvedValue(null);
  active.set(fresh); render(VoiceCallButton);
  const button = await freshButton() as HTMLButtonElement;
  await fireEvent.click(button);
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  expect(mocks.startSessionWithoutPrompt).toHaveBeenCalledTimes(1);
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
 });
 it('creates nothing when the user keeps the call already running', async () => {
  mocks.confirmCallReplacement.mockResolvedValue(false);
  active.set(fresh); render(VoiceCallButton);
  const button = await freshButton() as HTMLButtonElement;
  await fireEvent.click(button);
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  expect(mocks.confirmCallReplacement).toHaveBeenCalled();
  expect(mocks.startSessionWithoutPrompt).not.toHaveBeenCalled();
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
 });
 it('a double press creates one session and starts one call', async () => {
  let finish!: (id: string) => void;
  mocks.startSessionWithoutPrompt.mockReturnValue(new Promise<string>(resolve => { finish = resolve; }));
  active.set(fresh); render(VoiceCallButton);
  const button = await freshButton();
  await fireEvent.click(button);
  await fireEvent.click(button);
  await vi.waitFor(() => expect(mocks.startSessionWithoutPrompt).toHaveBeenCalledTimes(1));
  finish('created');
  await vi.waitFor(() => expect(mocks.startCallForSession).toHaveBeenCalledTimes(1));
 });
 it.each([
  ['another device is picked as the runner', { deviceIntent: { id: 'worker-1', name: 'Studio PC' } }, /Switch the runner to Local.*Studio PC/],
  ['a first message is already creating the session', { isStreaming: true }, /once this session is created/],
  ['a worktree is being set up', { isCreatingWorktree: true }, /once this session is created/],
 ])('is disabled with a reason when %s', async (_label, state, reason) => {
  active.set({ ...fresh, ...state }); render(VoiceCallButton);
  const button = await freshButton() as HTMLButtonElement;
  expect(button.disabled).toBe(true);
  expect(button.title).toMatch(reason);
  await fireEvent.click(button);
  expect(mocks.startSessionWithoutPrompt).not.toHaveBeenCalled();
 });
 it('explains a blocked press on touch screens', async () => {
  touchScreen(); active.set({ ...fresh, deviceIntent: { id: 'worker-1', name: 'Studio PC' } }); render(VoiceCallButton);
  const button = await freshButton() as HTMLButtonElement;
  await vi.waitFor(() => expect(button.disabled).toBe(false));
  await fireEvent.click(button);
  expect(mocks.banner).toHaveBeenCalledWith(button.title, expect.objectContaining({ persist: false }));
  expect(mocks.startSessionWithoutPrompt).not.toHaveBeenCalled();
 });
});
