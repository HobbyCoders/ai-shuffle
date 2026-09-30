import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('./runtime', () => ({ apiBase: () => '/api/v1', withAuth: (init: RequestInit) => init }));
import { prepareConversationCall } from './voice';
afterEach(() => vi.unstubAllGlobals());
describe('existing conversation call attachment', () => {
 it('sends no agent, model, permission or project override and consumes the same-session credential', async () => {
  const reply = { session_id: 'home', client_secret: '', expires_at: null, model: 'configured-call-model', options: {} };
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(reply), { status: 200 }));
  vi.stubGlobal('fetch', fetch);
  expect(await prepareConversationCall('home')).toEqual(reply);
  expect(fetch).toHaveBeenCalledWith('/api/v1/voice/sessions/home/attach', expect.objectContaining({ method: 'POST', body: '{}' }));
 });
 it('surfaces refusal instead of creating a replacement voice session', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Conversation is not eligible for call attachment.' }), { status: 404 }));
  vi.stubGlobal('fetch', fetch);
  await expect(prepareConversationCall('hidden')).rejects.toMatchObject({ status: 404, detail: 'Conversation is not eligible for call attachment.' });
  expect(fetch).toHaveBeenCalledTimes(1);
 });
});
