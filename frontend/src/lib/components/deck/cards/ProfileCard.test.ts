import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
 get: vi.fn(), put: vi.fn(), getAssistant: vi.fn(), getAgentIdentities: vi.fn(), updateAgentIdentity: vi.fn(),
 clearFeatureItemId: vi.fn(), openFeaturePage: vi.fn()
}));
vi.mock('$lib/api/client', async original => ({
 ...await original<typeof import('$lib/api/client')>(), api: { get: mocks.get, put: mocks.put }
}));
vi.mock('$lib/api/assistant', async original => ({
 ...await original<typeof import('$lib/api/assistant')>(),
 getAssistant: mocks.getAssistant, getAgentIdentities: mocks.getAgentIdentities, updateAgentIdentity: mocks.updateAgentIdentity
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: () => {} }));
vi.mock('$lib/stores/auth.svelte', async original => ({
 ...await original<typeof import('$lib/stores/auth.svelte')>(), isAdmin: (await import('svelte/store')).writable(true)
}));
vi.mock('$lib/stores/layout.svelte', async original => ({
 ...await original<typeof import('$lib/stores/layout.svelte')>(),
 layout: { clearFeatureItemId: mocks.clearFeatureItemId, openFeaturePage: mocks.openFeaturePage }
}));
vi.mock('$lib/api/toolSurface', async original => ({
 ...await original<typeof import('$lib/api/toolSurface')>(), fetchToolSurface: async () => ({ groups: [], errors: [], discovered_at: '' })
}));

import ProfileCard from './ProfileCard.svelte';
import { appData } from '$lib/stores/app-data.svelte';
import { assistantState, agentIdentities } from '$lib/stores/assistant';

let serverAvatar: string;
const profile = {
 id: 'agent', name: 'Researcher', description: 'Original description', is_builtin: false, is_modified: false,
 created_at: '', updated_at: '',
 config: { avatar_id: 'robot', system_prompt: { type: 'custom', content: 'Keep my original prompt' },
  model: 'original-model', permission_mode: 'default', custom_future_key: { keep: true }, tool_selection_mode: 'all' }
};

beforeEach(async () => {
 vi.clearAllMocks(); serverAvatar = 'robot'; assistantState.set(null); agentIdentities.set([]);
 mocks.get.mockImplementation(async (path: string) => {
  if (path === '/agents') return [{ ...profile, config: { ...profile.config, avatar_id: serverAvatar } }];
  return [];
 });
 mocks.getAssistant.mockResolvedValue(null);
 mocks.getAgentIdentities.mockImplementation(async () => [{ id: 'agent', name: 'Researcher', avatar_id: serverAvatar }]);
 mocks.updateAgentIdentity.mockImplementation(async (id, patch) => {
  serverAvatar = patch.avatar_id; return { id, name: 'Researcher', avatar_id: serverAvatar };
 });
 // Model the normal PUT contract: an omitted identity preserves its latest
 // narrow PATCH, while a stale explicit identity would overwrite it.
 mocks.put.mockImplementation(async (_path, payload) => {
  if (Object.hasOwn(payload.config, 'avatar_id')) serverAvatar = payload.config.avatar_id;
  return { ...profile, ...payload, config: { ...payload.config, avatar_id: serverAvatar } };
 });
 await appData.loadProfiles();
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('ordinary agent autosave and independently edited identity', () => {
 it('omits the stale read-projected avatar after a rendered identity edit', async () => {
  render(ProfileCard, { initialSelectedId: 'agent' });
  await fireEvent.click(await screen.findByRole('button', { name: 'orbit avatar' }));
  await waitFor(() => expect(mocks.updateAgentIdentity).toHaveBeenCalledWith('agent', { avatar_id: 'orbit' }));
  expect(screen.getByRole('button', { name: 'orbit avatar' }).getAttribute('aria-pressed')).toBe('true');
  await fireEvent.input(screen.getByPlaceholderText('A coding assistant for frontend work'), { target: { value: 'Edited description' } });
  await waitFor(() => expect(mocks.put).toHaveBeenCalled(), { timeout: 2000 });
  const [path, payload] = mocks.put.mock.calls.at(-1)!;
  expect(path).toBe('/agents/agent');
  expect(payload.description).toBe('Edited description');
  expect(payload.config).not.toHaveProperty('avatar_id');
  expect(payload.config.system_prompt.content).toBe('Keep my original prompt');
  // Hidden legacy bindings are retired by the full-replace PUT, as before;
  // keys outside the schema are preserved server-side, not echoed back.
  expect(payload.config).not.toHaveProperty('model');
  expect(payload.config).not.toHaveProperty('permission_mode');
  expect(payload.config).not.toHaveProperty('custom_future_key');
  expect(serverAvatar).toBe('orbit');
 });
});
