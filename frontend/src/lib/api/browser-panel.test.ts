import { afterEach, describe, it, expect, vi } from 'vitest';
import { startPanelBrowser } from './browser-panel';
const state = { runtime_id: 'runtime', revision: 1, available: true, target_id: 'tab', active_tab_id: 'tab',
 tabs: [{ id: 'tab', current: true, title: 'New tab', url: 'about:blank' }],
 viewport: { mode: 'fixed', width: 1280, height: 800 }, control: { owner: 'agent', viewer_id: null, pending_viewer_id: null }, busy: false };
afterEach(() => vi.unstubAllGlobals());
describe('panel browser start API', () => {
 it('uses the authenticated session API with an empty body and cancellation', async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(state)));
  vi.stubGlobal('fetch', fetch); const controller = new AbortController();
  expect(await startPanelBrowser('session/one', controller.signal)).toEqual(state);
  expect(fetch).toHaveBeenCalledWith('/api/v1/sessions/session%2Fone/browser/panel/start', expect.objectContaining({
   method: 'POST', credentials: 'include', body: undefined, signal: controller.signal,
  }));
  expect(fetch).toHaveBeenCalledTimes(1);
 });
 it('rejects an envelope instead of accepting an undocumented contract', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ state }))));
  await expect(startPanelBrowser('a', new AbortController().signal)).rejects.toThrow('did not return a workspace');
 });
 it('preserves actionable API failure details', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'Runtime unavailable. Try again.' }), { status: 503 })));
  await expect(startPanelBrowser('a', new AbortController().signal)).rejects.toMatchObject({ status: 503, detail: 'Runtime unavailable. Try again.' });
 });
 it('rejects an invalid workspace rather than navigating optimistically', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ state: {} }))));
  await expect(startPanelBrowser('a', new AbortController().signal)).rejects.toThrow('did not return a workspace');
 });
});