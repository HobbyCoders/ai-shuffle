/** Account-scoped defaults. Session transcripts and existing run bindings stay local to their session. */
import { preferenceSync } from '$lib/services/preferences';
import type { ClusterProject } from '$lib/api/installs';
import { sessionOwnerEpoch } from './session-owner';

type ProjectPick = { projectId: string; projectV2Id: string | null };
// A `branch` pick (New branch..., or a branch/checkout picked from the list) is
// one-time: the first session created on it spends it (#1528), so the next new
// chat falls back to Auto. A legacy `existing` worktree-ID pick still persists.
type CheckoutPick = ProjectPick & ({kind:'existing'; worktreeId:string; branch:string}
 | {kind:'branch'; branch:string; baseBranch:string; createNew:boolean});
interface Picks {
 profile: string; project: ProjectPick; checkout: CheckoutPick | null;
 model: string | null; effort: string | null; permissionMode: string | null;
 contextWindowChoice: string | null; fastMode: string | null; worktreeIntent: 'local' | 'worktree';
}
const legacy: Partial<Record<keyof Picks, string>> = {
 profile:'aihub_selectedProfile', model:'aihub_lastModel', effort:'aihub_lastEffort',
 permissionMode:'aihub_lastPermissionMode', contextWindowChoice:'aihub_lastContextWindowChoice',
 fastMode:'aihub_lastFastMode', worktreeIntent:'aihub_executionMode'
};
const defaults: Picks = {profile:'', project:{projectId:'',projectV2Id:null}, checkout:null,
 model:null, effort:null, permissionMode:null, contextWindowChoice:null, fastMode:null, worktreeIntent:'local'};
const values = {...defaults};
let checkoutRevision = 0;
export interface BranchClaim { consume(): void; release(): void }
const claims = new Set<{revision: number; ownerEpoch: number}>();
function sameCheckout(a: CheckoutPick | null, b: CheckoutPick | null) {
 if (a === b) return true;
 if (!a || !b || a.kind !== b.kind || a.projectId !== b.projectId
  || a.projectV2Id !== b.projectV2Id || a.branch !== b.branch) return false;
 return a.kind === 'branch' && b.kind === 'branch'
  ? a.baseBranch === b.baseBranch && a.createNew === b.createNew
  : a.kind === 'existing' && b.kind === 'existing' && a.worktreeId === b.worktreeId;
}
const listeners = new Set<() => void>();
let pendingNotificationEpoch: number | null = null;
function notifyRemote() {
 const epoch = sessionOwnerEpoch();
 if (pendingNotificationEpoch === epoch) return;
 pendingNotificationEpoch = epoch;
 // A snapshot applies its registered keys synchronously. Publish the complete
 // selection once, before a restored branch starts protecting that draft.
 queueMicrotask(() => {
  if (pendingNotificationEpoch !== epoch) return;
  pendingNotificationEpoch = null;
  if (sessionOwnerEpoch() !== epoch) return;
  for (const listener of listeners) listener();
 });
}
const projectPick = (v: unknown): v is ProjectPick => !!v && typeof v === 'object'
 && typeof (v as ProjectPick).projectId === 'string'
 && ((v as ProjectPick).projectV2Id === null || typeof (v as ProjectPick).projectV2Id === 'string');

function normalize<K extends keyof Picks>(key: K, value: unknown): Picks[K] {
 if (key === 'project') return (projectPick(value) ? {projectId:value.projectId,projectV2Id:value.projectV2Id} : defaults.project) as Picks[K];
 if (key === 'checkout') {
  if (!projectPick(value) || typeof (value as CheckoutPick).branch !== 'string') return null as Picks[K];
  const pick = value as CheckoutPick;
  if (pick.kind === 'branch' && typeof pick.baseBranch === 'string' && typeof pick.createNew === 'boolean') return {...pick} as Picks[K];
  if ('worktreeId' in pick && typeof pick.worktreeId === 'string') return {...pick,kind:'existing'} as Picks[K];
  return null as Picks[K];
 }
 if (key === 'worktreeIntent') return (value === 'worktree' ? 'worktree' : 'local') as Picks[K];
 // Preserve explicit access choices for future sessions, including Bypass.
 // Existing sessions restore their own binding, never this default.
 if (key === 'permissionMode' && value === 'plan') return 'default' as Picks[K];
 return (typeof value === 'string' ? value : defaults[key]) as Picks[K];
}
function store<K extends keyof Picks>(key: K, value: unknown) {
 const normalized = normalize(key, value);
 // Identical account refreshes are not new choices. Actual remote changes are.
 if (key === 'checkout' && !sameCheckout(values.checkout, normalized as CheckoutPick | null)) checkoutRevision++;
 values[key] = normalized;
 try {
  localStorage.setItem(`ai-shuffle-composer:${key}`, JSON.stringify(values[key]));
  if (key === 'project') localStorage.setItem('aihub_selectedProject', values.project.projectId);
  const oldKey = legacy[key];
  if (oldKey) {
   if (values[key]) localStorage.setItem(oldKey, String(values[key])); else localStorage.removeItem(oldKey);
  }
 } catch { /* memory still works if storage is unavailable */ }
}
for (const key of Object.keys(defaults) as (keyof Picks)[]) {
 try {
  const cached = localStorage.getItem(`ai-shuffle-composer:${key}`);
  const old = legacy[key] ? localStorage.getItem(legacy[key]!) : key === 'project'
   ? {projectId:localStorage.getItem('aihub_selectedProject') || '', projectV2Id:null} : undefined;
  store(key, cached !== null ? JSON.parse(cached) : old);
 } catch { /* SSR or unavailable storage */ }
 preferenceSync.register(`composer.${key}`, {
  read: () => values[key],
  apply(value) { store(key, value); notifyRemote(); }
 });
}
export const composerPreferences = {
 get current(): Readonly<Picks> { return values; },
 set<K extends keyof Picks>(key: K, value: Picks[K]) {
  // A deliberate re-pick is a new intent even when its fields are identical.
  if (key === 'checkout') checkoutRevision++;
  store(key, value); preferenceSync.save(`composer.${key}`, values[key]);
 },
 /**
  * Claim the remembered branch choice that this create request is spending,
  * before the POST. While claimed, `checkoutClaimed` is true so a New Chat
  * opened mid-create does not stage it. `consume` clears the unchanged choice
  * after success; `release` (always safe to call, e.g. from `finally`) ends
  * the claim and keeps the choice for a retry.
  */
 claimBranchChoice(project: ProjectPick, branch: string, createNew: boolean): BranchClaim {
  const pick = values.checkout;
  const matchesProject = pick && (pick.projectV2Id
   ? pick.projectV2Id === project.projectV2Id : pick.projectId === project.projectId);
  if (pick?.kind !== 'branch' || pick.createNew !== createNew || pick.branch !== branch || !matchesProject) {
   return {consume() {}, release() {}};
  }
  const claim = {revision: checkoutRevision, ownerEpoch: sessionOwnerEpoch()};
  claims.add(claim);
  return {
   consume() {
    if (!claims.delete(claim)) return;
    if (checkoutRevision === claim.revision && sessionOwnerEpoch() === claim.ownerEpoch) {
     composerPreferences.set('checkout', null);
    }
   },
   release() { claims.delete(claim); },
  };
 },
 /** True while the current checkout choice is spoken for by an in-flight create. */
 get checkoutClaimed() {
  for (const claim of claims) {
   if (claim.revision === checkoutRevision && claim.ownerEpoch === sessionOwnerEpoch()) return true;
  }
  return false;
 },
 onRemote(listener: () => void) { listeners.add(listener); return () => listeners.delete(listener); },
 async hydrate() { await preferenceSync.reconnect(); },
};

/** Resolve by shared project UUID against this worker's authorized catalog, never by path/name. */
export function resolveComposerProject(pick: ProjectPick, projects: {id:string}[], cluster: ClusterProject[]) {
 const linked = pick.projectV2Id ? cluster.find(p => p.project_v2_id === pick.projectV2Id) : undefined;
 const localId = pick.projectV2Id ? linked?.local_project_id ?? '' : pick.projectId;
 return {project: projects.some(p => p.id === localId) ? localId : '', projectV2Id: pick.projectV2Id};
}
