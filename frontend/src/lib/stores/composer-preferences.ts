/** Account-scoped defaults. Session transcripts and existing run bindings stay local to their session. */
import { preferenceSync } from '$lib/services/preferences';
import { startupDedupeEnabled } from '$lib/api/client';
import type { ClusterProject } from '$lib/api/installs';
import { sessionOwnerEpoch } from './session-owner';
import { getDefaultModel, canonicalEffortModel, isLegacyEffortAlias, legacyEffortFamily } from '$lib/constants/models';
import { EFFORT_OPTIONS, reconcileModelEffort, resolveModelEffort } from '$lib/constants/model-effort';

export function effortModelKey(model: string | null): string {
 const selected = model || getDefaultModel();
 return canonicalEffortModel(selected);
}

type ProjectPick = { projectId: string; projectV2Id: string | null };
// A `branch` pick (New branch..., or a branch/checkout picked from the list) is
// one-time: the first session created on it spends it (#1528), so the next new
// chat falls back to Auto. A legacy `existing` worktree-ID pick still persists.
type CheckoutPick = ProjectPick & ({kind:'existing'; worktreeId:string; branch:string}
 | {kind:'branch'; branch:string; baseBranch:string; createNew:boolean});
interface Picks {
 profile: string; project: ProjectPick; checkout: CheckoutPick | null;
 model: string | null; effort: string | null; modelEfforts: Record<string, string | null>; permissionMode: string | null;
 contextWindowChoice: string | null; fastMode: string | null; worktreeIntent: 'local' | 'worktree';
}
const legacy: Partial<Record<keyof Picks, string>> = {
 profile:'aihub_selectedProfile', model:'aihub_lastModel', effort:'aihub_lastEffort',
 permissionMode:'aihub_lastPermissionMode', contextWindowChoice:'aihub_lastContextWindowChoice',
 fastMode:'aihub_lastFastMode', worktreeIntent:'aihub_executionMode'
};
const defaults: Picks = {profile:'', project:{projectId:'',projectV2Id:null}, checkout:null,
 model:null, effort:null, modelEfforts:{}, permissionMode:null, contextWindowChoice:null, fastMode:null, worktreeIntent:'local'};
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
 if (key === 'modelEfforts') return Object.fromEntries(
  value && typeof value === 'object' && !Array.isArray(value) ? Object.entries(value)
   .filter(([model, effort]) => model.length > 0 && (effort === null || typeof effort === 'string'
    && EFFORT_OPTIONS.some(option => option.value === effort)))
   // Old family keys remain fallbacks; never manufacture a concrete choice from them.
   .map(([model, effort]) => [(isLegacyEffortAlias(model) && legacyEffortFamily(model)) || effortModelKey(model), effort]) : []
 ) as Picks[K];
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
export const MODEL_EFFORT_PREFIX = 'composer.modelEffort.';
export const effortPreferenceKey = (model: string | null) => MODEL_EFFORT_PREFIX + encodeURIComponent(effortModelKey(model));
const registeredEfforts = new Set<string>();
const appliedEfforts = new Set<string>();
function applyEffort(model: string, value: unknown) {
 const next = {...values.modelEfforts};
 if (value === undefined) delete next[model];
 else next[model] = typeof value === 'string' && EFFORT_OPTIONS.some(option => option.value === value) ? value : null;
 store('modelEfforts', next);
}
function effortAdapter(model: string) {
 registeredEfforts.add(model);
 return {
  readOnly: isLegacyEffortAlias(model),
  read: () => values.modelEfforts[model] ?? null,
  apply(value: unknown) {
   if (value === undefined) appliedEfforts.delete(model); else appliedEfforts.add(model);
   applyEffort(model, value); notifyRemote();
  }
 };
}
function registerEffort(model: string) {
 // Family records are discovered read-only from account snapshots, never created.
 if (!isLegacyEffortAlias(model) && !registeredEfforts.has(model))
  preferenceSync.register(effortPreferenceKey(model), effortAdapter(model));
}
for (const key of Object.keys(defaults) as (keyof Picks)[]) {
 try {
  const cached = localStorage.getItem(`ai-shuffle-composer:${key}`);
  const old = legacy[key] ? localStorage.getItem(legacy[key]!) : key === 'project'
   ? {projectId:localStorage.getItem('aihub_selectedProject') || '', projectV2Id:null} : undefined;
  // Migrate only the last model/effort pair; never seed unrelated models.
  const initial = key === 'modelEfforts' && cached === null && values.model && values.effort
   ? {[values.model]: values.effort} : old;
  store(key, cached !== null ? JSON.parse(cached) : initial);
 } catch { /* SSR or unavailable storage */ }
 if (key === 'modelEfforts') {
  // Read/migrate the old map, but never write new choices into that conflict unit.
  preferenceSync.register('composer.modelEfforts', {
   read: () => null,
   apply(value) {
    if (value === undefined) { appliedEfforts.clear(); store('modelEfforts', {}); }
    else for (const [model, effort] of Object.entries(normalize('modelEfforts', value))) {
     if (!appliedEfforts.has(model)) applyEffort(model, effort);
     registerEffort(model);
    }
    notifyRemote();
   }
  });
 } else preferenceSync.register(`composer.${key}`, {
  read: () => values[key],
  apply(value) { store(key, value); notifyRemote(); }
 });
}
preferenceSync.registerPrefix(MODEL_EFFORT_PREFIX, key => {
 try {
  const model = decodeURIComponent(key.slice(MODEL_EFFORT_PREFIX.length));
  const family = isLegacyEffortAlias(model) && legacyEffortFamily(model) === model;
  return model && (family || effortPreferenceKey(model) === key) ? effortAdapter(model) : null;
 } catch { return null; }
});
for (const model of Object.keys(values.modelEfforts)) registerEffort(model);

// Fast mode per model, one account-synced record each. Unlike effort there is
// no older whole-map preference: the map below is only this browser's cache.
export const MODEL_FAST_PREFIX = 'composer.modelFast.';
export const fastPreferenceKey = (model: string | null) => MODEL_FAST_PREFIX + encodeURIComponent(effortModelKey(model));
const FAST_CACHE_KEY = 'ai-shuffle-composer:modelFast';
let modelFast: Record<string, boolean> = {};
const registeredFast = new Set<string>();
function applyFast(model: string, value: unknown) {
 const next = {...modelFast};
 if (typeof value === 'boolean') next[model] = value; else delete next[model];
 modelFast = next;
 try { localStorage.setItem(FAST_CACHE_KEY, JSON.stringify(modelFast)); } catch { /* memory still works */ }
}
function fastAdapter(model: string) {
 registeredFast.add(model);
 return {
  read: () => modelFast[model] ?? null,
  apply(value: unknown) { applyFast(model, value); notifyRemote(); }
 };
}
function registerFast(model: string) {
 if (!registeredFast.has(model)) preferenceSync.register(fastPreferenceKey(model), fastAdapter(model));
}
try {
 const cached = localStorage.getItem(FAST_CACHE_KEY);
 const parsed: unknown = cached === null ? null : JSON.parse(cached);
 if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
  for (const [model, on] of Object.entries(parsed)) if (model && typeof on === 'boolean') modelFast[effortModelKey(model)] = on;
 } else if (cached === null && values.model && values.fastMode === '1') {
  // Migrate only the last model/fast pair; never seed unrelated models.
  applyFast(effortModelKey(values.model), true);
 }
} catch { /* SSR or unavailable storage */ }
preferenceSync.registerPrefix(MODEL_FAST_PREFIX, key => {
 try {
  const model = decodeURIComponent(key.slice(MODEL_FAST_PREFIX.length));
  return model && fastPreferenceKey(model) === key ? fastAdapter(model) : null;
 } catch { return null; }
});
for (const model of Object.keys(modelFast)) registerFast(model);

export const composerPreferences = {
 /** The owner's last fast-mode pick for this model; null when never picked. */
 fastForModel(model: string | null): boolean | null {
  return modelFast[effortModelKey(model)] ?? null;
 },
 rememberFast(model: string | null, on: boolean | null) {
  const key = effortModelKey(model);
  registerFast(key);
  applyFast(key, on);
  preferenceSync.save(fastPreferenceKey(key), on);
 },
 get current(): Readonly<Picks> { return values; },
 effortForModel(model: string | null): string | null {
  const key = effortModelKey(model), family = legacyEffortFamily(model || getDefaultModel());
  const saved = Object.prototype.hasOwnProperty.call(values.modelEfforts, key)
   ? values.modelEfforts[key] : family ? values.modelEfforts[family] : null;
  return reconcileModelEffort(saved ?? null, resolveModelEffort(model));
 },
 rememberEffort(model: string | null, effort: string | null) {
  const key = effortModelKey(model);
  registerEffort(key);
  applyEffort(key, effort);
  appliedEfforts.add(key);
  // Null is a replicated tombstone: legacy maps cannot resurrect a clear.
  preferenceSync.save(effortPreferenceKey(key), effort);
 },
 set<K extends keyof Picks>(key: K, value: Picks[K]) {
  if (key === 'modelEfforts') {
   const next = Object.fromEntries(Object.entries(normalize('modelEfforts', value))
    .map(([model, effort]) => [effortModelKey(model), effort]));
   for (const model of Object.keys(values.modelEfforts))
    if (isLegacyEffortAlias(model)) applyEffort(model, undefined);
   for (const model of new Set([...Object.keys(values.modelEfforts), ...Object.keys(next)]))
    composerPreferences.rememberEffort(model, next[model] ?? null);
   return;
  }
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
 async hydrate() {
  await (startupDedupeEnabled() ? preferenceSync.hydrate() : preferenceSync.reconnect());
 },
};

/** Resolve by shared project UUID against this worker's authorized catalog, never by path/name. */
export function resolveComposerProject(pick: ProjectPick, projects: {id:string}[], cluster: ClusterProject[]) {
 const linked = pick.projectV2Id ? cluster.find(p => p.project_v2_id === pick.projectV2Id) : undefined;
 const localId = pick.projectV2Id ? linked?.local_project_id ?? '' : pick.projectId;
 return {project: projects.some(p => p.id === localId) ? localId : '', projectV2Id: pick.projectV2Id};
}
