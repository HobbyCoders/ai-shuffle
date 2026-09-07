import { beforeEach, expect, it, vi } from 'vitest';
vi.mock('$lib/api/client', () => ({ api: { get: vi.fn(), post: vi.fn(async () => ({})), delete: vi.fn(async () => ({})) } }));
vi.mock('./auth.svelte', () => ({ auth: { current: { status: { authenticated: true } } } }));
import { api } from '$lib/api/client';
import { notify, notifications } from './notifications';
const row = (id: string, read = false) => ({id, read, severity: 'info' as const, message: id});
beforeEach(async () => { notify.clearAll(); await Promise.resolve(); vi.clearAllMocks(); });

it('applies remote read state and removes remotely deleted persisted rows', async () => {
 notify.ingest(row('a')); notify.ingest(row('b'));
 vi.mocked(api.get).mockResolvedValue({notifications: [row('a', true)]});
 await notify.loadHistory();
 expect(notifications.current.history).toMatchObject([{id:'a', read:true}]);
});

it('preserves new events, local read and local dismiss while a snapshot is in flight', async () => {
 notify.ingest(row('a')); notify.ingest(row('b'));
 let finish!: (value: unknown) => void;
 vi.mocked(api.get).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
 const loading = notify.loadHistory();
 await vi.waitFor(() => expect(finish).toBeTypeOf('function'));
 notify.markRead('a'); notify.dismiss('b'); notify.ingest(row('new'));
 finish({notifications:[row('a'), row('b')]});
 await loading;
 expect(notifications.current.history.map(n => n.id).sort()).toEqual(['a', 'new']);
 expect(notifications.current.history.find(n => n.id === 'a')?.read).toBe(true);
});
