import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';
const mocks = vi.hoisted(() => ({ isVoiceEnabled: vi.fn(), startCallForSession: vi.fn() }));
vi.mock('$lib/api/voice', () => ({ isVoiceEnabled: mocks.isVoiceEnabled }));
vi.mock('$lib/stores/active-session', async () => ({ activeSession: (await import('svelte/store')).writable({ sessionId: 'home', mode: 'code' }) }));
vi.mock('$lib/stores/app-data.svelte', async () => ({ sessions: (await import('svelte/store')).writable([]) }));
vi.mock('$lib/services/voiceCall', async () => ({ startCallForSession: mocks.startCallForSession, voiceCallState: (await import('svelte/store')).writable({ status: 'idle' }), isCallSurfaceActive: () => false }));
import { activeSession } from '$lib/stores/active-session';
import VoiceCallButton from './VoiceCallButton.svelte';
const active = activeSession as unknown as Writable<{ sessionId: string | null; mode: string }>;
beforeEach(() => { vi.clearAllMocks(); mocks.isVoiceEnabled.mockResolvedValue(true); active.set({ sessionId: 'home', mode: 'code' }); });
afterEach(cleanup);
describe('call action on a conversation', () => {
 it.each(['code', 'chat'])('offers explicit call attachment for %s without changing context', async mode => {
  active.set({ sessionId: 'home', mode }); render(VoiceCallButton);
  const button = await screen.findByRole('button', { name: 'Start a call in this conversation' });
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
  await fireEvent.click(button);
  expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true });
 });
 it('retains secret reconnect for a historical voice session', async () => {
  active.set({ sessionId: 'legacy', mode: 'voice' }); render(VoiceCallButton);
  await fireEvent.click(await screen.findByRole('button', { name: 'Start a call in this conversation' }));
  expect(mocks.startCallForSession).toHaveBeenCalledWith('legacy', { attach: false });
 });
 it('never creates an implicit voice session for an empty composer', async () => {
  active.set({ sessionId: null, mode: 'chat' }); render(VoiceCallButton);
  await Promise.resolve(); expect(screen.queryByRole('button')).toBeNull();
 });
 it('is absent when the configured voice surface is disabled', async () => {
  mocks.isVoiceEnabled.mockResolvedValue(false); render(VoiceCallButton);
  await Promise.resolve(); expect(screen.queryByRole('button')).toBeNull();
 });
});
