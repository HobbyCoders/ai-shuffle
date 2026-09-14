import { auth } from '$lib/stores/auth.svelte';
import { wsUrl } from '$lib/api/runtime';
import '$lib/stores/theme.svelte';
import '$lib/stores/timezonePreference.svelte';
import '$lib/stores/chatPreferences.svelte';
import '$lib/stores/hotkeyPreferences.svelte';
import '$lib/stores/gitActionPreferences.svelte';
import '$lib/stores/modelFavorites.svelte';
import '$lib/stores/files.svelte';
import '$lib/stores/groups.svelte';
import '$lib/stores/layout.svelte';
import '$lib/stores/sidebar-prefs.svelte';
import '$lib/stores/expansion-preferences';
import '$lib/stores/build-preference.svelte';
import '$lib/stores/install-prompt-preference.svelte';
import '$lib/stores/sessionMode.svelte';
import '$lib/stores/browser-panel.svelte';
import './notificationSound';
import { getOrCreateDeviceId } from '$lib/stores/device-id';
import { bindSessionOwner } from '$lib/stores/session-owner';
import { preferenceSync } from './preferences';
import { resourceSync } from './resource-sync';
import { resetUserEventDedupe } from './user-event-dedupe';
import { handleGlobalUserEvent, activeSession } from '$lib/stores/active-session';
import { appData } from '$lib/stores/app-data.svelte';
import { hydrateModelRegistry } from '$lib/constants/models';
import { notify } from '$lib/stores/notifications';
import { feedController } from '$lib/stores/session/v2/feed-controller';

/** Root-layout lifetime, not chat lifetime. Cookie authentication also supports
 * HttpOnly sessions; no token is copied to storage or put into diagnostics. */
export function startLiveSync() {
 let owner: string | null | undefined;
 let socket: WebSocket | null = null;
 let retry: ReturnType<typeof setTimeout> | undefined;
 let heartbeat: ReturnType<typeof setInterval> | undefined;
 let generation = 0, attempts = 0;
 const unsubscribeResources = [
  resourceSync.subscribe(['sessions', 'tags'], () => appData.loadSessions()),
  resourceSync.subscribe(['projects'], async () => {
   await Promise.all([appData.loadProjects(), appData.loadClusterProjects(), appData.loadSessions()]);
   activeSession.applyComposerDefaults();
  }),
  resourceSync.subscribe(['agents'], async () => {
   await appData.loadProfiles();
   activeSession.applyComposerDefaults();
  }),
  resourceSync.subscribe(['settings'], async () => {
   await hydrateModelRegistry();
   activeSession.applyComposerDefaults();
  }),
  resourceSync.subscribe(['notifications'], () => notify.loadHistory())
 ];
 function disconnect(reset: boolean) {
  generation++; if (retry) clearTimeout(retry); if (heartbeat) clearInterval(heartbeat);
  retry = undefined; heartbeat = undefined;
  const old = socket; socket = null; old?.close();
  preferenceSync.stop(reset); resourceSync.clear(); resetUserEventDedupe();
  if (reset) notify.resetLocal();
 }
 function connect() {
  if (!owner) return;
  const epoch = generation;
  const ws = new WebSocket(wsUrl('/api/v1/ws/global', { device_id: getOrCreateDeviceId() }));
  socket = ws;
  let lastMessage = Date.now();
  ws.onmessage = event => {
   if (epoch !== generation || socket !== ws) return;
   lastMessage = Date.now();
   let data: Record<string, unknown>;
   try { data = JSON.parse(event.data); } catch { return; }
   if (data.type === 'sync_ready') {
    attempts = 0;
    void preferenceSync.reconnect();
    feedController.invalidate();
    const selected = activeSession.current.sessionId;
    if (selected && feedController.isFeeding(selected)) feedController.restart(selected);
    resourceSync.invalidate();
   } else if (data.type === 'sync_reconcile') {
    void preferenceSync.reconnect();
    resourceSync.invalidate();
   } else if (data.type === 'chat_access_changed' && typeof data.session_id === 'string') {
    resourceSync.invalidate([`chat-access:${data.session_id}`]);
   } else if (data.type === 'preferences_changed' && Array.isArray(data.keys)) {
    for (const key of new Set(data.keys)) if (typeof key === 'string') void preferenceSync.refresh(key);
   } else if (data.type === 'resources_changed' && Array.isArray(data.resources)) {
    resourceSync.invalidate(data.resources.filter((v): v is string => typeof v === 'string'));
   } else handleGlobalUserEvent(data);
  };
  ws.onopen = () => {
   if (epoch !== generation) return;
   heartbeat = setInterval(() => {
    if (Date.now() - lastMessage > 90000) ws.close();
    else if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
   }, 25000);
  };
  ws.onclose = event => {
   if (epoch !== generation || socket !== ws) return;
   socket = null;
   if (heartbeat) clearInterval(heartbeat);
   if (event.code === 4001 || event.code === 1008) { void auth.checkAuth().catch(() => {}); return; }
   retry = setTimeout(connect, Math.min(30000, 1000 * 2 ** attempts++));
  };
  ws.onerror = () => ws.close();
 }
 const unsubscribeAuth = auth.subscribe(state => {
  if (state.loading) return;
  const identity = state.status?.preference_identity;
  const next = !state.status?.authenticated ? null : identity
   ? `${identity.user_type}:${identity.user_id}`
   : state.status.is_admin && state.status.username ? `admin:${state.status.username}` : null;
  if (next === owner) return;
  let cachedOwner: string | null = null;
  try { cachedOwner = localStorage.getItem('ai-shuffle-preferences-owner'); } catch { /* no cache */ }
  // Retire old editors and restores before resetting adapters can notify them.
  if (bindSessionOwner(owner, next, cachedOwner)) activeSession.resetForAccountChange();
  const previous = owner; disconnect(!!previous); owner = next;
  if (!owner) return;
  preferenceSync.start(!!cachedOwner && cachedOwner !== owner, owner);
  try { localStorage.setItem('ai-shuffle-preferences-owner', owner); } catch { /* no cache */ }
  void preferenceSync.reconnect();
  connect();
 });
 const reconnect = () => { if (owner && (!socket || socket.readyState === WebSocket.CLOSED)) { if (retry) clearTimeout(retry); connect(); } };
 const resume = () => {
  if (!owner || document.visibilityState !== 'visible') return;
  reconnect();
  void preferenceSync.reconnect();
  resourceSync.invalidate();
 };
 window.addEventListener('online', resume);
 document.addEventListener('visibilitychange', resume);
 return () => {
  unsubscribeAuth(); unsubscribeResources.forEach(unsubscribe => unsubscribe());
  window.removeEventListener('online', resume);
  document.removeEventListener('visibilitychange', resume);
  disconnect(false);
 };
}
