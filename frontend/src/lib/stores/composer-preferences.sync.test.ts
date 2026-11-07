// #1078 through the real PreferenceSync and a versioned account store: a
// branch move held across retired identities must survive sync settling.
import {beforeEach, expect, it, vi} from 'vitest';
const server = vi.hoisted(() => {
 const rows = new Map<string, {value:unknown; version:string}>();
 let n = 0;
 const row = (key:string) => rows.get(key) ?? null;
 // Like the backend, a client write ID becomes the stored version.
 const write = (key:string, value:unknown, writeId?:string) => {
  rows.set(key, {value:JSON.parse(JSON.stringify(value)), version:writeId ?? `v${++n}`}); return rows.get(key)!;
 };
 // Keys whose next write commits but loses its response.
 const lose = new Set<string>();
 return {rows, row, write, lose, reset() { rows.clear(); lose.clear(); n = 0; }};
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
  // Like the backend: only_if_missing keeps any stored row (null included), a
  // retry of the write the row already holds is applied, and any other stale
  // expected version is a conflict.
  put: async (key, value, onlyIfMissing, expected, writeId) => {
   const existing = server.row(key);
   if (onlyIfMissing && existing) return existing;
   if (expected !== undefined && (existing?.version ?? null) !== expected) {
    if (writeId && existing?.version === writeId) return existing;
    throw Object.assign(new Error('conflict'), {status:409});
   }
   const saved = server.write(key, value, writeId);
   if (server.lose.delete(key)) throw new Error('network: response lost');
   return saved;
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

it.each([
 ['its own branch', {intent:'worktree', checkout:{...ids.N, kind:'branch', branch:'topic/theirs', baseBranch:'main', createNew:false}}],
 ['Auto, equal to the promoted choice', {intent:'worktree', checkout:null}],
])('never overwrites a choice another device saved over the retired identity: %s', async (_, theirs) => {
 const {p, key, settle, claim} = await setup();
 p.linkWorktreeProject(ids.N, ids.O); await settle();
 p.linkWorktreeProject(ids.M, ids.N); await settle();
 // Another device saves under N; this one has not seen it yet, so its
 // promotion back onto N loses the version check.
 server.write(key(ids.N), theirs);
 p.linkWorktreeProject(ids.N, ids.M); await settle();
 claim.release(); await settle();
 expect(server.row(key(ids.N))?.value).toEqual(theirs);
 expect(p.worktreeForProject(ids.N)).toEqual(theirs);
});

it('keeps a held branch when the promotion commits but its response is lost', async () => {
 const {p, key, settle, claim} = await setup();
 p.linkWorktreeProject(ids.N, ids.O); await settle();
 p.linkWorktreeProject(ids.M, ids.N); await settle();
 // The save back onto N commits; reconnect retries it from the old version.
 server.lose.add(key(ids.N));
 p.linkWorktreeProject(ids.N, ids.M); await settle();
 expect(server.lose.size).toBe(0);
 claim.release(); await settle();
 const expected = {intent:'worktree', checkout:branch('N')};
 expect(p.worktreeForProject(ids.N)).toEqual(expected);
 expect(server.row(key(ids.N))?.value).toEqual(expected);
});
