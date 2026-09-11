import { beforeEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
const mocks = vi.hoisted(() => ({ settings:vi.fn(), create: vi.fn(), start: vi.fn(), confirm: vi.fn(), switchSession: vi.fn(), load: vi.fn(), open: vi.fn(), lane: vi.fn(), store: null as any }));
vi.mock('$lib/stores/active-session', () => ({ activeSession: Object.assign(mocks.store, { switchSession: mocks.switchSession }) }));
vi.mock('$lib/stores/app-data.svelte', () => ({ appData: { loadSessions: mocks.load } }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openChat: mocks.open } }));
vi.mock('$lib/stores/sessionMode.svelte', () => ({ appMode: { set: mocks.lane } }));
vi.mock('$lib/stores/notifications', () => ({ notify: { error: vi.fn() } }));
vi.mock('$lib/api/voice', async original => ({...await original<typeof import('$lib/api/voice')>(), createVoiceSession: mocks.create, loadVoiceSettings:mocks.settings }));
vi.mock('$lib/services/voiceCall', () => ({ startCall: mocks.start, confirmCallReplacement: mocks.confirm }));
mocks.store = writable({ profile: 'agent', project: 'project', projectV2Id: 'pv2', worktreeId: 'wt', selectedWorktreeBranch: 'feature/voice', worktreeBranch: 'feature/voice', worktreeBaseBranch: 'main', worktreeIntent: 'worktree', deviceIntent: { id: 'runner' }, modelOverride: 'model', effortLevelOverride: 'high', permissionModeOverride: 'default', fastMode: true, contextWindowChoice: 'standard' });
const { startNewVoiceSession } = await import('./startVoiceSession');
beforeEach(() => { vi.clearAllMocks(); mocks.settings.mockResolvedValue({voice_model:'gpt-realtime'}); mocks.confirm.mockResolvedValue(true); mocks.create.mockResolvedValue({ session_id: 'new', client_secret: 'test', model: 'realtime' }); });
describe('voice creation context', () => {
	it('carries execution bindings without converting the voice lane to code', async () => {
		expect(await startNewVoiceSession()).toBe(true);
		expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({ profile_id: 'agent', project_id: 'project', project_v2_id: 'pv2', worktree_id: 'wt', worktree_branch: 'feature/voice', base_branch: 'main', target_instance_id: 'runner', execution_mode: 'worktree', model: 'model', effort: 'high', permission_mode: 'default', fast: true }));
		expect(mocks.lane).toHaveBeenCalledWith('voice');
		expect(mocks.start).toHaveBeenCalledWith('new', { client_secret: 'test', model: 'realtime' });
	});
	it('uses saved client defaults outside the voice lane and forwards the pinned options', async () => {
		mocks.settings.mockResolvedValue({voice_model:'gpt-live-1',voice_options:{delegation_type:'client',delegation_model:'saved-model',delegation_effort:'low'}});
		mocks.create.mockResolvedValue({session_id:'new',client_secret:'',model:'gpt-live-1',options:{delegation_type:'client'}});
		expect(await startNewVoiceSession()).toBe(true);
		expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({model:'saved-model',effort:'low'}));
		expect(mocks.start).toHaveBeenCalledWith('new',{client_secret:'',model:'gpt-live-1',options:{delegation_type:'client'}});
	});
	it('does not create a row after replacement is declined', async () => {
		mocks.confirm.mockResolvedValue(false);
		expect(await startNewVoiceSession()).toBe(false);
		expect(mocks.create).not.toHaveBeenCalled();
	});
});
