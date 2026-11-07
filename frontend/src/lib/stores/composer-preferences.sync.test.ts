// #1078 through the real PreferenceSync and a versioned account store: a
// branch move held across retired identities must survive sync settling.
import {beforeEach, expect, it, vi} from 'vitest';
const server = vi.hoisted(() => {
 const rows = new Map<string, {value:unknown; version:string}>();
 let n = 0;
 const row = (key:string) => rows.get(key) ?? null;
 const write = (key:string, value:unknown) => { rows.set(key, {value:JSON.parse(JSON.stringify(value)), version:`v${++n}`}); return rows.get(key)!; };
 return {rows, row, write, reset() { rows.clear(); n = 0; }};
});
vi.mock('./session-owner', () => ({sessionOwnerEpoch:() => 0}));
// A fresh sync per test: the composer store re-imports each time, the mock does not.
const holder = vi.hoisted(() => ({sync:null as any}));
vi.mock('$lib/services/preferences', () => ({preferenceSync:new Proxy({}, {get:(_, name) => {
 const value = holder.sync[name];
 return typeof value === 'function' ? value.bind(holder.sync) : value;
}})}));
async function freshSync() {
 const {PreferenceSync} = await import('$lib/services/preference-sync');
 holder.sync = new PreferenceSync({
  all: async () => Object.fromEntries([...server.rows].map(([k, r]) => [k, r.value])),
  allWithVersions: async () => ({preferences:Object.fromEntries([...server.rows].map(([k, r]) => [k, r.value])),
   versions:Object.fromEntries([...server.rows].map(([k, r]) => [k, r.version]))}),
  get: async key => server.row(key),
  // Like the backend: only_if_missing keeps any stored row (null included), and
  // a stale expected version is a conflict.
  put: async (key, value, onlyIfMissing, expected) => {
   const existing = server.row(key);
   if (onlyIfMissing && existing) return existing;
   if (expected !== undefined && (existing?.version ?? null) !== expected) throw Object.assign(new Error('conflict'), {status:409});
   return server.write(key, value);
  },
 });
}
beforeEach(async () => { vi.resetModules(); localStorage.clear(); server.reset(); await freshSync(); });

const ids = {O:{projectId:'a', projectV2Id:'pv2-o'}, N:{projectId:'a', projectV2Id:'pv2-n'}, M:{projectId:'a', projectV2Id:'pv2-m'}};
const branch = (id: keyof typeof ids) => ({...ids[id], kind:'branch' as const, branch:'topic/old', baseBranch:'main', createNew:true});
async function setup() {
 const {preferenceSync} = await import('$lib/services/preferences');
 const {composerPreferences:p, projectWorktreeKey:key} = await import('./composer-preferences');
 preferenceSync.start();
 const settle = async () => { for (let i = 0; i < 3; i++) { await preferenceSync.reconnect(); await new Promise(r => setTimeout(r, 0)); } };
 await settle();
 p.rememberWorktreeIntent(ids.O, 'worktree');
 p.rememberCheckout(ids.O, branch('O'));
 await settle();
 return {p, key, settle, claim:p.claimBranchChoice(ids.O, 'topic/old', true)};
}

it('keeps a held branch and its worktree intent when a chain returns to an identity it retired', async () => {
 const {p, key, settle, claim} = await setup();
 for (const [to, from] of [['N', 'O'], ['M', 'N'], ['N', 'M']] as const) {
  p.linkWorktreeProject(ids[to], ids[from]);
  await settle();
 }
 claim.release();
 await settle();
 const expected = {intent:'worktree', checkout:branch('N')};
 expect(p.worktreeForProject(ids.N)).toEqual(expected);
 expect(server.row(key(ids.N))?.value).toEqual(expected);
 expect(server.row(key(ids.O))?.value).toBeNull();
 expect(server.row(key(ids.M))?.value).toBeNull();
});

it('never overwrites a choice another device saved over the retired identity', async () => {
 const {p, key, settle, claim} = await setup();
 p.linkWorktreeProject(ids.N, ids.O); await settle();
 p.linkWorktreeProject(ids.M, ids.N); await settle();
 // Another device picks its own branch under N; this one has not seen it yet.
 const theirs = {intent:'worktree', checkout:{...ids.N, kind:'branch', branch:'topic/theirs', baseBranch:'main', createNew:false}};
 server.write(key(ids.N), theirs);
 p.linkWorktreeProject(ids.N, ids.M); await settle();
 claim.release(); await settle();
 expect(server.row(key(ids.N))?.value).toEqual(theirs);
 expect(p.worktreeForProject(ids.N)).toEqual(theirs);
});
