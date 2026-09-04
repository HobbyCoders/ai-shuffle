import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(() => Promise.resolve({})), delete: vi.fn(() => Promise.resolve({})) } }));
vi.mock('./auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));
import { api } from '$lib/api/client';
import { notify, notifications, notificationHistory, unreadCount } from './notifications';
beforeEach(() => {
 for (const n of notifications.current.live) notify.dismissLive(n.id);
 notify.clearAll();
 vi.clearAllMocks();
});
describe('two notification surfaces', () => {
 it.each(['error', 'warning', 'info', 'success'] as const)('%s goes only to the bell and persists', (severity) => {
  const id = notify[severity]('Message');
  expect(notifications.current.live).toEqual([]);
  expect(notificationHistory.current).toMatchObject([{ id, severity, surface: 'notification', persist: true }]);
  expect(api.post).toHaveBeenCalledWith('/notifications', expect.objectContaining({id, severity, surface: 'notification'}));
 });
 it('keeps banners out of the bell and sticky until dismissed', () => {
  const id = notify.banner('Update ready', {severity: 'info'});
  expect(notificationHistory.current).toEqual([]);
  expect(notifications.current.live).toMatchObject([{id, surface: 'banner', duration_ms: 0}]);
  expect(api.post).not.toHaveBeenCalled();
  notify.dismissLive(id);
  expect(notifications.current.live).toEqual([]);
 });
 it('keeps explicitly persisted banners in bell history without reposting server events', async () => {
  notify.ingest({id:'live-only', severity:'warning', surface:'banner', message:'Connecting', persist:false});
  expect(notificationHistory.current).toEqual([]);
  notify.ingest({id:'saved', severity:'error', surface:'banner', message:'Reconnect account', persist:true});
  expect(notificationHistory.current).toMatchObject([{id:'saved'}]);
  expect(api.post).not.toHaveBeenCalled();
  notify.clearAll();
  vi.mocked(api.get).mockResolvedValue({notifications:[{id:'saved',severity:'error',surface:'banner',message:'Reconnect account'}]});
  await notify.loadHistory();
  expect(notificationHistory.current).toMatchObject([{id:'saved',surface:'notification'}]);
 });
 it('persists client banners only when explicitly requested', () => {
  const id = notify.banner('Keep this', {persist:true});
  expect(notificationHistory.current).toMatchObject([{id}]);
  expect(api.post).toHaveBeenCalledWith('/notifications', expect.objectContaining({id,surface:'banner'}));
 });
 it('allows local-only bell items without silently discarding them', () => {
  notify.info('Local', {persist: false});
  expect(notificationHistory.current).toHaveLength(1);
  expect(api.post).not.toHaveBeenCalled();
 });
 it.each(['toast','inline',undefined] as const)('normalizes legacy %s events, without reposting or duplicating', (surface) => {
  const row = {id: 'server-row', severity: 'info' as const, surface, message: 'Done'};
  notify.ingest(row);
  notify.markRead(row.id);
  vi.clearAllMocks();
  notify.ingest(row);
  expect(notificationHistory.current).toMatchObject([{id: row.id, surface:'notification', read:true}]);
  expect(notificationHistory.current).toHaveLength(1);
  expect(notifications.current.live).toEqual([]);
  expect(api.post).not.toHaveBeenCalled();
 });
 it('preserves actions and session navigation metadata through server echoes and history loading', async () => {
  const onClick = vi.fn();
  const id = notify.success('Done', {session_id:'session-1', action:{label:'Open', onClick}});
  notify.ingest({id, severity:'success', message:'Done'});
  vi.mocked(api.get).mockResolvedValue({notifications:[{id, severity:'success',surface:'toast',message:'Done'}]});
  await notify.loadHistory();
  expect(notificationHistory.current).toHaveLength(1);
  expect(notificationHistory.current[0].session_id).toBe('session-1');
  notificationHistory.current[0].action?.onClick();
  expect(onClick).toHaveBeenCalledOnce();
 });
 it('hydrates legacy rows once, excludes dismissed rows, and never shows history live', async () => {
  vi.mocked(api.get).mockResolvedValue({notifications:[
   {id:'old',severity:'info',surface:'inline',message:'Old'},
   {id:'old',severity:'info',surface:'toast',message:'Old'},
   {id:'gone',severity:'error',message:'Gone',dismissed:true}
  ]});
  await notify.loadHistory();
  expect(notificationHistory.current).toMatchObject([{id:'old',surface:'notification'}]);
  expect(notificationHistory.current).toHaveLength(1);
  expect(notifications.current.live).toEqual([]);
 });
 it('marks read, dismisses and clears locally and remotely', () => {
  const first = notify.info('First');
  notify.success('Second');
  expect(unreadCount.current).toBe(2);
  notify.markRead(first);
  expect(unreadCount.current).toBe(1);
  notify.markAllRead();
  expect(unreadCount.current).toBe(0);
  notify.dismiss(first);
  expect(notificationHistory.current).toHaveLength(1);
  notify.clearAll();
  expect(notificationHistory.current).toEqual([]);
  expect(api.post).toHaveBeenCalledWith(`/notifications/${first}/read`);
  expect(api.post).toHaveBeenCalledWith(`/notifications/${first}/dismiss`);
  expect(api.delete).toHaveBeenCalledWith('/notifications');
 });
});
