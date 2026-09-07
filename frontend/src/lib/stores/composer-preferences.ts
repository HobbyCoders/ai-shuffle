/** Account-scoped defaults. Session transcripts and existing run bindings stay local to their session. */
import { preferenceSync } from '$lib/services/preferences';
import type { ClusterProject } from '$lib/api/installs';

type ProjectPick = { projectId: string; projectV2Id: string | null };
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
const listeners = new Set<() => void>();
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
 return (typeof value === 'string' ? value : defaults[key]) as Picks[K];
}
function store<K extends keyof Picks>(key: K, value: unknown) {
 values[key] = normalize(key, value);
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
  apply(value) { store(key, value); for (const listener of listeners) listener(); }
 });
}
export const composerPreferences = {
 get current(): Readonly<Picks> { return values; },
 set<K extends keyof Picks>(key: K, value: Picks[K]) {
  store(key, value); preferenceSync.save(`composer.${key}`, values[key]);
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
