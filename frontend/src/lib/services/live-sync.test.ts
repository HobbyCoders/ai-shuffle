import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const m = vi.hoisted(() => ({ bindings:new Set<string>(),listener: undefined as undefined | ((s: any)=>void), start:vi.fn(),stop:vi.fn(),reconnect:vi.fn(),refresh:vi.fn(),invalidate:vi.fn(),clear:vi.fn(),handle:vi.fn(),unsubscribe:vi.fn(),check:vi.fn().mockResolvedValue(undefined),loadProjects:vi.fn(),loadClusterProjects:vi.fn(),loadProjectCatalog:vi.fn(),loadSessions:vi.fn(),
 feed:{invalidate:vi.fn(),invalidateUnverified:vi.fn(),isFeeding:vi.fn(()=>false),hasLogEpoch:vi.fn(()=>false),restart:vi.fn(),revive:vi.fn()} }));
vi.mock('$lib/stores/auth.svelte',()=>({auth:{current:{status:{preview_access:false}},subscribe:(fn:(s:any)=>void)=>{m.listener=fn;return m.unsubscribe;},checkAuth:m.check}}));
vi.mock('./preferences',()=>({preferenceSync:{start:m.start,stop:m.stop,reconnect:m.reconnect,refresh:m.refresh,register:(key:string)=>m.bindings.add(key)}}));
vi.mock('./resource-sync', async (importOriginal) => {
 const actual = await importOriginal<typeof import('./resource-sync')>();
 const sync = new actual.ResourceSync();
 m.invalidate.mockImplementation(sync.invalidate.bind(sync));
 m.clear.mockImplementation(sync.clear.bind(sync));
 return { ...actual, resourceSync: { subscribe: vi.fn(sync.subscribe.bind(sync)), invalidate: m.invalidate, clear: m.clear } };
});
vi.mock('$lib/stores/active-session',()=>({handleGlobalUserEvent:m.handle,activeSession:{current:{sessionId:null},applyComposerDefaults:vi.fn(),resetForAccountChange:vi.fn()}}));
vi.mock('$lib/stores/app-data.svelte',()=>({appData:{loadProfiles:vi.fn().mockResolvedValue(undefined),loadProjects:m.loadProjects,loadClusterProjects:m.loadClusterProjects,loadProjectCatalog:m.loadProjectCatalog,loadSessions:m.loadSessions}}));
vi.mock('$lib/stores/notifications',()=>({notify:{resetLocal:vi.fn()}}));
vi.mock('$lib/constants/models',()=>({hydrateModelRegistry:vi.fn().mockResolvedValue(true)}));
vi.mock('$lib/stores/session/v2/feed-controller',()=>({feedController:m.feed}));
vi.mock('$lib/stores/theme.svelte',()=>({}));
vi.mock('$lib/stores/timezonePreference.svelte',()=>({}));
vi.mock('$lib/stores/chatPreferences.svelte',()=>({}));
vi.mock('$lib/stores/hotkeyPreferences.svelte',()=>({}));
vi.mock('$lib/stores/gitActionPreferences.svelte',()=>({}));
vi.mock('$lib/stores/modelFavorites.svelte',()=>({}));
vi.mock('$lib/stores/files.svelte',()=>({}));
vi.mock('$lib/stores/groups.svelte',()=>({}));
vi.mock('$lib/stores/layout.svelte',()=>({}));
vi.mock('$lib/stores/sidebar-prefs.svelte',()=>({}));
vi.mock('$lib/stores/sessionMode.svelte',()=>({}));
vi.mock('$lib/stores/browser-panel.svelte',()=>({}));
const access = vi.hoisted(() => ({get: vi.fn()}));
vi.mock('$lib/api/client', async (importOriginal) => {
 const actual = await importOriginal<typeof import('$lib/api/client')>();
 return {...actual, api: {...actual.api, get: access.get}};
});
import { render, screen, cleanup } from '@testing-library/svelte';
import { tick } from 'svelte';
import ChatAccess from '$lib/components/chat/ChatAccess.svelte';
import { startLiveSync } from './live-sync';
import { resourceSync } from './resource-sync';
import { hydrateModelRegistry } from '$lib/constants/models';
import { activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { TAB_ACTIVE_SESSION_KEY } from '$lib/stores/device-id';
import { TAB_SESSION_OWNER_KEY } from '$lib/stores/session-owner';
import { readDraft, writeDraft } from '$lib/stores/session/draft-persistence';
class Socket {
 static OPEN=1;static CLOSED=3;static all:Socket[]=[];readyState=0;
 onopen?:()=>void;onmessage?:(event:{data:string})=>void;onclose?:(event:{code:number})=>void;onerror?:()=>void;
 constructor(public url:string){Socket.all.push(this);}
 send=vi.fn();close=vi.fn(()=>{this.readyState=3;this.onclose?.({code:1000});});
 emit(data:unknown){this.onmessage?.({data:JSON.stringify(data)});}
}
let stop:()=>void;
function account(username:string|null) {m.listener?.({loading:false,status:{authenticated:!!username,username,preference_identity:username ? {user_type:'admin',user_id:username} : null}});}
beforeEach(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});vi.useFakeTimers();vi.clearAllMocks();(activeSession.current as {sessionId:string|null}).sessionId=null;m.feed.isFeeding.mockReturnValue(false);m.feed.hasLogEpoch.mockReturnValue(false);Socket.all=[];localStorage.clear();sessionStorage.clear();vi.stubGlobal('WebSocket',Socket);});
afterEach(()=>{stop?.();vi.useRealTimers();vi.unstubAllGlobals();});
describe('root authenticated live-sync lifecycle',()=>{
 it('delivers a scoped socket access change through the real resource subscriber to the approval card', async () => {
  Object.defineProperty(document, 'hidden', {configurable:true, value:false});
  const empty = {requests:[],actions:[],grants:[],saved_connections:[],vault_available:false};
  let snapshot: Record<string, unknown> = empty;
  access.get.mockImplementation(async (url: string) => url.startsWith('/sessions/') ? {mode:'code'} : snapshot);
  stop=startLiveSync();account('alice');
  const view=render(ChatAccess,{sessionId:'s1'});
  try {
   await vi.advanceTimersByTimeAsync(0);
   snapshot = {...empty, actions:[{id:'a1',grant_id:'g1',operation:'command',arguments:{command:'pwd'},expires_at:9999999999}]};
   Socket.all[0].emit({type:'chat_access_changed',session_id:'other'});
   await vi.advanceTimersByTimeAsync(100);await tick();
   expect(screen.queryByText('Approve this remote command?')).toBeNull();
   Socket.all[0].emit({type:'chat_access_changed',session_id:'s1'});
   await vi.advanceTimersByTimeAsync(100);await tick();
   expect(screen.getByText('Approve this remote command?')).toBeTruthy();
   expect(access.get.mock.calls.filter(([url])=>url.startsWith('/chat-access/'))).toHaveLength(2);
   expect(access.get.mock.calls.filter(([url])=>url.startsWith('/sessions/'))).toHaveLength(1);
  } finally {view.unmount();cleanup();resourceSync.clear();}
 });
 it.each(['settings','agents'])('refreshes synced %s and reapplies composer defaults without opening Settings',async(resource)=>{
  stop=startLiveSync();account('alice');
  const subscription=vi.mocked(resourceSync.subscribe).mock.calls.find(([names])=>names.includes(resource));
  const loader=resource==='settings'?hydrateModelRegistry:appData.loadProfiles;
  expect(subscription).toBeDefined();
  await subscription![1]();
  expect(loader).toHaveBeenCalledOnce();
  expect(activeSession.applyComposerDefaults).toHaveBeenCalledOnce();
  expect(vi.mocked(loader).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(activeSession.applyComposerDefaults).mock.invocationCallOrder[0]);
 });

 it('registers the install choice before authentication starts the first snapshot',()=>{
  expect(m.bindings.has('install_prompt')).toBe(true);
  expect(m.start).not.toHaveBeenCalled();
 });
 it('retires the old session before preference resets and clears stale tab restores',()=>{
  stop=startLiveSync();account('alice');
  sessionStorage.setItem(TAB_ACTIVE_SESSION_KEY, '{"sessionId":"private"}');
  writeDraft(null,'alice private draft');
  m.stop.mockClear();
  account('bob');
  expect(activeSession.resetForAccountChange).toHaveBeenCalledOnce();
  expect(readDraft(null)).toBeNull();
  expect(sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY)).toBeNull();
  expect(vi.mocked(activeSession.resetForAccountChange).mock.invocationCallOrder[0]).toBeLessThan(m.stop.mock.invocationCallOrder[0]);
 });
 it('preserves a same-owner startup draft and retires a different owner after reload',()=>{
  sessionStorage.setItem(TAB_SESSION_OWNER_KEY, JSON.stringify('admin:alice'));
  localStorage.setItem('ai-shuffle-preferences-owner','admin:bob');
  writeDraft(null,'alice private draft');
  stop=startLiveSync();account('alice');
  expect(activeSession.resetForAccountChange).not.toHaveBeenCalled();
  expect(readDraft(null)).toBe('alice private draft');
  stop();
  stop=startLiveSync();account('bob');
  expect(activeSession.resetForAccountChange).toHaveBeenCalledOnce();
  expect(readDraft(null)).toBeNull();
 });
 it('opens a cookie-auth global socket outside chat and refetches on every sync_ready',()=>{
  stop=startLiveSync();account('alice');const socket=Socket.all[0];
  expect(socket.url).toContain('/api/v1/ws/global?device_id=');expect(socket.url).not.toContain('token=');
  socket.emit({type:'sync_ready'});socket.emit({type:'sync_ready'});
  expect(m.reconnect).toHaveBeenCalledTimes(3);expect(m.invalidate).toHaveBeenCalledTimes(2);
  socket.emit({type:'preferences_changed',keys:['theme','theme',17]});expect(m.refresh).toHaveBeenCalledTimes(1);
  socket.emit({type:'resources_changed',resources:['notes',null]});expect(m.invalidate).toHaveBeenLastCalledWith(['notes']);
 });
 it('leaves the open transcript alone on the first sync_ready and resumes it at the cursor after a reconnect',async()=>{
  (activeSession.current as {sessionId:string|null}).sessionId='s1';
  m.feed.isFeeding.mockReturnValue(true);m.feed.hasLogEpoch.mockReturnValue(true);
  stop=startLiveSync();account('alice');m.feed.invalidate.mockClear();
  // The feed started its own load before this socket existed; nothing was missed.
  Socket.all[0].emit({type:'sync_ready'});
  expect(m.feed.restart).not.toHaveBeenCalled();expect(m.feed.revive).not.toHaveBeenCalled();
  expect(m.feed.invalidate).not.toHaveBeenCalled();expect(m.feed.invalidateUnverified).not.toHaveBeenCalled();
  // A reconnect follows a gap: resume at the cursor, the server checks the epoch.
  Socket.all[0].close();await vi.advanceTimersByTimeAsync(1000);
  Socket.all[1].emit({type:'sync_ready'});
  expect(m.feed.revive).toHaveBeenCalledExactlyOnceWith('s1');
  expect(m.feed.invalidateUnverified).toHaveBeenCalledOnce();
  expect(m.feed.restart).not.toHaveBeenCalled();expect(m.feed.invalidate).not.toHaveBeenCalled();
 });
 it('reloads the open transcript after a reconnect when the server reports no log epoch',()=>{
  (activeSession.current as {sessionId:string|null}).sessionId='s1';
  m.feed.isFeeding.mockReturnValue(true);
  stop=startLiveSync();account('alice');
  Socket.all[0].emit({type:'sync_ready'});Socket.all[0].emit({type:'sync_ready'});
  expect(m.feed.restart).toHaveBeenCalledExactlyOnceWith('s1');expect(m.feed.revive).not.toHaveBeenCalled();
 });
 it('drops cached transcripts on an owner change and treats the next sync_ready as the first',()=>{
  (activeSession.current as {sessionId:string|null}).sessionId='s1';
  m.feed.isFeeding.mockReturnValue(true);
  stop=startLiveSync();account('alice');Socket.all[0].emit({type:'sync_ready'});
  m.feed.invalidate.mockClear();
  account('bob');
  expect(m.feed.invalidate).toHaveBeenCalledOnce();
  Socket.all[1].emit({type:'sync_ready'});
  expect(m.feed.restart).not.toHaveBeenCalled();expect(m.feed.revive).not.toHaveBeenCalled();
 });
 it('ignores loading auth polls and isolates account changes; late messages cannot update the next user',()=>{
  localStorage.setItem('ai-shuffle-preferences-owner','admin:alice');stop=startLiveSync();account('bob');
  expect(m.start).toHaveBeenCalledWith(true,'admin:bob');const old=Socket.all[0];
  m.listener?.({loading:true});expect(old.close).not.toHaveBeenCalled();
  account(null);expect(old.close).toHaveBeenCalled();expect(m.stop).toHaveBeenLastCalledWith(true);
  m.refresh.mockClear();old.emit({type:'preferences_changed',keys:['theme']});expect(m.refresh).not.toHaveBeenCalled();
  account('carol');expect(Socket.all).toHaveLength(2);expect(m.start).toHaveBeenLastCalledWith(true,'admin:carol');
 });
 it('defers hidden list invalidations and reconciles once on visibility',async()=>{
  Object.defineProperty(document,'hidden',{configurable:true,value:true});
  Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});
  stop=startLiveSync();account('alice');
  for(let i=0;i<20;i++) {
   Socket.all[0].emit({type:'resources_changed',resources:['sessions','projects']});
   await vi.advanceTimersByTimeAsync(100);
  }
  expect(m.loadSessions).not.toHaveBeenCalled();
  expect(m.loadProjectCatalog).not.toHaveBeenCalled();
  Object.defineProperty(document,'hidden',{configurable:true,value:false});
  Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(100);
  expect(m.loadProjectCatalog).toHaveBeenCalledOnce();
  // Both subscriptions enter the central coalescing loadSessions operation.
  expect(m.loadSessions).toHaveBeenCalledTimes(2);
 });
 it('refreshes session project assignments when a project is changed or removed',async()=>{
  stop=startLiveSync();account('alice');
  const subscription=vi.mocked(resourceSync.subscribe).mock.calls.filter(([names])=>names.includes('projects')).at(-1);
  await subscription?.[1]();
  expect(m.loadProjectCatalog).toHaveBeenCalledOnce();expect(m.loadSessions).toHaveBeenCalledOnce();
 });
 it('lets the first catalog refresh join a boot load that started after sync_ready',async()=>{
  stop=startLiveSync();account('alice');
  const before=performance.now();
  Socket.all[0].emit({type:'sync_ready'});
  await vi.advanceTimersByTimeAsync(100);
  expect(m.loadProjectCatalog).toHaveBeenCalledOnce();
  // The refresh names the moment of the invalidation, not of the refresh.
  const [freshSince]=m.loadProjectCatalog.mock.calls[0];
  expect(freshSince).toBeGreaterThanOrEqual(before);
  expect(freshSince).toBeLessThanOrEqual(performance.now());
  m.loadProjectCatalog.mockClear();
  Socket.all[0].emit({type:'resources_changed',resources:['sessions']});
  await vi.advanceTimersByTimeAsync(100);
  expect(m.loadProjectCatalog).not.toHaveBeenCalled();
 });
 it('uses stable server identities for renamed devices and separates roles with equal IDs',()=>{
  stop=startLiveSync();account('alice');m.start.mockClear();
  m.listener?.({loading:false,status:{authenticated:true,username:'Phone',preference_identity:{user_type:'admin',user_id:'alice'}}});
  expect(m.start).not.toHaveBeenCalled();
  m.listener?.({loading:false,status:{authenticated:true,username:'alice',preference_identity:{user_type:'api_user',user_id:'alice'}}});
  expect(m.start).toHaveBeenCalledWith(true,'api_user:alice');
 });
 it('resynchronizes on return from suspension even when the socket still looks open',()=>{
  stop=startLiveSync();account('alice');m.reconnect.mockClear();m.invalidate.mockClear();
  Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
  document.dispatchEvent(new Event('visibilitychange'));
  expect(m.reconnect).toHaveBeenCalledOnce();expect(m.invalidate).toHaveBeenCalledOnce();
  stop();m.reconnect.mockClear();
  document.dispatchEvent(new Event('visibilitychange'));
  expect(m.reconnect).not.toHaveBeenCalled();
 });
 it('reconnects after close, but teardown removes retries and stale callbacks',async()=>{
  stop=startLiveSync();account('alice');Socket.all[0].close();await vi.advanceTimersByTimeAsync(1000);
  expect(Socket.all).toHaveLength(2);Socket.all[1].close();stop();await vi.advanceTimersByTimeAsync(30000);
  expect(Socket.all).toHaveLength(2);expect(m.unsubscribe).toHaveBeenCalled();
 });
});
