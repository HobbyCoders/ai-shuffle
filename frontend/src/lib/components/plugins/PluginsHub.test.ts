import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
const calls = vi.hoisted(() => ({ install: vi.fn(), getConnections: vi.fn() }));
vi.mock('$lib/stores/auth.svelte', () => ({ isAdmin: writable(true) }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/api/connectors', () => ({
 getConnectors: calls.getConnections,
 getConnectorTypes: vi.fn(async () => [{type:'gmail',name:'Gmail',description:'Email',config_fields:[],triggers:[],actions:[],tools:[]}])
}));
vi.mock('$lib/api/plugins', () => ({
 getInstalledPlugins: vi.fn(async () => [{id:'writer@local',name:'Writer',description:'Writing skills',enabled:true}]),
 getAvailablePlugins: vi.fn(async () => [{id:'writer@local',name:'Writer',description:'Duplicate'}, {id:'review@local',name:'Review',description:'Code review'}]),
 getPluginDetails: vi.fn(), installPlugin: calls.install
}));
import PluginsHub from './PluginsHub.svelte';
afterEach(cleanup);
beforeEach(() => { calls.getConnections.mockResolvedValue([{id:'work',type:'gmail',name:'Work',is_active:true}]); calls.install.mockReset(); });
it('merges installed apps and packages without duplicate catalog entries', async () => {
 const page = render(PluginsHub);
 await page.findAllByRole('button', {name:'Manage Gmail'});
 expect(page.getAllByRole('article')).toHaveLength(3);
 expect(page.getByText('1 connection')).toBeTruthy();
 await fireEvent.input(page.getByRole('textbox', {name:'Search plugins'}), {target:{value:'gmail'}});
 expect(page.getAllByRole('article')).toHaveLength(1);
 await fireEvent.input(page.getByRole('textbox', {name:'Search plugins'}), {target:{value:'no-such-plugin'}});
 expect(page.getByText('No plugins match your search.')).toBeTruthy();
});
it('filters apps independently of agent plugins', async () => {
 const page = render(PluginsHub);
 await waitFor(() => expect(page.getAllByRole('article')).toHaveLength(3));
 await fireEvent.click(page.getByRole('button', {name:'Apps'}));
 expect(page.getAllByRole('article')).toHaveLength(1);
 await fireEvent.click(page.getByRole('button', {name:'Agent plugins'}));
 expect(page.getAllByRole('article')).toHaveLength(2);
});
it('installs using the existing package id, not a connection id', async () => {
 const page = render(PluginsHub);
 await fireEvent.click(await page.findByRole('button', {name:'Install Review'}));
 expect(calls.install).toHaveBeenCalledWith('review@local');
});
