import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

// No real API/provider request leaves this fixture. Only the component code is production.
let preferences: Record<string, unknown> = {};
const versions: Record<string, string> = {};
let writes = 0;
window.fetch = async (input, init) => {
 const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href);
 let body: unknown = {};
 let status = 200;
 if (url.pathname.endsWith('/preferences/all')) body = { preferences, versions };
 else if (url.pathname.includes('/preferences/')) {
  const key = decodeURIComponent(url.pathname.split('/').at(-1)!);
  if (init?.method === 'PUT') {
   const request = JSON.parse(String(init.body));
   preferences[key] = request.value; versions[key] = `fixture-${++writes}`;
  }
  if (key in preferences) body = { value: preferences[key], version: versions[key] ?? null }; else status = 404;
 } else if (url.pathname.endsWith('/profiles') || url.pathname.endsWith('/projects')) body = [];
 else if (url.pathname.includes('/hooks')) body = [];
 else if (url.pathname.includes('/plugins')) body = { plugins: [], installed: [], marketplaces: [] };
 else if (url.pathname.includes('/mcp')) body = { servers: [], config: { mcpServers: {} } };
 else if (url.pathname.includes('/models')) body = { models: [] };
 else if (url.pathname.includes('/notifications')) body = { notifications: [], total: 0 };
 else if (url.pathname.includes('/context-usage')) body = { available: false, reason: 'fixture' };
 return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
};
class LocalSocket {
 static OPEN = 1; static CLOSED = 3; readyState = 1;
 send() {} close() {} addEventListener() {} removeEventListener() {}
}
window.WebSocket = LocalSocket as unknown as typeof WebSocket;
window.EventSource = LocalSocket as unknown as typeof EventSource;

// Enter the session/app-data cycle through the same owner as the app shell.
await import('../src/lib/stores/active-session');
const [{ preferenceSync }, { appData }, { pwa }, { bindSessionOwner }, { default: Fixture }] = await Promise.all([
 import('../src/lib/services/preferences'), import('../src/lib/stores/app-data.svelte'),
 import('../src/lib/stores/pwa.svelte'), import('../src/lib/stores/session-owner'),
 import('./PreferencePresentationFixture.svelte')
]);
Object.assign(appData.current, { sessions: [{ id: 'fixture-session', title: 'Fixture conversation',
 mode: 'code', status: 'idle', is_active: false, created_at: new Date().toISOString(), project_id: null }], projects: [], clusterProjects: [] });
preferenceSync.start(true, 'fixture-a');
await preferenceSync.reconnect();
mount(Fixture, { target: document.getElementById('app')!, props: {
 remote: async () => {
  Object.assign(preferences, { context_breakdown_open: false, context_usage_sections: { plan: false },
   sidebar_collapsed_groups: { settled: true }, build_subtab: 'hooks', install_prompt: { never: true, dismissedAt: null } });
  const before = writes;
  for (const key of ['context_breakdown_open', 'context_usage_sections', 'sidebar_collapsed_groups', 'build_subtab', 'install_prompt'])
   await preferenceSync.refresh(key);
  return `Other-device values applied; local writes: ${writes - before}.`;
 },
 reset: async () => {
  bindSessionOwner('fixture-a', 'fixture-b'); preferences = {};
  preferenceSync.start(true, 'fixture-b'); await preferenceSync.reconnect();
  return 'Fresh account defaults applied; previous account choices cleared.';
 },
 install: () => {
  pwa.init();
  const event = new Event('beforeinstallprompt', { cancelable: true });
  Object.assign(event, { prompt: async () => { throw new Error('Native installation is outside this fixture'); },
   userChoice: Promise.resolve({ outcome: 'dismissed', platform: 'fixture' }) });
  window.dispatchEvent(event);
 }
} });
