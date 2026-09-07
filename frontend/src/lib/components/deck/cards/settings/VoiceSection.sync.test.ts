import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
const identity = vi.hoisted(() => ({ epoch: 0, listeners: new Set<() => void>() }));
vi.mock('$lib/api/client', () => ({ api: transport }));
vi.mock('$lib/stores/session-owner', () => ({
 sessionOwnerEpoch: () => identity.epoch,
 sessionOwnerBoundary: { subscribe(callback: () => void) {
  identity.listeners.add(callback); callback(); return () => identity.listeners.delete(callback);
 } }
}));
import VoiceSection from './VoiceSection.svelte';
import { resourceSync } from '$lib/services/resource-sync';

const settings = (voice = 'marin', minutes = 60) => ({ voice_mode_enabled: true,
 voice_model: 'gpt-realtime-2.1', voice_voice: voice, voice_provider_id: null,
 voice_max_call_minutes: minutes, voice_app_tools_enabled: false });
function changeAccount() {
 identity.epoch++;
 for (const listener of identity.listeners) listener();
}
beforeEach(() => { transport.get.mockReset(); transport.put.mockReset(); changeAccount(); });
afterEach(() => { cleanup(); resourceSync.clear(); });

async function mountSection() {
 let server = settings();
 transport.get.mockImplementation(async (path: string) => path === '/settings/voice' ? {...server} : []);
 const view = render(VoiceSection);
 await screen.findByLabelText('Agent voice');
 return { ...view, remote(value: ReturnType<typeof settings>) {
  server = value; resourceSync.invalidate(['settings']);
 } };
}

describe('mounted voice settings synchronization', () => {
 it('refreshes controls without sending settings writes', async () => {
  const view = await mountSection();
  view.remote(settings('cedar', 90));
  await waitFor(() => expect((screen.getByLabelText('Agent voice') as HTMLSelectElement).value).toBe('cedar'));
  expect((screen.getByLabelText('Max call minutes') as HTMLInputElement).value).toBe('90');
  expect(transport.put).not.toHaveBeenCalled();
 });

 it('keeps an uncommitted minutes field while other controls refresh', async () => {
  const view = await mountSection();
  await fireEvent.input(screen.getByLabelText('Max call minutes'), {target:{value:'123'}});
  view.remote(settings('cedar', 90));
  await waitFor(() => expect((screen.getByLabelText('Agent voice') as HTMLSelectElement).value).toBe('cedar'));
  expect((screen.getByLabelText('Max call minutes') as HTMLInputElement).value).toBe('123');
  expect(transport.put).not.toHaveBeenCalled();
 });

 it('prevents an old editor from saving after an account change', async () => {
  await mountSection();
  await fireEvent.input(screen.getByLabelText('Max call minutes'), {target:{value:'123'}});
  changeAccount();
  await fireEvent.blur(screen.getByLabelText('Max call minutes'));
  expect(transport.put).not.toHaveBeenCalled();
 });
});
