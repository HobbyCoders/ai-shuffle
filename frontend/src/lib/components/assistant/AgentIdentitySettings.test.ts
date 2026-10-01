import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
const mocks = vi.hoisted(() => ({ getAssistant: vi.fn(), getAgentIdentities: vi.fn(), updateAgentIdentity: vi.fn(), openFeaturePage: vi.fn() }));
vi.mock('$lib/api/assistant', async original => ({ ...await original<typeof import('$lib/api/assistant')>(), ...mocks }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openFeaturePage: mocks.openFeaturePage } }));
import AgentIdentitySettings from './AgentIdentitySettings.svelte';
import { assistantState, agentIdentities } from '$lib/stores/assistant';
beforeEach(() => {
 vi.clearAllMocks(); assistantState.set(null); agentIdentities.set([]);
 mocks.getAssistant.mockResolvedValue(null); mocks.getAgentIdentities.mockResolvedValue([{ id: 'agent', name: 'Researcher', avatar_id: 'robot' }]);
 mocks.updateAgentIdentity.mockImplementation(async (id, patch) => ({ id, name: 'Researcher', ...patch }));
});
afterEach(cleanup);
describe('unified agent identity controls', () => {
 it('changes presentation with a narrow identity patch, not a whole-agent save', async () => {
  render(AgentIdentitySettings, { agentId: 'agent' });
  await fireEvent.click(await screen.findByRole('button', { name: 'sprout avatar' }));
  await waitFor(() => expect(mocks.updateAgentIdentity).toHaveBeenCalledWith('agent', { avatar_id: 'sprout' }));
  expect(await screen.findByRole('status')).toHaveProperty('textContent', expect.stringContaining('Saved'));
  expect(screen.getByRole('button', { name: 'sprout avatar' }).getAttribute('aria-pressed')).toBe('true');
 });
 it('opens deliberate assistant setup without designating or activating anything immediately', async () => {
  render(AgentIdentitySettings, { agentId: 'agent' });
  await fireEvent.click(screen.getByRole('button', { name: 'Set up as assistant' }));
  expect(mocks.openFeaturePage).toHaveBeenCalledWith('assistant', 'agent');
  expect(mocks.updateAgentIdentity).not.toHaveBeenCalled();
 });
 it('keeps built-in avatars read-only', async () => {
  render(AgentIdentitySettings, { agentId: 'agent', builtin: true });
  const button = await screen.findByRole('button', { name: 'spark avatar' });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  await fireEvent.click(button); expect(mocks.updateAgentIdentity).not.toHaveBeenCalled();
 });
 it('displays a recoverable save failure without claiming success', async () => {
  mocks.updateAgentIdentity.mockRejectedValue({ detail: 'Could not update identity' });
  render(AgentIdentitySettings, { agentId: 'agent' });
  await fireEvent.click(await screen.findByRole('button', { name: 'pixel avatar' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Could not update identity'));
  expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
 });
});
