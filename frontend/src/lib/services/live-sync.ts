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
import '$lib/stores/install-prompt-preference.svelte';
import '$lib/stores/sessionMode.svelte';
import '$lib/stores/browser-panel.svelte';
import { getOrCreateDeviceId } from '$lib/stores/device-id';
import { bindSessionOwner } from '$lib/stores/session-owner';
import { promptStash } from '$lib/stores/session/prompt-stash';
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
 // The server sends sync_ready on every /ws/global connection. Only a LATER one
 // follows a gap in which broadcasts (a clear, a delete) may have been missed;
 // the first one after start or an owner change has nothing to catch up on.
 let syncedBefore = false;
 // Keep lifecycle/permission delivery live; defer presentation-only catalogue
 // reads until resume invalidates the full set once. In-flight dirty work stays
 // coalesced by loadSessions/resourceSync, not queued per background event.
 const visible = (refresh: () => void | Promise<unknown>) => () => {
  if (!document.hidden) return refresh();
 };
 // When the project catalog was last invalidated: a catalog request that
 // started after it (the boot load, at startup) already answers it.
 let projectsInvalidatedAt = 0;
 const invalidate = (names?: string[]) => {
  if (!names || names.includes('projects')) projectsInvalidatedAt = performance.now();
  resourceSync.invalidate(names);
 };
 const unsubscribeResources = [
  resourceSync.subscribe(['sessions', 'tags'], visible(() => appData.loadSessions())),
  resourceSync.subscribe(['projects'], visible(async () => {
   await Promise.all([appData.loadProjectCatalog(projectsInvalidatedAt), appData.loadSessions()]);
   activeSession.applyComposerDefaults();
  })),
  resourceSync.subscribe(['agents'], visible(async () => {
   await appData.loadProfiles();
   activeSession.applyComposerDefaults();
  })),
  resourceSync.subscribe(['settings'], visible(async () => {
   await hydrateModelRegistry();
   activeSession.applyComposerDefaults();
  })),
  resourceSync.subscribe(['notifications'], visible(() => notify.loadHistory()))
 ];
 function disconnect(reset: boolean) {
  generation++; if (retry) clearTimeout(retry); if (heartbeat) clearInterval(heartbeat);
  retry = undefined; heartbeat = undefined;
  const old = socket; socket = null; old?.close();
  preferenceSync.stop(reset); resourceSync.clear(); resetUserEventDedupe();
  if (reset) notify.resetLocal();
 }
 function resyncFeedAfterGap() {
  // Cached transcripts that carry a server log epoch are checked on resume (a
  // rewritten log answers with a reset), so only unverifiable ones are dropped.
  feedController.invalidateUnverified();
  const selected = activeSession.current.sessionId;
  if (!selected || !feedController.isFeeding(selected)) return;
  // Resume at the cursor when the server can vouch for it; otherwise the only
  // safe answer to a possibly missed clear is a full reload.
  if (feedController.hasLogEpoch(selected)) feedController.revive(selected);
  else feedController.restart(selected);
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
    if (syncedBefore) resyncFeedAfterGap();
    syncedBefore = true;
    invalidate();
   } else if (data.type === 'sync_reconcile') {
    void preferenceSync.reconnect();
    invalidate();
   } else if (data.type === 'chat_access_changed' && typeof data.session_id === 'string') {
    resourceSync.invalidate([`chat-access:${data.session_id}`]);
   } else if (data.type === 'preferences_changed' && Array.isArray(data.keys)) {
    for (const key of new Set(data.keys)) if (typeof key === 'string') void preferenceSync.refresh(key);
   } else if (data.type === 'resources_changed' && Array.isArray(data.resources)) {
    invalidate(data.resources.filter((v): v is string => typeof v === 'string'));
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
  // Stashed prompts belong to the account that stashed them.
  promptStash.setOwner(owner);
  syncedBefore = false;
  // Cached transcripts belong to the account that loaded them.
  feedController.invalidate();
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
  invalidate();
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
