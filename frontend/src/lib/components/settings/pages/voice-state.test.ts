import { beforeEach, describe, expect, it, vi } from 'vitest';

const voiceApi = vi.hoisted(() => ({ load: vi.fn(), save: vi.fn() }));
const transport = vi.hoisted(() => ({ get: vi.fn() }));
const identity = vi.hoisted(() => ({ epoch: 1 }));
vi.mock('$lib/api/voice', () => ({
	loadVoiceSettings: voiceApi.load,
	saveVoiceSettings: voiceApi.save,
	previewVoice: vi.fn()
}));
vi.mock('$lib/api/client', () => ({ api: transport }));
vi.mock('$lib/stores/session-owner', () => ({ sessionOwnerEpoch: () => identity.epoch }));

import { VoiceState } from './voice-state.svelte';
import { saveStatus } from '$lib/settings/save-status.svelte';

const settings = (voice: string) => ({ voice_voice: voice, voice_model: 'gpt-live-1' }) as never;

function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((r) => (resolve = r));
	return { promise, resolve };
}

beforeEach(() => {
	vi.resetAllMocks();
	identity.epoch = 1;
	saveStatus.reset();
});

describe('VoiceState sync', () => {
	it('never lets an older read overwrite a newer one', async () => {
		const state = new VoiceState();
		const slow = deferred<unknown>();
		voiceApi.load.mockReturnValueOnce(slow.promise).mockResolvedValueOnce(settings('cedar'));
		const first = state.refresh();
		await state.refresh();
		slow.resolve(settings('marin'));
		await first;
		expect((state.settings as { voice_voice: string } | null)?.voice_voice).toBe('cedar');
	});

	it('ignores a read that lands while a save is in flight, then takes the saved value', async () => {
		const state = new VoiceState();
		const put = deferred<unknown>();
		voiceApi.save.mockReturnValueOnce(put.promise);
		voiceApi.load.mockResolvedValue(settings('cedar'));
		const saving = state.save({ voice_voice: 'cedar' });
		await state.refresh();
		expect(state.settings).toBeNull();
		put.resolve(settings('cedar'));
		expect(await saving).toBe(true);
		expect(saveStatus.visible).toBe(true);
		expect((state.settings as { voice_voice: string } | null)?.voice_voice).toBe('cedar');
	});

	it('treats a 403 as read-only, not as an error', async () => {
		const state = new VoiceState();
		voiceApi.load.mockRejectedValue({ status: 403, detail: 'Admin only' });
		await state.refresh();
		expect(state.status).toBe('forbidden');
		expect(state.error).toBe('');
	});

	it('refuses to write after the account changed', async () => {
		const state = new VoiceState();
		identity.epoch = 2;
		expect(await state.save({ voice_mode_enabled: true })).toBe(false);
		expect(voiceApi.save).not.toHaveBeenCalled();
	});

	it('reports a failed save so controls can revert', async () => {
		const state = new VoiceState();
		voiceApi.save.mockRejectedValue({ detail: 'Nope' });
		voiceApi.load.mockResolvedValue(settings('marin'));
		expect(await state.save({ voice_voice: 'cedar' })).toBe(false);
		expect(state.error).toBe('Nope');
	});

	it('lists only providers that can fund GPT Live calls', async () => {
		const state = new VoiceState();
		transport.get.mockResolvedValue([
			{ id: 'openai', name: 'OpenAI API key' },
			{ id: '__fal_voice__', name: 'fal.ai' }
		]);
		await state.loadProviders();
		expect(state.providers).toEqual([{ id: 'openai', name: 'OpenAI API key' }]);
	});
});
