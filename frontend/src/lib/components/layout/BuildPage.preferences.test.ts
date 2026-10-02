import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/svelte';
import { flushSync } from 'svelte';
import { writable } from 'svelte/store';
import BuildPreferencePanel from '$lib/test-stubs/BuildPreferencePanel.svelte';
const mocks = vi.hoisted(() => ({ adapters: new Map<string, {apply(value: unknown): void; read(): unknown}>(), save: vi.fn() }));
vi.mock('$lib/services/preferences', () => ({ preferenceSync: { register: (key: string, adapter: any) => mocks.adapters.set(key, adapter), save: mocks.save } }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: {loadProfiles:vi.fn()}, profiles:writable([]) }));
vi.mock('$lib/stores/mcp.svelte', () => ({ mcp: {loadGlobalServers:vi.fn(async () => {})}, globalMcpServers:writable([]) }));
vi.mock('$lib/stores/plugins.svelte', () => ({ plugins: {loadInstalled:vi.fn()}, installedPlugins:writable([]) }));
vi.mock('$lib/stores/hooks.svelte', () => ({ hooks: {loadAppHooks:vi.fn(async () => {})}, projectHooks:writable([]) }));
vi.mock('$lib/components/deck/lazy/lazy-component', () => ({ createLazyComponent: () => ({preload:vi.fn()}) }));
vi.mock('$lib/components/deck/lazy/LazyPanel.svelte', () => ({default:BuildPreferencePanel}));
import BuildPage from './BuildPage.svelte';
import { buildPreference } from '$lib/stores/build-preference.svelte';
beforeEach(() => { for (const adapter of mocks.adapters.values()) adapter.apply(undefined); mocks.save.mockClear(); localStorage.clear(); });
afterEach(cleanup);

it('uses a startup snapshot, follows remote updates/reset, and saves explicit tab clicks', async () => {
 const adapter = mocks.adapters.get('build_subtab')!;
 adapter.apply('mcp');
 const page = render(BuildPage);
 expect(page.getByRole('button',{name:'MCP Servers'}).getAttribute('aria-current')).toBe('page');
 flushSync(() => adapter.apply('hooks'));
 expect(page.getByRole('button',{name:'Hooks'}).getAttribute('aria-current')).toBe('page');
 expect(mocks.save).not.toHaveBeenCalled();
 await fireEvent.click(page.getByRole('button',{name:'MCP Servers'}));
 expect(buildPreference.current).toBe('mcp');
 expect(mocks.save).toHaveBeenLastCalledWith('build_subtab','mcp');
 mocks.save.mockClear();
 flushSync(() => adapter.apply(undefined));
 expect(page.getByRole('button',{name:'Agents'}).getAttribute('aria-current')).toBe('page');
 expect(mocks.save).not.toHaveBeenCalled();
});

it('has no Plugins tab, and a remembered Plugins tab falls back to Agents (plugins moved to Settings)', () => {
 mocks.adapters.get('build_subtab')!.apply('plugins');
 expect(buildPreference.current).toBe('profile');
 const page = render(BuildPage);
 expect(page.queryByRole('button',{name:'Plugins'})).toBeNull();
 expect(page.getByRole('button',{name:'Agents'}).getAttribute('aria-current')).toBe('page');
});

it('keeps unsaved editor contents mounted while another device changes the remembered tab', async () => {
 const page = render(BuildPage);
 await fireEvent.input(page.getByRole('textbox'), {target:{value:'keep this unsaved agent'}});
 await fireEvent.click(page.getByRole('button',{name:'Open editor'}));
 flushSync(() => mocks.adapters.get('build_subtab')!.apply('hooks'));
 expect((page.getByRole('textbox') as HTMLTextAreaElement).value).toBe('keep this unsaved agent');
 expect(page.getByRole('region',{name:'Agents panel'})).toBeTruthy();
 await fireEvent.click(page.getByRole('button',{name:'Close editor'}));
 expect(page.getByRole('region',{name:'Hooks panel'})).toBeTruthy();
 expect(mocks.save).not.toHaveBeenCalled();
});

it('honors an explicit editor deep link over a remote remembered tab', async () => {
 const page = render(BuildPage, {requestedSub:'profile',requestedItemId:'agent-123'});
 flushSync(() => mocks.adapters.get('build_subtab')!.apply('hooks'));
 expect(page.getByRole('region',{name:'Agents panel'})).toBeTruthy();
 expect(mocks.save).toHaveBeenCalledExactlyOnceWith('build_subtab','profile');
});
