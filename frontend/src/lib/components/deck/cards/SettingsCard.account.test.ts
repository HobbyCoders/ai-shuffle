import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() }));
const workspace = vi.hoisted(() => ({ get: vi.fn(), validate: vi.fn(), save: vi.fn() }));
vi.mock('$lib/api/client', () => ({ api: transport }));
vi.mock('$lib/api/auth', () => ({
 getWorkspaceConfig: workspace.get,
 validateWorkspacePath: workspace.validate,
 setWorkspaceConfig: workspace.save,
}));
vi.mock('$lib/stores/auth.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { auth: { checkAuth: vi.fn() }, isAdmin: writable(true) };
});
vi.mock('$lib/stores/providers.svelte', () => ({
 providers: { refreshAll: vi.fn(), refreshProviders: vi.fn() },
}));
vi.mock('$lib/stores/theme.svelte', async () => {
 const { writable } = await import('svelte/store');
 return {
  theme: { setTheme: vi.fn(), setPreset: vi.fn() },
  themePreference: writable('system'), currentPresetId: writable(null), themePresets: [],
 };
});
vi.mock('$lib/stores/chatPreferences.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { chatPreferences: { ...writable(null), load: vi.fn() } };
});
vi.mock('$lib/stores/timezonePreference.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { timezonePreference: { ...writable('local'), load: vi.fn(), setTimezone: vi.fn() } };
});
// Keep Account's real shell, GeneralSection, and (before the fix) SecuritySection.
// Other tabs are outside this regression and must not initialize their stores.
vi.mock('./settings/providers/ProvidersSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/DevicesSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/ChatBehaviorSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/HotkeysSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/VoiceSection.svelte', () => ({ default: () => {} }));
vi.mock('./settings/ApiTab.svelte', () => ({ default: () => {} }));
vi.mock('./settings/SystemTab.svelte', () => ({ default: () => {} }));
vi.mock('./settings/LogsTab.svelte', () => ({ default: () => {} }));
vi.mock('./settings/LicenseSection.svelte', () => ({ default: () => {} }));

import SettingsCard from './SettingsCard.svelte';
import { resourceSync } from '$lib/services/resource-sync';

beforeEach(() => {
 vi.clearAllMocks();
 workspace.get.mockResolvedValue({ workspace_path: '/workspace/projects' });
 workspace.validate.mockResolvedValue({ valid: true, exists: true, writable: true });
 workspace.save.mockResolvedValue({ workspace_path: '/workspace/new-projects' });
 transport.get.mockImplementation(async (path: string) => {
  if (path === '/auth/github/status') return { authenticated: false, user: null };
  if (path === '/settings/security-instructions') return { current: 'Instructions', default: 'Instructions', is_customized: false };
  return [];
 });
});
afterEach(() => { cleanup(); resourceSync.clear(); vi.useRealTimers(); });

async function mountAccount() {
 const view = render(SettingsCard);
 await screen.findByDisplayValue('/workspace/projects');
 return view;
}

describe('Account settings', () => {
 it('opens the License tab when another panel asks for it (remote-access refusal)', async () => {
  // jsdom has no Web Animations; the tab switch fades.
  const proto = Element.prototype as unknown as { animate?: unknown };
  const hadAnimate = 'animate' in proto;
  if (!hadAnimate) proto.animate = () => ({ cancel() {}, finish() {}, onfinish: null, finished: Promise.resolve() });
  await mountAccount();
  const nav = screen.getByRole('navigation', { name: 'Settings sections' });
  const license = within(nav).getByRole('button', { name: 'License' });
  expect(license.classList.contains('on')).toBe(false);
  const { openSettingsTab } = await import('$lib/api/account');
  openSettingsTab('not-a-tab');
  await Promise.resolve();
  expect(within(nav).getByRole('button', { name: 'Account' }).classList.contains('on')).toBe(true);
  openSettingsTab('license');
  await waitFor(() => expect(license.classList.contains('on')).toBe(true));
  if (!hadAnimate) delete proto.animate;
 });

 it('opens the tab a deep link names, then clears the link (Assistant "Sign in to GitHub")', async () => {
  const proto = Element.prototype as unknown as { animate?: unknown };
  const hadAnimate = 'animate' in proto;
  if (!hadAnimate) proto.animate = () => ({ cancel() {}, finish() {}, onfinish: null, finished: Promise.resolve() });
  onTestFinished(() => { if (!hadAnimate) delete proto.animate; });
  const { layout } = await import('$lib/stores/layout.svelte');
  const clear = vi.spyOn(layout, 'clearFeatureItemId');
  render(SettingsCard, { initialTab: 'providers' });
  const nav = await screen.findByRole('navigation', { name: 'Settings sections' });
  await waitFor(() => expect(within(nav).getByRole('button', { name: 'Providers' }).classList.contains('on')).toBe(true));
  expect(clear).toHaveBeenCalled();
  cleanup();
  render(SettingsCard, { initialTab: 'not-a-tab' });
  const again = await screen.findByRole('navigation', { name: 'Settings sections' });
  expect(within(again).getByRole('button', { name: 'Account' }).classList.contains('on')).toBe(true);
 });

 it('omits Downloads; Plugins is its own section, not an Open Plugins shortcut (#1610)', async () => {
  render(SettingsCard);
  await screen.findByDisplayValue('/workspace/projects');
  expect(screen.queryByRole('button', { name: 'Downloads' })).toBeNull();
  expect(screen.queryByText('Open Plugins')).toBeNull();
  for (const name of ['Account', 'Chat & Input', 'Voice', 'Providers', 'Plugins', 'API', 'System', 'Logs']) {
   expect(within(screen.getByRole('navigation', { name: 'Settings sections' })).getByRole('button', { name })).toBeTruthy();
  }
 });

 it('omits the entire account and remote access group while keeping appearance and workspace controls', async () => {
  await mountAccount();
  expect(screen.queryByText('Account and remote access')).toBeNull();
  expect(screen.queryByText('Use Shuffle on your local network without an account.')).toBeNull();
  expect(screen.queryByText('Connect from anywhere')).toBeNull();
  expect(screen.queryByText('Link this worker to your Shuffle account in Devices to enable secure remote access.')).toBeNull();
  expect(screen.getByRole('heading', { name: 'Appearance' })).toBeTruthy();
  expect(screen.getByRole('heading', { name: 'Workspace' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Light' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Validate' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
 });

 it('does not fetch unused agent or project lists', async () => {
  await mountAccount();
  expect(transport.get.mock.calls.map(([path]) => path)).not.toContain('/agents');
  expect(transport.get.mock.calls.map(([path]) => path)).not.toContain('/projects');
 });

 it('refreshes settings but not unrelated agent or project changes', async () => {
  await mountAccount();
  transport.get.mockClear();
  workspace.get.mockClear();
  vi.useFakeTimers();
  resourceSync.invalidate(['agents', 'projects']);
  await vi.runAllTimersAsync();
  expect(transport.get).not.toHaveBeenCalled();
  expect(workspace.get).not.toHaveBeenCalled();
  vi.useRealTimers();
  workspace.get.mockResolvedValue({ workspace_path: '/workspace/remote' });
  resourceSync.invalidate(['settings']);
  await screen.findByDisplayValue('/workspace/remote');
  expect(workspace.get).toHaveBeenCalledOnce();
 });

 it('still validates and saves workspace changes', async () => {
  await mountAccount();
  await fireEvent.input(screen.getByDisplayValue('/workspace/projects'), { target: { value: '/workspace/new-projects' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Validate' }));
  expect(workspace.validate).toHaveBeenCalledExactlyOnceWith('/workspace/new-projects');
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(workspace.save).toHaveBeenCalledExactlyOnceWith('/workspace/new-projects'));
 });
});
