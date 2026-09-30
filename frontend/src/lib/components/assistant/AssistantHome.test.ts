import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import type { AssistantState, AssistantSource } from '$lib/api/assistant';

const mocks = vi.hoisted(() => ({
 getAssistant: vi.fn(), getAgentIdentities: vi.fn(), configureAssistant: vi.fn(), openAssistant: vi.fn(),
 pauseAssistant: vi.fn(), resumeAssistant: vi.fn(), getAssistantSources: vi.fn(), getAssistantActivity: vi.fn(), setAssistantSource: vi.fn(),
 openSession: vi.fn(), startCallForSession: vi.fn(), isVoiceEnabled: vi.fn(), openFeaturePage: vi.fn(),
 loadSessions: vi.fn(), loadProfiles: vi.fn(),
}));
vi.mock('$lib/api/assistant', async original => ({ ...await original<typeof import('$lib/api/assistant')>(), ...mocks }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession }));
vi.mock('$lib/services/voiceCall', () => ({ startCallForSession: mocks.startCallForSession }));
vi.mock('$lib/api/voice', () => ({ isVoiceEnabled: mocks.isVoiceEnabled }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openFeaturePage: mocks.openFeaturePage } }));
vi.mock('$lib/stores/app-data.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { profiles: writable([{ id: 'guide', name: 'Guide', is_builtin: false, config: { system_prompt: { content: 'KEEP ME' }, provider_models: { native: 'keep' } } }, { id: 'builtin', name: 'Built-in', is_builtin: true }]), projects: writable([{ id: 'project-1', name: 'My project' }]), appData: { loadSessions: mocks.loadSessions, loadProfiles: mocks.loadProfiles } };
});
import AssistantHome from './AssistantHome.svelte';
import { assistantState, agentIdentities } from '$lib/stores/assistant';
const initial = (): AssistantState => ({
 configured: false, agent_id: null, home_session_id: null, enabled: false, paused: false, status: 'unconfigured', agent: null,
 assistant_name: null, avatar_id: 'robot', avatar_presets: ['robot', 'spark', 'orbit', 'sprout', 'pixel'], introduction: {},
 proactive_guard_ready: true, cost_accounting: 'reported_after_run_not_hard_cap',
 max_runs_per_day: 12, max_concurrent_runs: 1, max_turns: 3, timeout_seconds: 180, max_cost_per_run: 1, max_cost_per_day: 5,
 usage: { runs_today: 0, in_flight_runs: 0, reported_cost_today: 0 },
});
let server: AssistantState;
const configured = (patch: Partial<AssistantState> = {}) => {
 server = { ...initial(), configured: true, agent_id: 'guide', home_session_id: 'home', assistant_name: 'Guide', avatar_id: 'orbit', agent: { id: 'guide', name: 'Guide', avatar_id: 'orbit' }, status: 'disabled', ...patch };
};
const source: AssistantSource = { id: 'connected', connector_id: 'connected', name: 'My authorized inbox', type: 'gmail', enabled: false, available: true, status: 'connected', trigger_ids: ['new_email'], observation_mode: 'poll_and_events', last_observed_at: null, last_error: null };
beforeEach(() => {
 vi.clearAllMocks(); server = initial(); assistantState.set(null); agentIdentities.set([]);
 mocks.getAssistant.mockImplementation(async () => structuredClone(server));
 mocks.getAgentIdentities.mockResolvedValue([{ id: 'guide', name: 'Guide', avatar_id: 'orbit' }]);
 mocks.getAssistantSources.mockResolvedValue([]); mocks.getAssistantActivity.mockResolvedValue([]);
 mocks.loadSessions.mockResolvedValue(undefined); mocks.loadProfiles.mockResolvedValue(undefined);
 mocks.isVoiceEnabled.mockResolvedValue(true); mocks.openAssistant.mockResolvedValue({ session_id: 'home', home_session_id: 'home' });
 mocks.configureAssistant.mockImplementation(async patch => { server = { ...server, ...patch, configured: true, agent: { id: patch.agent_id, name: patch.assistant_name || 'Guide', avatar_id: patch.avatar_id || 'robot' }, assistant_name: patch.assistant_name || 'Guide', status: patch.enabled ? 'ready' : 'disabled' }; return structuredClone(server); });
 mocks.pauseAssistant.mockImplementation(async () => { server = { ...server, paused: true, status: 'paused' }; return server; });
 mocks.resumeAssistant.mockImplementation(async () => { server = { ...server, enabled: true, paused: false, status: 'ready' }; return server; });
 mocks.setAssistantSource.mockImplementation(async (_id, enabled) => ({ ...source, enabled }));
});
afterEach(cleanup);

async function choose() {
 const select = await screen.findByRole('combobox', { name: 'Assistant agent' });
 await fireEvent.change(select, { target: { value: 'guide' } });
}
describe('Assistant home', () => {
 it('starts unconfigured with no ambient call, automatic activation or invented sources', async () => {
  render(AssistantHome); await choose();
  expect((screen.getByRole('checkbox', { name: /Enable background research/ }) as HTMLInputElement).checked).toBe(false);
  expect(screen.getByText('No sources connected')).toBeTruthy();
  expect(mocks.configureAssistant).not.toHaveBeenCalled(); expect(mocks.startCallForSession).not.toHaveBeenCalled();
  expect(mocks.openAssistant).not.toHaveBeenCalled();
 });
 it('saves only explicit identity, introduction and finite policy; does not send agent config', async () => {
  render(AssistantHome); await choose();
  await fireEvent.input(screen.getByRole('textbox', { name: 'Assistant name' }), { target: { value: 'Dotty' } });
  await fireEvent.input(screen.getByRole('textbox', { name: 'What should I call you?' }), { target: { value: 'Alex' } });
  await fireEvent.input(screen.getByRole('textbox', { name: 'Repositories you care about' }), { target: { value: 'owner/one\nowner/two' } });
  await fireEvent.click(screen.getByRole('button', { name: 'spark avatar' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save assistant' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalledTimes(1));
  const patch = mocks.configureAssistant.mock.calls[0][0];
  expect(patch).toMatchObject({ agent_id: 'guide', assistant_name: 'Dotty', avatar_id: 'spark', enabled: false, paused: true, max_runs_per_day: 12, max_concurrent_runs: 1, timeout_seconds: 180 });
  expect(patch.introduction).toMatchObject({ preferred_name: 'Alex', github_username: '', repository_refs: ['owner/one', 'owner/two'] });
  expect(patch).not.toHaveProperty('config'); expect(patch).not.toHaveProperty('model');
  expect(mocks.setAssistantSource).not.toHaveBeenCalled();
 });
 it('requires an explicit activation choice and keeps context separate from source access', async () => {
  render(AssistantHome); await choose();
  await fireEvent.click(screen.getByRole('checkbox', { name: /Enable background research/ }));
  await fireEvent.click(screen.getByRole('checkbox', { name: 'My project' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Save & enable research' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalled());
  expect(mocks.configureAssistant.mock.calls[0][0]).toMatchObject({ enabled: true, paused: false, introduction: { project_ids: ['project-1'] } });
  expect(mocks.setAssistantSource).not.toHaveBeenCalled();
 });
 it('reopens the same home and attaches a call only on a deliberate call action', async () => {
  configured(); render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Open conversation' }));
  await waitFor(() => expect(mocks.openSession).toHaveBeenCalledWith('home'));
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Start call' }));
  await waitFor(() => expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true }));
  expect(mocks.openAssistant).toHaveBeenCalledTimes(2);
 });
 it('only offers sources from the backend catalog and lets an unavailable opted-in source be revoked', async () => {
  configured(); mocks.getAssistantSources.mockResolvedValue([source, { ...source, id: 'revoked', connector_id: 'revoked', name: 'Revoked connection', available: false, enabled: true }]);
  render(AssistantHome); const toggle = await screen.findByRole('switch', { name: 'Observe My authorized inbox' });
  expect(screen.getByRole('heading', { name: 'Opted-in sources 1' })).toBeTruthy();
  await fireEvent.click(toggle); expect(mocks.setAssistantSource).toHaveBeenCalledWith('connected', true);
  await waitFor(() => expect(toggle.getAttribute('aria-checked')).toBe('true'));
  await fireEvent.click(screen.getByRole('switch', { name: 'Observe Revoked connection' }));
  expect(mocks.setAssistantSource).toHaveBeenCalledWith('revoked', false);
 });
 it('pauses and resumes background work without cancelling or replacing the home', async () => {
  configured({ enabled: true, status: 'ready' }); render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Pause background' }));
  await waitFor(() => expect(mocks.pauseAssistant).toHaveBeenCalled());
  const resume = await screen.findByRole('button', { name: 'Resume background' });
  await waitFor(() => expect((resume as HTMLButtonElement).disabled).toBe(false));
  await fireEvent.click(resume);
  await waitFor(() => expect(mocks.resumeAssistant).toHaveBeenCalled());
  expect(mocks.openAssistant).not.toHaveBeenCalled();
 });
 it('shows linked sessions and real reported usage, with a non-hard-cap explanation', async () => {
  configured({ usage: { runs_today: 2, reported_cost_today: .42, in_flight_runs: 1 } });
  mocks.getAssistantActivity.mockResolvedValue([{ id: 'run', kind: 'linked', status: 'completed', session_id: 'task', session_title: 'Repository review', agent_name: 'Reviewer', avatar_id: 'pixel', source_ids: [], event_ids: [], cost_usd: .42, error: null, created_at: '2026-09-30T12:00:00Z', ended_at: null }]);
  render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Repository review' }));
  expect(mocks.openSession).toHaveBeenCalledWith('task');
  expect(screen.getByText(/not a hard billing cap/)).toBeTruthy();
 });
 it('does not erase an introduction draft when background state changes', async () => {
  configured({ introduction: { preferred_name: 'Original', repository_refs: ['owner/repo'] } }); render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Edit assistant and introduction' }));
  const name = screen.getByRole('textbox', { name: 'What should I call you?' }) as HTMLInputElement;
  await fireEvent.input(name, { target: { value: 'Editing' } });
  assistantState.update(value => value && { ...value, usage: { ...value.usage, runs_today: 3 } });
  expect(name.value).toBe('Editing');
  expect((screen.getByRole('textbox', { name: 'Repositories you care about' }) as HTMLTextAreaElement).value).toBe('owner/repo');
 });
 it('keeps save errors visible with the editable draft intact', async () => {
  mocks.configureAssistant.mockRejectedValue({ detail: 'Agent is unavailable' }); render(AssistantHome); await choose();
  await fireEvent.click(screen.getByRole('button', { name: 'Save assistant' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Agent is unavailable'));
  expect((screen.getByRole('combobox', { name: 'Assistant agent' }) as HTMLSelectElement).value).toBe('guide');
 });
 it('does not send presentation mutations for a built-in agent', async () => {
  render(AssistantHome); await screen.findByRole('combobox', { name: 'Assistant agent' });
  await fireEvent.change(screen.getByRole('combobox', { name: 'Assistant agent' }), { target: { value: 'builtin' } });
  expect((screen.getByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement).disabled).toBe(true);
  await fireEvent.click(screen.getByRole('button', { name: 'Save assistant' }));
  const patch = mocks.configureAssistant.mock.calls[0][0];
  expect(patch.agent_id).toBe('builtin'); expect(patch).not.toHaveProperty('assistant_name'); expect(patch).not.toHaveProperty('avatar_id');
 });
 it('preselects the real agent when entered through its normal editor', async () => {
  configured(); render(AssistantHome, { requestedAgentId: 'guide' });
  const selection = await screen.findByRole('combobox', { name: 'Assistant agent' });
  expect((selection as HTMLSelectElement).value).toBe('guide');
  expect(get(assistantState)?.agent_id).toBe('guide');
 });
});
