import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const transport = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), patch: vi.fn() }));
const identity = vi.hoisted(() => ({ epoch: 0, listeners: new Set<() => void>() }));
vi.mock('$lib/api/client', () => ({ api: transport, getErrorMessage: (_e: unknown, fallback: string) => fallback }));
vi.mock('$lib/stores/session-owner', () => ({
	sessionOwnerEpoch: () => identity.epoch,
	sessionOwnerBoundary: {
		subscribe(callback: () => void) {
			identity.listeners.add(callback);
			callback();
			return () => identity.listeners.delete(callback);
		}
	}
}));

import VoicePage from './VoicePage.svelte';
import { resourceSync } from '$lib/services/resource-sync';
import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import { createVoiceOutput, setVoiceVolume, voiceVolume } from '$lib/services/voiceVolume';
import { get } from 'svelte/store';

const settings = (voice = 'marin', minutes = 60) => ({
	voice_mode_enabled: true,
	voice_model: 'gpt-live-1',
	voice_voice: voice,
	voice_provider_id: null as string | null,
	voice_max_call_minutes: minutes,
	voice_app_tools_enabled: false,
	voice_input_model: 'fal-ai/whisper',
	voice_input_mode: 'auto' as 'auto' | 'toggle',
	voice_input_silence_seconds: 5,
	voice_input_models: [
		{ id: 'fal-ai/whisper', name: 'Whisper (Large v3)', provider: 'fal-ai' },
		{ id: 'openai/gpt-4o-mini-transcribe', name: 'GPT-4o mini Transcribe', provider: 'openai' }
	],
	voice_options: { opening: '', instructions: '', delegation_instructions: '' },
	model_details: {
		model: 'gpt-live-1',
		transport: 'stream' as const,
		voices: ['marin', 'cedar'].map((id) => ({ id, label: id })),
		runtime_settings: [],
		prompts: [
			{ label: 'Voice conversation prompt', key: 'instructions' as const, text: 'Talk', default_text: 'Talk' },
			{ label: 'Chat agent prompt', key: 'delegation_instructions' as const, text: 'Work', default_text: 'Work' }
		],
		prompt_note: 'Runtime context is appended.',
		preview_note: ''
	}
});
type Settings = ReturnType<typeof settings>;

let server: Settings;
let providers: { id: string; name: string }[];

function changeAccount() {
	identity.epoch++;
	for (const listener of identity.listeners) listener();
}

beforeEach(() => {
	transport.get.mockReset();
	transport.put.mockReset();
	changeAccount();
	server = settings();
	providers = [
		{ id: 'openai-key', name: 'OpenAI API key' },
		{ id: '__fal_voice__', name: 'fal.ai' }
	];
	transport.get.mockImplementation(async (path: string) => {
		if (path === '/settings/voice') return structuredClone(server);
		if (path === '/voice/providers') return providers;
		return [];
	});
	transport.put.mockImplementation(async (_path: string, patch: Partial<Settings>) => {
		server = { ...server, ...patch } as Settings;
		return structuredClone(server);
	});
	hotkeyPreferences.setGlobalSttEnabled(false);
	settingsNav.reset();
});
afterEach(() => {
	cleanup();
	resourceSync.clear();
});

async function mountPage() {
	const view = render(VoicePage);
	await waitFor(() => expect((screen.getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement).value).toBe(server.voice_voice));
	return {
		...view,
		remote(value: Settings) {
			server = value;
			resourceSync.invalidate(['settings']);
		}
	};
}

const field = (name: string) => screen.getByLabelText(name) as HTMLInputElement;

describe('Voice page', () => {
	describe('voice volume', () => {
		afterEach(() => {
			setVoiceVolume(100);
			localStorage.removeItem('voice-playback-volume');
		});

		it('saves on this device, boosts past 100%, and reaches a call already playing', async () => {
			setVoiceVolume(100);
			// A call in progress: its output gain follows the same store.
			const gain = { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() };
			const limiter = { threshold: { value: 0 }, knee: { value: 0 }, ratio: { value: 0 }, attack: { value: 0 }, release: { value: 0 }, connect: vi.fn(), disconnect: vi.fn() };
			const output = createVoiceOutput({ createGain: () => gain, createDynamicsCompressor: () => limiter, destination: {} } as unknown as AudioContext);
			await mountPage();
			const slider = screen.getByRole('slider', { name: 'Voice volume' }) as HTMLInputElement;
			expect(slider.max).toBe('300');
			expect(slider.value).toBe('100');
			await fireEvent.input(slider, { target: { value: '250' } });
			expect(get(voiceVolume)).toBe(250);
			expect(localStorage.getItem('voice-playback-volume')).toBe('250');
			expect(gain.gain.value).toBe(2.5);
			expect(slider.getAttribute('aria-valuetext')).toBe('250% (boosted)');
			const row = slider.closest('[data-setting-anchor]') as HTMLElement;
			expect(row.textContent).toContain('250%');
			expect(row.textContent).toContain('Boosted');
			// A device preference: the worker's voice settings are untouched.
			expect(transport.put).not.toHaveBeenCalled();
			output.close();
		});

		it('resets to 100%', async () => {
			setVoiceVolume(40);
			await mountPage();
			const row = screen.getByRole('slider', { name: 'Voice volume' }).closest('[data-setting-anchor]') as HTMLElement;
			await fireEvent.click(within(row).getByRole('button', { name: /reset/i }));
			expect(get(voiceVolume)).toBe(100);
		});
	});

	it('renders a row for every Voice entry once global input is on', async () => {
		hotkeyPreferences.setGlobalSttEnabled(true);
		await assertCategoryCoverage('voice', () => render(VoicePage).container);
	});

	it('shows the hotkey only while global voice input is on', async () => {
		await mountPage();
		expect(screen.queryByText('Voice input hotkey')).toBeNull();
		await fireEvent.click(screen.getByRole('switch', { name: 'Global voice input' }));
		expect(hotkeyPreferences.current.globalSttEnabled).toBe(true);
		expect(screen.getByText('Voice input hotkey')).toBeTruthy();
	});

	it('marks the per-device rows, and only those, as this device only', async () => {
		hotkeyPreferences.setGlobalSttEnabled(true);
		await mountPage();
		const rowOf = (label: string) => screen.getByText(label).closest('[data-setting-anchor]') as HTMLElement;
		for (const label of ['Global voice input', 'Voice input hotkey', 'Sound cues', 'Voice volume']) {
			expect(rowOf(label).textContent, label).toContain('This device only');
		}
		// Worker settings follow the account to every device.
		expect(rowOf('Microphone mode').textContent).not.toContain('This device only');
		expect(rowOf('Stop after silence').textContent).not.toContain('This device only');
	});

	it('links the dictation model to Default models without editing it here', async () => {
		await mountPage();
		const link = screen.getByRole('button', { name: /Dictation model: Whisper \(Large v3\)/ });
		await fireEvent.click(link);
		expect(settingsNav.category).toBe('models');
		expect(transport.put).not.toHaveBeenCalled();
	});

	it('defaults to Auto, saves the silence timeout, and hides it in Toggle mode', async () => {
		await mountPage();
		expect(field('Stop after silence').value).toBe('5');
		await fireEvent.input(field('Stop after silence'), { target: { value: '9' } });
		await fireEvent.blur(field('Stop after silence'));
		await waitFor(() => expect(transport.put).toHaveBeenCalledWith('/settings/voice', { voice_input_silence_seconds: 9 }));
		await fireEvent.click(screen.getByRole('button', { name: 'Toggle' }));
		await waitFor(() => expect(transport.put).toHaveBeenCalledWith('/settings/voice', { voice_input_mode: 'toggle' }));
		await waitFor(() => expect(screen.queryByLabelText('Stop after silence')).toBeNull());
		expect(screen.getByText('Click the mic to stop')).toBeTruthy();
	});

	it.each([
		['0', 1],
		['61', 60],
		['2.4', 2]
	])('clamps a silence timeout of %s to a whole number of seconds (%i)', async (value, saved) => {
		await mountPage();
		await fireEvent.input(field('Stop after silence'), { target: { value } });
		await fireEvent.blur(field('Stop after silence'));
		await waitFor(() => expect(transport.put).toHaveBeenCalledWith('/settings/voice', { voice_input_silence_seconds: saved }));
	});

	it('does not save an empty silence timeout', async () => {
		await mountPage();
		await fireEvent.input(field('Stop after silence'), { target: { value: '' } });
		await fireEvent.blur(field('Stop after silence'));
		expect(transport.put).not.toHaveBeenCalled();
		expect(field('Stop after silence').value).toBe('5');
	});

	it('loads server voices without hardcoding their names', async () => {
		const view = await mountPage();
		view.remote({ ...settings('quartz'), model_details: { ...settings().model_details, voices: [{ id: 'quartz', label: 'Quartz' }] } });
		await waitFor(() => expect(screen.getByRole('button', { name: 'Play Quartz sample' })).toBeTruthy());
	});

	it('saves voice selection without changing the model, provider or capability settings', async () => {
		await mountPage();
		await fireEvent.change(screen.getByRole('combobox', { name: 'Voice' }), { target: { value: 'cedar' } });
		expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_voice: 'cedar' });
	});

	it('saves only the edited call limit', async () => {
		await mountPage();
		await fireEvent.input(field('Max call length'), { target: { value: '120' } });
		await fireEvent.blur(field('Max call length'));
		expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_max_call_minutes: 120 });
	});

	it('turns voice calls off', async () => {
		await mountPage();
		await fireEvent.click(screen.getByRole('switch', { name: 'Voice calls' }));
		expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_mode_enabled: false });
	});

	it('saves the opening words on their own', async () => {
		await mountPage();
		const opening = field('Opening words');
		await fireEvent.focus(opening);
		await fireEvent.input(opening, { target: { value: 'Hi there' } });
		await fireEvent.blur(opening);
		expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', {
			voice_options: { opening: 'Hi there' },
			voice_options_model: 'gpt-live-1'
		});
	});

	it('keeps the last good value and reports a rejected save', async () => {
		await mountPage();
		transport.put.mockRejectedValueOnce({ detail: 'Voice not supported' });
		await fireEvent.change(screen.getByRole('combobox', { name: 'Voice' }), { target: { value: 'cedar' } });
		expect((await screen.findByRole('alert')).textContent).toBe('Voice not supported');
		await waitFor(() => expect((screen.getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement).value).toBe('marin'));
	});
});

describe('Calls run on', () => {
	it('links the one OpenAI provider and ignores fal.ai', async () => {
		await mountPage();
		expect(await screen.findByRole('button', { name: /Calls run on OpenAI API key/ })).toBeTruthy();
		expect(screen.getByText('gpt-live-1')).toBeTruthy();
	});

	it('offers a select when several providers can fund calls, showing the first by name until one is pinned', async () => {
		providers = [
			{ id: 'zeta', name: 'Zeta key' },
			{ id: 'alpha', name: 'Alpha key' }
		];
		await mountPage();
		const select = (await screen.findByRole('combobox', { name: 'Calls run on' })) as HTMLSelectElement;
		expect(select.value).toBe('alpha');
		await fireEvent.change(select, { target: { value: 'zeta' } });
		expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_provider_id: 'zeta' });
	});

	it('points to Providers when no provider can fund calls', async () => {
		providers = [];
		await mountPage();
		expect(await screen.findByRole('button', { name: /Add an OpenAI API key/ })).toBeTruthy();
	});
});

describe('mounted voice settings synchronization', () => {
	it('refreshes controls without sending settings writes', async () => {
		const view = await mountPage();
		view.remote(settings('cedar', 90));
		await waitFor(() => expect((screen.getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement).value).toBe('cedar'));
		expect(field('Max call length').value).toBe('90');
		expect(transport.put).not.toHaveBeenCalled();
	});

	it('keeps an uncommitted minutes field while other controls refresh', async () => {
		const view = await mountPage();
		await fireEvent.input(field('Max call length'), { target: { value: '123' } });
		view.remote(settings('cedar', 90));
		await waitFor(() => expect((screen.getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement).value).toBe('cedar'));
		expect(field('Max call length').value).toBe('123');
		expect(transport.put).not.toHaveBeenCalled();
	});

	it('prevents an old editor from saving after an account change', async () => {
		await mountPage();
		await fireEvent.input(field('Max call length'), { target: { value: '123' } });
		changeAccount();
		await fireEvent.blur(field('Max call length'));
		expect(transport.put).not.toHaveBeenCalled();
	});
});

describe('voice preview playback', () => {
	it('plays the selected voice without changing it and reuses its sample', async () => {
		const module = await import('$lib/api/voice');
		const preview = vi.spyOn(module, 'previewVoice').mockResolvedValue(new Blob(['audio'], { type: 'audio/wav' }));
		const play = vi.fn().mockResolvedValue(undefined);
		const pause = vi.fn();
		vi.stubGlobal('Audio', class { src = ''; onended = null; onerror = null; play = play; pause = pause; });
		const create = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:sample');
		const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
		try {
			const view = await mountPage();
			await fireEvent.click(screen.getByRole('button', { name: 'Play marin sample' }));
			await waitFor(() => expect(play).toHaveBeenCalledOnce());
			expect(preview).toHaveBeenCalledWith('marin', expect.any(AbortSignal));
			expect(transport.put).not.toHaveBeenCalled();
			await fireEvent.click(screen.getByRole('button', { name: 'Stop marin sample' }));
			expect(pause).toHaveBeenCalledOnce();
			await fireEvent.click(screen.getByRole('button', { name: 'Play marin sample' }));
			await waitFor(() => expect(play).toHaveBeenCalledTimes(2));
			expect(preview).toHaveBeenCalledOnce();
			view.unmount();
			expect(revoke).toHaveBeenCalledWith('blob:sample');
		} finally {
			preview.mockRestore();
			create.mockRestore();
			revoke.mockRestore();
			vi.unstubAllGlobals();
		}
	});

	it('aborts a pending sample on stop and reports failed previews accessibly', async () => {
		const module = await import('$lib/api/voice');
		let signal: AbortSignal | undefined;
		const preview = vi.spyOn(module, 'previewVoice').mockImplementation((_voice, abort) => {
			signal = abort;
			return new Promise((_resolve, reject) =>
				abort?.addEventListener('abort', () => reject(new DOMException('Cancelled', 'AbortError')))
			);
		});
		vi.stubGlobal('Audio', class { pause() {} });
		try {
			await mountPage();
			await fireEvent.click(screen.getByRole('button', { name: 'Play marin sample' }));
			// The volume slider's <output> is a status too; this is the preview's.
			expect(screen.getAllByRole('status').some((node) => node.textContent?.includes('Generating'))).toBe(true);
			await fireEvent.click(screen.getByRole('button', { name: 'Stop marin sample' }));
			expect(signal?.aborted).toBe(true);
			expect(screen.queryByRole('alert')).toBeNull();
			preview.mockRejectedValue({ detail: 'GPT Live unavailable' });
			await fireEvent.click(screen.getByRole('button', { name: 'Play marin sample' }));
			expect((await screen.findByRole('alert')).textContent).toBe('GPT Live unavailable');
		} finally {
			preview.mockRestore();
			vi.unstubAllGlobals();
		}
	});
});
