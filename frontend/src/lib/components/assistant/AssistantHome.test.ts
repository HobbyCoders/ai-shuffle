import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import { AVATARS, type AssistantState, type AssistantSource } from '$lib/api/assistant';
import { resourceSync } from '$lib/services/resource-sync';

const mocks = vi.hoisted(() => ({
 getAssistant: vi.fn(), getAgentIdentities: vi.fn(), configureAssistant: vi.fn(), openAssistant: vi.fn(),
 getAssistantSources: vi.fn(), getAssistantActivity: vi.fn(), setAssistantSource: vi.fn(), getAssistantLimits: vi.fn(),
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
 mocks.getAssistantLimits.mockResolvedValue({ spend_limit_enabled: true, daily_spend_limit_usd: 20, job_limit_enabled: true, max_job_minutes: 120, spent_today_usd: 0, day: '2026-10-03', timezone: 'UTC', resets_at: '2026-10-04T00:00:00+00:00', limit_reached: false, warning: false });
 mocks.loadSessions.mockResolvedValue(undefined); mocks.loadProfiles.mockResolvedValue(undefined);
 mocks.isVoiceEnabled.mockResolvedValue(true); mocks.openAssistant.mockResolvedValue({ session_id: 'home', home_session_id: 'home' });
 mocks.configureAssistant.mockImplementation(async patch => { server = { ...server, ...patch, configured: true, agent: { id: patch.agent_id, name: patch.assistant_name || 'Guide', avatar_id: patch.avatar_id || 'robot' }, assistant_name: patch.assistant_name || 'Guide', status: 'no_sources' }; return structuredClone(server); });
 mocks.setAssistantSource.mockImplementation(async (_id, enabled) => ({ ...source, enabled }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('Assistant home', () => {
 it('starts unconfigured with no ambient call or invented sources', async () => {
  render(AssistantHome); await screen.findByRole('textbox', { name: 'Assistant name' });
  expect(screen.getByText('No sources connected')).toBeTruthy();
  expect(mocks.configureAssistant).not.toHaveBeenCalled(); expect(mocks.startCallForSession).not.toHaveBeenCalled();
  expect(mocks.openAssistant).not.toHaveBeenCalled();
 });
 it('offers no agent choice, agent picker or agent links', async () => {
  render(AssistantHome); await screen.findByRole('textbox', { name: 'Assistant name' });
  expect(screen.queryByRole('radiogroup')).toBeNull();
  expect(screen.queryAllByRole('radio')).toHaveLength(0);
  expect(screen.queryByRole('combobox', { name: /agent/i })).toBeNull();
  expect(screen.queryByRole('button', { name: /Manage agents/ })).toBeNull();
  expect(screen.queryByText(/What runs it|dedicated assistant agent|One of my agents/)).toBeNull();
 });
 it('defaults to Assistant with a preset avatar and sends only name and avatar', async () => {
  render(AssistantHome);
  const name = await screen.findByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement;
  expect(name.value).toBe('Assistant');
  const pressed = screen.getAllByRole('button', { pressed: true });
  expect(pressed).toHaveLength(1);
  const avatar = pressed[0].getAttribute('aria-label')!.replace(' avatar', '');
  expect(AVATARS).toContain(avatar);
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalledTimes(1));
  expect(mocks.configureAssistant.mock.calls[0][0]).toEqual({ assistant_name: 'Assistant', avatar_id: avatar });
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
 it('saves an edited name and avatar without agent fields or limits', async () => {
  render(AssistantHome);
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Assistant name' }), { target: { value: 'Dotty' } });
  await fireEvent.click(screen.getByRole('button', { name: 'spark avatar' }));
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalledTimes(1));
  expect(mocks.configureAssistant.mock.calls[0][0]).toEqual({ assistant_name: 'Dotty', avatar_id: 'spark' });
  expect(mocks.setAssistantSource).not.toHaveBeenCalled();
 });
 it('reopens the same home and attaches a call only on a deliberate call action', async () => {
  configured(); render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Open conversation' }));
  await waitFor(() => expect(mocks.openSession).toHaveBeenCalledWith('home', undefined));
  expect(mocks.startCallForSession).not.toHaveBeenCalled();
  await fireEvent.click(screen.getByRole('button', { name: 'Start call' }));
  await waitFor(() => expect(mocks.startCallForSession).toHaveBeenCalledWith('home', { attach: true }));
  expect(mocks.openAssistant).toHaveBeenCalledTimes(2);
 });
 it('uses the returned worker-local session when the home is absent from ordinary lists', async () => {
  configured({ home_session_id: null });
  const home = { id: 'local-home', hidden: true, title: 'Guide', mode: null };
  mocks.openAssistant.mockImplementation(async () => {
   server = { ...server, home_session_id: home.id };
   return { session_id: home.id, home_session_id: home.id, session: home };
  });
  render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Open conversation' }));
  await waitFor(() => expect(mocks.openSession).toHaveBeenCalledWith(home.id, home));
  expect(get(assistantState)?.home_session_id).toBe(home.id);
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
  await waitFor(() => expect(mocks.openSession).toHaveBeenCalledWith('home', undefined));
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
  const name = screen.getByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement;
  expect(name.value).toBe('Guide');
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
  mocks.configureAssistant.mockRejectedValue({ detail: 'Assistant is unavailable' }); render(AssistantHome);
  await fireEvent.input(await screen.findByRole('textbox', { name: 'Assistant name' }), { target: { value: 'Draft' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Create assistant' }));
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', expect.stringContaining('Assistant is unavailable'));
  expect((screen.getByRole('textbox', { name: 'Assistant name' }) as HTMLInputElement).value).toBe('Draft');
 });
 it('ignores an agent id passed in by older entry points', async () => {
  configured(); render(AssistantHome, { requestedAgentId: 'guide' });
  expect(await screen.findByRole('button', { name: 'Open conversation' })).toBeTruthy();
  expect(screen.queryByRole('combobox', { name: /agent/i })).toBeNull();
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

describe('About you', () => {
 const detected = { preferred_name: 'ada', email: 'ada@example.com', projects: ['Shuffle'] };
 const about = (patch: Partial<AssistantState> = {}) => configured({
  introduction: { ...detected, github_username: 'ada-gh' }, introduction_detected: detected,
  introduction_corrected: ['github_username'], suggested_connections: [], ...patch
 });
 it('shows what was detected read-only and asks the server to re-detect once loaded', async () => {
  about(); render(AssistantHome);
  const list = (await screen.findByRole('heading', { name: 'What your assistant knows' })).closest('section')!;
  expect(list.textContent).toContain('ada@example.com');
  expect(list.textContent).toContain('ada-gh· corrected');
  expect(list.textContent).toContain('Shuffle');
  expect(list.textContent).toMatch(/RepositoriesNot found/);
  expect(list.textContent).not.toContain('Notes');
  expect(list.querySelectorAll('input')).toHaveLength(0);
  await waitFor(() => expect(mocks.getAssistant).toHaveBeenCalledWith(true));
 });
 it('corrects fields; an empty field goes back to the detected value', async () => {
  about();
  mocks.configureAssistant.mockImplementation(async patch => ({ ...structuredClone(server), introduction: { ...detected, preferred_name: patch.introduction.preferred_name }, introduction_corrected: ['preferred_name'] }));
  render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: /Correct/ }));
  const name = screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement;
  expect(name.value).toBe('');
  expect(name.placeholder).toBe('ada');
  expect((screen.getByRole('textbox', { name: 'GitHub username' }) as HTMLInputElement).value).toBe('ada-gh');
  await fireEvent.input(name, { target: { value: 'Ada Lovelace' } });
  await fireEvent.input(screen.getByRole('textbox', { name: 'GitHub username' }), { target: { value: '' } });
  await fireEvent.input(screen.getByRole('textbox', { name: 'Repositories' }), { target: { value: 'ada/one, ada/two' } });
  await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(mocks.configureAssistant).toHaveBeenCalledWith({ introduction: {
   preferred_name: 'Ada Lovelace', email: '', github_username: '', repository_refs: ['ada/one', 'ada/two']
  } }));
  const section = screen.getByRole('heading', { name: 'What your assistant knows' }).closest('section')!;
  await waitFor(() => expect(section.textContent).toContain('Ada Lovelace· corrected'));
  expect(screen.queryByRole('textbox', { name: 'Name' })).toBeNull();
 });
 it('suggests only the missing connections and links to the existing flows', async () => {
  about({ suggested_connections: ['gmail', 'github'] }); render(AssistantHome);
  await fireEvent.click(await screen.findByRole('button', { name: 'Connect Gmail' }));
  // Settings > Plugins > Gmail, and Settings > Integrations > GitHub (where sign-in lives now).
  expect(mocks.openFeaturePage).toHaveBeenCalledWith('plugins', 'connection:gmail');
  await fireEvent.click(screen.getByRole('button', { name: 'Sign in to GitHub' }));
  expect(mocks.openFeaturePage).toHaveBeenCalledWith('settings', 'category=integrations&item=github');
 });
 it('shows no suggestion cards when nothing is missing', async () => {
  about(); render(AssistantHome);
  await screen.findByRole('heading', { name: 'What your assistant knows' });
  expect(screen.queryByRole('button', { name: 'Connect Gmail' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Sign in to GitHub' })).toBeNull();
 });
 it('shows the owner limits once set up, never in the setup form', async () => {
  render(AssistantHome); await screen.findByRole('textbox', { name: 'Assistant name' });
  expect(screen.queryByRole('heading', { name: 'Limits' })).toBeNull();
  cleanup(); configured();
  render(AssistantHome);
  expect(await screen.findByRole('heading', { name: 'Limits' })).toBeTruthy();
  expect(await screen.findByText(/spent today/)).toBeTruthy();
  expect(mocks.getAssistantLimits).toHaveBeenCalled();
 });
});
