import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
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

const SUPERGROK = 'grok-voice-supergrok';
const LABEL = 'Grok voice (SuperGrok subscription)';
const PREVIEW_NOTE = 'Calls use your connected SuperGrok login. Previews are not available for this engine.';
const GROK_VOICES = [{ id: 'eve', label: 'Eve' }, { id: 'ara', label: 'Ara' }];

type Details = Record<string, unknown>;
let server: Record<string, unknown> & { voice_model: string; voice_voice: string; model_details: Details };
let providers: { id: string; name: string; provider_type?: string }[];

/** Server rule: model_details describe the CURRENTLY selected engine only. */
function serve(patch: Record<string, unknown> = {}) {
	const next = { ...server, ...patch } as typeof server;
	const supergrok = next.voice_model === SUPERGROK;
	server = {
		...next,
		voice_voice: supergrok && next.voice_voice === 'marin' ? 'eve' : next.voice_voice,
		model_details: supergrok
			? { ...next.model_details, model: SUPERGROK, label: LABEL, available: true, unavailable_reason: null, voices: GROK_VOICES, preview_note: PREVIEW_NOTE }
			: { ...next.model_details, model: next.voice_model }
	};
	return structuredClone(server);
}

beforeEach(() => {
	transport.get.mockReset();
	transport.put.mockReset();
	identity.epoch++;
	for (const listener of identity.listeners) listener();
	server = {
		voice_mode_enabled: true,
		voice_model: 'gpt-live-1',
		voice_voice: 'marin',
		voice_provider_id: null,
		voice_max_call_minutes: 60,
		voice_app_tools_enabled: false,
		voice_input_model: 'fal-ai/whisper',
		voice_input_mode: 'auto',
		voice_input_silence_seconds: 5,
		voice_input_models: [{ id: 'fal-ai/whisper', name: 'Whisper (Large v3)', provider: 'fal-ai' }],
		voice_options: { instructions: '', delegation_instructions: '' },
		model_details: {
			model: 'gpt-live-1',
			transport: 'stream',
			voices: [{ id: 'marin', label: 'marin' }],
			runtime_settings: [],
			prompts: [],
			prompt_note: '',
			preview_note: ''
		}
	};
	providers = [
		{ id: 'openai-key', name: 'OpenAI API key', provider_type: 'openai' },
		{ id: 'xai-key', name: 'xAI API key', provider_type: 'xai' },
		{ id: 'chatgpt-login', name: 'ChatGPT Plus', provider_type: 'chatgpt' },
		{ id: 'grok-login', name: 'SuperGrok', provider_type: 'grok' }
	];
	transport.get.mockImplementation(async (path: string) => {
		if (path === '/settings/voice') return structuredClone(server);
		if (path === '/voice/providers') return providers;
		return [];
	});
	transport.put.mockImplementation(async (_path: string, patch: Record<string, unknown>) => serve(patch));
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
	await waitFor(() => expect(transport.get.mock.calls.some(([path]) => path === '/voice/providers')).toBe(true));
	return view;
}

const engine = () => screen.getByRole('combobox', { name: 'Voice engine' }) as HTMLSelectElement;
const rowOf = (el: HTMLElement) => el.closest('[data-setting-anchor]') as HTMLElement;

describe('Grok voice (SuperGrok subscription)', () => {
	it('is offered with a neutral label but is never the default', async () => {
		await mountPage();
		expect(engine().value).toBe('gpt-live-1');
		const option = Array.from(engine().options).find((o) => o.value === SUPERGROK);
		expect(option?.textContent).toBe(LABEL);
		expect(transport.put).not.toHaveBeenCalled();
	});

	it('selecting it is the opt-in: saves the engine with the old API pin cleared and offers Grok voices', async () => {
		server.voice_provider_id = 'xai-key';
		await mountPage();
		await fireEvent.change(engine(), { target: { value: SUPERGROK } });
		await waitFor(() => expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_model: SUPERGROK, voice_provider_id: null }));
		await waitFor(() => expect((screen.getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement).value).toBe('eve'));
		expect(screen.getByRole('option', { name: 'Ara' })).toBeTruthy();
		expect(screen.getByText('SuperGrok login · server VAD')).toBeTruthy();
	});

	it('shows no warning wording or warning banners', async () => {
		serve({ voice_model: SUPERGROK });
		await mountPage();
		// Other engines' picker labels are not part of this engine's presentation.
		const page = document.body.cloneNode(true) as HTMLElement;
		page.querySelectorAll('option').forEach((o) => o.remove());
		expect(page.textContent).not.toMatch(/unofficial|unsupported|not verified/i);
		expect(document.querySelector('.st-banner')).toBeNull();
	});

	it('runs calls on connected SuperGrok logins only, never on an xAI or OpenAI API key', async () => {
		serve({ voice_model: SUPERGROK });
		await mountPage();
		const link = screen.getByRole('button', { name: /Calls run on SuperGrok/ });
		expect(screen.queryByText('xAI API key')).toBeNull();
		expect(screen.queryByText('OpenAI API key')).toBeNull();
		expect(screen.queryByText('ChatGPT Plus')).toBeNull();
		await fireEvent.click(link);
		expect(settingsNav.category).toBe('providers');
	});

	it('lets the user pick between several SuperGrok logins and lists only those', async () => {
		providers.push({ id: 'grok-2', name: 'SuperGrok Heavy', provider_type: 'grok' });
		serve({ voice_model: SUPERGROK });
		await mountPage();
		const select = screen.getByRole('combobox', { name: 'Calls run on' }) as HTMLSelectElement;
		expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['SuperGrok', 'SuperGrok Heavy']);
		await fireEvent.change(select, { target: { value: 'grok-2' } });
		await waitFor(() => expect(transport.put).toHaveBeenLastCalledWith('/settings/voice', { voice_provider_id: 'grok-2' }));
	});

	it('asks to connect a SuperGrok login when none is connected, even with an xAI API key present', async () => {
		providers = providers.filter((p) => p.provider_type !== 'grok');
		serve({ voice_model: SUPERGROK });
		await mountPage();
		expect(rowOf(engine()).textContent).toContain('Needs a SuperGrok login');
		expect(screen.getByText('Connect your SuperGrok login in Settings → Providers to use this.')).toBeTruthy();
		expect(document.querySelector('.st-banner')).toBeNull();
		expect(screen.queryByText('xAI API key')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Open Providers to connect your SuperGrok login' }));
		expect(settingsNav.category).toBe('providers');
		settingsNav.reset();
		await fireEvent.click(screen.getByRole('button', { name: /Connect SuperGrok/ }));
		expect(settingsNav.category).toBe('providers');
	});

	it('hides the connect note once a login is connected', async () => {
		serve({ voice_model: SUPERGROK });
		await mountPage();
		expect(screen.queryByText(/Connect your SuperGrok login/)).toBeNull();
		expect(rowOf(engine()).textContent).not.toContain('Needs a SuperGrok login');
	});

	it('disables the sample button with the server preview note instead of requesting one', async () => {
		const module = await import('$lib/api/voice');
		const preview = vi.spyOn(module, 'previewVoice');
		try {
			serve({ voice_model: SUPERGROK });
			await mountPage();
			const button = screen.getByRole('button', { name: `Samples are not available for ${LABEL}` }) as HTMLButtonElement;
			expect(button.disabled).toBe(true);
			expect(button.title).toBe(PREVIEW_NOTE);
			await fireEvent.click(button);
			expect(preview).not.toHaveBeenCalled();
		} finally {
			preview.mockRestore();
		}
	});

	it('leaves API-key Grok Voice on xAI keys only', async () => {
		serve({ voice_model: 'grok-voice-latest' });
		await mountPage();
		expect(screen.getByRole('button', { name: /Calls run on xAI API key/ })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /Calls run on SuperGrok/ })).toBeNull();
	});
});
