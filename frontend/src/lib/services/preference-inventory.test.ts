import { beforeEach, describe, expect, it, vi } from 'vitest';
const bindings = vi.hoisted(() => new Map<string, { read(): unknown; apply(value: unknown): void }>());
const save = vi.hoisted(() => vi.fn());
vi.mock('$lib/services/preferences', () => ({ preferenceSync: { register: (key: string, adapter: {read():unknown;apply(value:unknown):void}) => bindings.set(key, adapter), save, refresh: vi.fn() } }));
// Explicit imports are intentional: this inventory fails if a production binding disappears.
async function stores() {
 return [
  await import('$lib/stores/theme.svelte'), await import('$lib/stores/timezonePreference.svelte'),
  await import('$lib/stores/chatPreferences.svelte'), await import('$lib/stores/hotkeyPreferences.svelte'),
  await import('$lib/stores/gitActionPreferences.svelte'), await import('$lib/stores/modelFavorites.svelte'),
  await import('$lib/stores/files.svelte'), await import('$lib/stores/groups.svelte'),
  await import('$lib/stores/layout.svelte'), await import('$lib/stores/sidebar-prefs.svelte'), await import('$lib/stores/sessionMode.svelte'),
  await import('$lib/stores/browser-panel.svelte'), await import('./notificationSound')
 ] as const;
}
beforeEach(() => { vi.resetModules(); localStorage.clear(); bindings.clear(); save.mockClear();
 Object.defineProperty(window, 'matchMedia', { configurable: true, value: vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) });
});
describe('persistent semantic preference inventory', () => {
 it('registers every semantic preference, never navigation, auth, drafts, or device IDs', async () => {
  const loaded = await stores(); expect(bindings.size).toBe(loaded.length);
  expect([...bindings.keys()].sort()).toEqual(['theme','timezone','chat_behavior','hotkey_settings','git_action_settings','model_favorites','files_preferences','groups','layout','sidebar_workspace','session_mode','notification_sound','browser_panel'].sort());
  expect(Object.keys(bindings.get('files_preferences')!.read() as object).sort()).toEqual(['showHidden','sortAsc','sortBy','viewMode']);
  expect(Object.keys(bindings.get('layout')!.read() as object).sort()).toEqual(['sidebarCollapsed','sidebarWidth']);
 });
 it('remote updates and resets notify reactive subscribers without any write', async () => {
  const [theme,tz,chat,hotkeys,git,favorites,files,groups,layout,sidebar,mode] = await stores();
  const updates: Record<string,unknown> = {
   theme: {preference:'dark',presetId:null}, timezone:'Europe/Paris', chat_behavior:{showMessageTimestamps:true},
   hotkey_settings:{voiceSoundCues:false}, git_action_settings:{nextStep:'pr',futureFeature:42},
   model_favorites:['remote'], files_preferences:{viewMode:'grid'}, groups:{projects:{groups:[{name:'Shared',collapsed:true}],assignments:{}}},
   layout:{sidebarCollapsed:true,sidebarWidth:310}, sidebar_workspace:{version:1,order:['notes'],shown:['notes']}, session_mode:'voice'
  };
  const changed=vi.fn(); const stop=chat.chatPreferences.subscribe(changed);
  for (const [key,value] of Object.entries(updates)) bindings.get(key)!.apply(value);
  expect(theme.theme.current.resolved).toBe('dark'); expect(document.documentElement.classList.contains('dark')).toBe(true);
  expect(tz.timezonePreference.current).toBe('Europe/Paris'); expect(chat.chatPreferences.current.showMessageTimestamps).toBe(true);
  expect(hotkeys.hotkeyPreferences.current.voiceSoundCues).toBe(false); expect((git.gitActionPreferences.current as unknown as Record<string,unknown>).futureFeature).toBe(42);
  expect(favorites.getFavoriteModels()).toEqual(['remote']); expect(files.files.current.viewMode).toBe('grid');
  expect(groups.groups.current.projects.groups[0].name).toBe('Shared'); expect(layout.layout.current.sidebarWidth).toBe(310);
  expect(sidebar.sidebarWorkspace.isShown('notes')).toBe(true); expect(mode.appMode.current).toBe('voice');
  for (const adapter of bindings.values()) adapter.apply(undefined);
  expect(chat.chatPreferences.current.showMessageTimestamps).toBe(false); expect(favorites.getFavoriteModels()).toEqual([]);
  expect(groups.groups.current.projects.groups).toEqual([]); expect(mode.appMode.current).toBe('code');
  expect(changed).toHaveBeenCalledTimes(3); expect(save).not.toHaveBeenCalled(); stop();
 });
 it('keeps overlay/navigation local while remote sidebar preferences are applied', async () => {
  const all=await stores(), layout=all[8].layout;
  layout.openFeaturePage('notes','private-selection'); layout.setSidebarOverlay(true); layout.setSidebarCollapsed(false);
  save.mockClear(); bindings.get('layout')!.apply({sidebarCollapsed:true,sidebarWidth:333});
  expect(layout.current.activeView).toEqual({type:'notes',sessionId:null,itemId:'private-selection'});
  expect(layout.current.sidebarCollapsed).toBe(false); expect(save).not.toHaveBeenCalled();
  layout.setSidebarOverlay(false); expect(layout.current.sidebarCollapsed).toBe(true); expect(save).not.toHaveBeenCalled();
 });
 it('following an existing session lane does not alter the shared default', async () => {
  const all=await stores(), mode=all[10].appMode;
  mode.set('voice'); mode.set('chat',false); save.mockClear();
  bindings.get('session_mode')!.apply('code'); expect(mode.current).toBe('chat');
  expect(bindings.get('session_mode')!.read()).toBe('code'); expect(save).not.toHaveBeenCalled();
 });
});
