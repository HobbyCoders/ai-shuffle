import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { AVATARS, type AssistantState, type AssistantSource } from '$lib/api/assistant';
import { resourceSync } from '$lib/services/resource-sync';

const mocks = vi.hoisted(() => ({
 getAssistant: vi.fn(), getAgentIdentities: vi.fn(), configureAssistant: vi.fn(), openAssistant: vi.fn(),
 getAssistantSources: vi.fn(), getAssistantActivity: vi.fn(), setAssistantSource: vi.fn(),
 openSession: vi.fn(), startCallForSession: vi.fn(), isVoiceEnabled: vi.fn(), openFeaturePage: vi.fn(),
 loadSessions: vi.fn(), loadProfiles: vi.fn(), banner: vi.fn(() => 'banner-id'),
}));
vi.mock('$lib/api/assistant', async original => ({ ...await original<typeof import('$lib/api/assistant')>(), ...mocks }));
vi.mock('$lib/services/session-navigation', () => ({ openSession: mocks.openSession }));
vi.mock('$lib/services/voiceCall', () => ({ startCallForSession: mocks.startCallForSession }));
vi.mock('$lib/api/voice', () => ({ isVoiceEnabled: mocks.isVoiceEnabled }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openFeaturePage: mocks.openFeaturePage } }));
vi.mock('$lib/stores/app-data.svelte', async () => {
 const { writable } = await import('svelte/store');
 return { sessions: writable([]), profiles: writable([{ id: 'guide', name: 'Guide', is_builtin: false, config: { system_prompt: { content: 'KEEP ME' }, provider_models: { native: 'keep' } } }, { id: 'builtin', name: 'Built-in', is_builtin: true }]), projects: writable([{ id: 'project-1', name: 'My project' }]), appData: { loadSessions: mocks.loadSessions, loadProfiles: mocks.loadProfiles } };
});
vi.mock('$lib/stores/active-session', async () => {
 const { writable } = await import('svelte/store');
 return {
  activeSession: writable({ sessionId: null, isStreaming: false, activeTasks: [], pendingPermissions: [], pendingQuestions: [] }),
  streamingSessions: writable(new Map()), backgroundPermissions: writable(new Map()),
 };
});
vi.mock('$lib/stores/notifications', () => ({ notify: { banner: mocks.banner, dismissLive: vi.fn() } }));
import type { Writable } from 'svelte/store';
import AssistantHome from './AssistantHome.svelte';
import { assistantState, agentIdentities } from '$lib/stores/assistant';
import { sessions } from '$lib/stores/app-data.svelte';
import { activeSession, streamingSessions, backgroundPermissions } from '$lib/stores/active-session';
const rows = sessions as unknown as Writable<Record<string, unknown>[]>;
const active = activeSession as unknown as Writable<Record<string, unknown>>;
const streaming = streamingSessions as unknown as Writable<Map<string, unknown>>;
const prompts = backgroundPermissions as unknown as Writable<Map<string, { count: number; kind: string }>>;
const initial = (): AssistantState => ({
 configured: false, agent_id: null, home_session_id: null, enabled: false, status: 'unconfigured', research_sources: 0, agent: null,
 assistant_name: null, avatar_id: 'robot', avatar_presets: ['robot', 'spark', 'orbit', 'sprout', 'pixel'], introduction: {},
 proactive_guard_ready: true,
});
let server: AssistantState;
const configured = (patch: Partial<AssistantState> = {}) => {
 server = { ...initial(), configured: true, agent_id: 'guide', home_session_id: 'home', assistant_name: 'Guide', avatar_id: 'orbit', agent: { id: 'guide', name: 'Guide', avatar_id: 'orbit' }, status: 'no_sources', ...patch };
};
const source: AssistantSource = { id: 'connected', connector_id: 'connected', name: 'My authorized inbox', type: 'gmail', enabled: false, available: true, status: 'connected', trigger_ids: ['new_email'], observation_mode: 'poll_and_events', last_observed_at: null, last_error: null };
beforeEach(() => {
 vi.clearAllMocks(); server = initial(); assistantState.set(null); agentIdentities.set([]);
 rows.set([]); streaming.set(new Map()); prompts.set(new Map());
 active.set({ sessionId: null, isStreaming: false, activeTasks: [], pendingPermissions: [], pendingQuestions: [] });
 mocks.getAssistant.mockImplementation(async () => structuredClone(server));
 mocks.getAgentIdentities.mockResolvedValue([{ id: 'guide', name: 'Guide', avatar_id: 'orbit' }]);
 mocks.getAssistantSources.mockResolvedValue([]); mocks.getAssistantActivity.mockResolvedValue([]);
 mocks.loadSessions.mockResolvedValue(undefined); mocks.loadProfiles.mockResolvedValue(undefined);
 mocks.isVoiceEnabled.mockResolvedValue(true); mocks.openAssistant.mockResolvedValue({ session_id: 'home', home_session_id: 'home' });
 mocks.configureAssistant.mockImplementation(async patch => { server = { ...server, ...patch, configured: true, agent: { id: patch.agent_id, name: patch.assistant_name || 'Guide', avatar_id: patch.avatar_id || 'robot' }, assistant_name: patch.assistant_name || 'Guide', status: 'no_sources' }; return structuredClone(server); });
 mocks.setAssistantSource.mockImplementation(async (_id, enabled) => ({ ...source, enabled }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

async function useMine() {
 await fireEvent.click(await screen.findByRole('radio', { name: /One of my agents/ }));
}
async function choose() {
 await useMine();
 const select = await screen.findByRole('combobox', { name: 'Assistant agent' });
 await fireEvent.change(select, { target: { value: 'guide' } });
}
describe('Assistant home', () => {
 it('starts unconfigured with no ambient call or invented sources', async () => {
  render(AssistantHome); await choose();
  expect(screen.getByText('No sources connected')).toBeTruthy();
  expect(mocks.configureAssistant).not.toHaveBeenCalled(); expect(mocks.startCallForSession).not.toHaveBeenCalled();
  expect(mocks.openAssistant).not.toHaveBeenCalled();
 });
 it('defaults to a new dedicated agent named Assistant with a preset avatar', async () => {
  render(AssistantHome);
  const name = await screen.findByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement;
  expect(name.value).toBe('Assistant');
  expect((screen.getByRole('radio', { name: /A dedicated assistant agent/ }) as HTMLInputElement).checked).toBe(true);
  expect(screen.queryByRole('combobox', { name: 'Assistant agent' })).toBeNull();
  const pressed = screen.getAllByRole('button', { pressed: true });
  expect(pressed).toHaveLength(1);
  const avatar = pressed[0].getAttribute('aria-label')!.replace(' avatar', '');
  expect(AVATARS).toContain(avatar);
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalledTimes(1));
  const patch = mocks.configureAssistant.mock.calls[0][0];
  expect(patch).toEqual({ create_agent: true, assistant_name: 'Assistant', avatar_id: avatar });
 });
 it('has no introduction form, research switch, limit fields or limit numbers', async () => {
  render(AssistantHome); await screen.findByRole('textbox', { name: 'Assistant name' });
  expect(screen.queryByRole('textbox', { name: 'What should I call you?' })).toBeNull();
  expect(screen.queryByRole('textbox', { name: 'Repositories you care about' })).toBeNull();
  expect(screen.queryAllByRole('spinbutton')).toHaveLength(0);
  expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  expect(screen.queryByText(/background research/i)).toBeNull();
  expect(screen.queryByText(/12 passes|admission limit/)).toBeNull();
  expect(screen.getAllByRole('button', { name: / avatar$/ })).toHaveLength(AVATARS.length);
 });
 it('reuses the dedicated agent setup already made instead of creating another', async () => {
  mocks.getAgentIdentities.mockResolvedValue([{ id: 'assistant', name: 'Juniper', avatar_id: 'owl', dedicated: true }]);
  render(AssistantHome);
  await waitFor(() => expect((screen.getByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement).value).toBe('Juniper'));
  expect(screen.getByRole('button', { name: 'owl avatar' }).getAttribute('aria-pressed')).toBe('true');
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalledTimes(1));
  const patch = mocks.configureAssistant.mock.calls[0][0];
  expect(patch).toMatchObject({ agent_id: 'assistant', assistant_name: 'Juniper', avatar_id: 'owl' });
  expect(patch).not.toHaveProperty('create_agent');
 });
 it('saves only explicit identity for one of my agents; does not send agent config or limits', async () => {
  render(AssistantHome); await choose();
  await fireEvent.input(screen.getByRole('textbox', { name: 'Assistant name' }), { target: { value: 'Dotty' } });
  await fireEvent.click(screen.getByRole('button', { name: 'spark avatar' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalledTimes(1));
  const patch = mocks.configureAssistant.mock.calls[0][0];
  expect(patch).toEqual({ agent_id: 'guide', assistant_name: 'Dotty', avatar_id: 'spark' });
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
 it('has no research controls or usage; one neutral line names the sources in use', async () => {
  configured({ enabled: true, status: 'ready', research_sources: 2 });
  mocks.getAssistantSources.mockResolvedValue([{ ...source, enabled: true }, { ...source, id: 'second', connector_id: 'second', name: 'Second inbox', enabled: true }, { ...source, id: 'third', connector_id: 'third', name: 'Not opted in' }]);
  mocks.getAssistantActivity.mockResolvedValue([{ id: 'run', kind: 'linked', status: 'completed', session_id: 'task', session_title: 'Repository review', agent_name: 'Reviewer', avatar_id: 'pixel', source_ids: [], event_ids: [], cost_usd: .42, error: null, created_at: '2026-09-30T12:00:00Z', ended_at: null }]);
  render(AssistantHome);
  expect(await screen.findByText('Using 2 connected sources.')).toBeTruthy();
  for (const name of [/Pause/, /Resume/, /Review & enable/]) expect(screen.queryByRole('button', { name })).toBeNull();
  expect(screen.queryByText(/hard billing cap|runs · last 24 hours|in flight/)).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Repository review' }));
  expect(mocks.openSession).toHaveBeenCalledWith('task');
  expect(screen.queryByText(/UTC/)).toBeNull();
 });
 it('names the worker that holds the home and runs research when it is another one', async () => {
  configured({ enabled: true, status: 'ready', runs_here: false, home_worker: { device_id: 'dev_desk', name: 'Desk', is_self: false, online: false } });
  mocks.getAssistantSources.mockResolvedValue([source]);
  render(AssistantHome);
  expect(await screen.findByText(/runs on Desk, which is offline right now/)).toBeTruthy();
  expect(screen.getByText('gmail · Available on Desk')).toBeTruthy();
  expect(screen.getByText(/Research runs on Desk, so its activity is listed there/)).toBeTruthy();
  expect(screen.queryByText(/runs · last 24 hours|hard billing cap/)).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Open conversation' }));
  await waitFor(() => expect(mocks.openSession).toHaveBeenCalledWith('home'));
 });
 it('reloads when another worker changes the shared assistant', async () => {
  configured(); render(AssistantHome);
  await screen.findByRole('button', { name: 'Open conversation' });
  const before = mocks.getAssistant.mock.calls.length;
  server = { ...server, assistant_name: 'Renamed elsewhere', agent: { id: 'guide', name: 'Renamed elsewhere', avatar_id: 'orbit' } };
  resourceSync.invalidate(['assistant']);
  await waitFor(() => expect(mocks.getAssistant.mock.calls.length).toBeGreaterThan(before));
  expect(await screen.findByRole('heading', { name: 'Renamed elsewhere' })).toBeTruthy();
 });
 it('says no sources are in use when research is not running', async () => {
  configured({ status: 'guard_unavailable' }); mocks.getAssistantSources.mockResolvedValue([{ ...source, enabled: true }]);
  render(AssistantHome);
  expect(await screen.findByText('No connected sources in use.')).toBeTruthy();
 });
 it('does not erase a setup draft when background state changes', async () => {
  configured(); render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Edit assistant' }));
  expect((screen.getByRole('combobox', { name: 'Assistant agent' }) as HTMLSelectElement).value).toBe('guide');
  const name = screen.getByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement;
  await fireEvent.input(name, { target: { value: 'Editing' } });
  assistantState.update(value => value && { ...value, status: 'ready', research_sources: 1 });
  expect(name.value).toBe('Editing');
 });
 it('opens setup for an unconfigured owner when a retry follows a failed first load', async () => {
  mocks.getAssistantSources.mockRejectedValueOnce({ detail: 'Temporarily unavailable' });
  render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Retry refresh' }));
  expect(await screen.findByRole('textbox', { name: 'Assistant name' })).toBeTruthy();
  expect(screen.queryByRole('alert')).toBeNull();
 });
 it('keeps save errors visible with the editable draft intact', async () => {
  mocks.configureAssistant.mockRejectedValue({ detail: 'Agent is unavailable' }); render(AssistantHome); await choose();
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Agent is unavailable'));
  expect((screen.getByRole('combobox', { name: 'Assistant agent' }) as HTMLSelectElement).value).toBe('guide');
 });
 it('does not send presentation mutations for a built-in agent', async () => {
  render(AssistantHome); await useMine();
  await fireEvent.change(await screen.findByRole('combobox', { name: 'Assistant agent' }), { target: { value: 'builtin' } });
  expect((screen.getByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement).disabled).toBe(true);
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
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
describe('Assistant home call gate', () => {
 const startCall = async () => await screen.findByRole('button', { name: 'Start call' }) as HTMLButtonElement;
 it.each([
  ['reports running tasks', () => rows.set([{ id: 'home', status: 'active', has_running_tasks: true }]), /finishes working/],
  ['is streaming on another device', () => streaming.set(new Map([['home', {}]])), /finishes working/],
  ['waits on a permission or question', () => prompts.set(new Map([['home', { count: 1, kind: 'question' }]])), /finishes working/],
  ['is the open session and streaming', () => active.set({ sessionId: 'home', isStreaming: true, activeTasks: [], pendingPermissions: [], pendingQuestions: [] }), /finishes working/],
  ['is parked on background work', () => rows.set([{ id: 'home', status: 'active', parked_task_kind: 'shell' }]), /background tasks finish/],
 ])('disables Start call with a reason while the home %s', async (_label, arrange, reason) => {
  configured(); arrange(); render(AssistantHome);
  const button = await startCall();
  expect(button.disabled).toBe(true);
  expect(button.title).toMatch(reason);
  await fireEvent.click(button);
  expect(mocks.openAssistant).not.toHaveBeenCalled();
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
 });
 it('re-enables Start call once the home finishes', async () => {
  configured(); rows.set([{ id: 'home', status: 'active', has_running_tasks: true }]); render(AssistantHome);
  const button = await startCall();
  expect(button.disabled).toBe(true);
  rows.set([{ id: 'home', status: 'active', has_running_tasks: false }]);
  await waitFor(() => expect(button.disabled).toBe(false));
  await fireEvent.click(button);
  await waitFor(() => expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true }));
 });
 it('explains a blocked Start call with a brief banner on a touch screen', async () => {
  vi.stubGlobal('matchMedia', (query: string) => ({ matches: query === '(hover: none)', media: query, addEventListener() {}, removeEventListener() {} }));
  configured(); rows.set([{ id: 'home', status: 'active', parked_task_kind: 'agent' }]); render(AssistantHome);
  const button = await startCall();
  expect(button.disabled).toBe(false);
  expect(button.getAttribute('aria-disabled')).toBe('true');
  await fireEvent.click(button);
  expect(mocks.banner).toHaveBeenCalledWith(button.title, expect.objectContaining({ persist: false }));
  expect(mocks.openAssistant).not.toHaveBeenCalled();
 });
});
