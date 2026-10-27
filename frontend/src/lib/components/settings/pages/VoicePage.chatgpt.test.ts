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

const CODEX = 'gpt-live-1-codex';
const LABEL = 'GPT-Live-1.1 · ChatGPT subscription';
const UNAVAILABLE =
	"ChatGPT voice isn't ready yet. Voice components are still installing; start a call to finish setup.";
const PREVIEW_NOTE = "Uses your ChatGPT subscription login. Previews aren't available for subscription voice.";
/** openai/codex RealtimeVoicesList::builtin().v3, mirrored by app.core.voice_models.CODEX_VOICES. */
const CODEX_VOICES = ['juniper', 'maple', 'spruce', 'ember', 'vale', 'breeze', 'arbor', 'sol', 'cove', 'aube', 'bubbie', 'haetsal', 'hanul', 'himari', 'leher', 'miko', 'neer', 'porto', 'rindo', 'rio', 'rivage', 'selva', 'sonna', 'tinta', 'viola'];
const cap = (id: string) => id.charAt(0).toUpperCase() + id.slice(1);
/** No warning framing anywhere on the page for the subscription engine. */
const SCARY = /unofficial|unsupported|may stop working|refuse access/i;

type Details = Record<string, unknown>;
let server: Record<string, unknown> & { voice_model: string; voice_voice: string; model_details: Details };
let providers: { id: string; name: string; provider_type?: string }[];
let runtimeAvailable: boolean;

/** Server rule: model_details describe the CURRENTLY selected engine only. */
function serve(patch: Record<string, unknown> = {}) {
	const next = { ...server, ...patch } as typeof server;
	const codex = next.voice_model === CODEX;
	server = {
		...next,
		voice_voice: codex ? (CODEX_VOICES.includes(next.voice_voice) ? next.voice_voice : 'cove') : next.voice_voice,
		voice_options: { ...(server.voice_options as object), ...((patch.voice_options as object) ?? {}) },
		model_details: codex
			? {
					...next.model_details,
					model: CODEX,
					label: LABEL,
					available: runtimeAvailable,
					unavailable_reason: runtimeAvailable ? null : UNAVAILABLE,
					voices: CODEX_VOICES.map((id) => ({ id, label: cap(id) })),
					preview_note: PREVIEW_NOTE
				}
			: { ...next.model_details, model: next.voice_model }
	};
	return structuredClone(server);
}

beforeEach(() => {
	transport.get.mockReset();
	transport.put.mockReset();
	identity.epoch++;
	for (const listener of identity.listeners) listener();
	runtimeAvailable = true;
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
		voice_options: { instructions: '', delegation_instructions: '', mic_threshold_db: -42 },
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
		{ id: 'chatgpt-login', name: 'ChatGPT Plus', provider_type: 'chatgpt' }
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

describe('GPT-Live-1.1 · ChatGPT subscription', () => {
	it('is offered in the picker but never the default', async () => {
		await mountPage();
		expect(engine().value).toBe('gpt-live-1');
		expect(Array.from(engine().options).map((o) => [o.value, o.textContent])).toEqual([
			['gpt-live-1', 'GPT Live 1'],
			['grok-voice-latest', 'Grok Voice'],
			['grok-voice-supergrok', 'Grok voice (SuperGrok subscription)'],
			[CODEX, LABEL]
		]);
		expect(transport.put).not.toHaveBeenCalled();
	});

	it('selecting it is the opt-in: saves the engine with the old API pin cleared and uses its own voices', async () => {
		server.voice_provider_id = 'openai-key';
		await mountPage();
		await fireEvent.change(engine(), { target: { value: CODEX } });
		await waitFor(() => expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_model: CODEX, voice_provider_id: null }));
		await waitFor(() => expect((screen.getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement).value).toBe('cove'));
		expect(screen.getByRole('option', { name: 'Cove' })).toBeTruthy();
		expect(screen.queryByRole('option', { name: 'marin' })).toBeNull();
		expect(screen.getByText('ChatGPT subscription login · server VAD')).toBeTruthy();
		expect(screen.getByText("Doesn't fall back to an API key")).toBeTruthy();
		// Connected and ready: no note under the engine, and nothing styled as a warning.
		expect(document.querySelector('.chatgpt-help')).toBeNull();
		expect(document.querySelector('.st-banner')).toBeNull();
		expect(document.body.textContent).not.toMatch(SCARY);
	});

	it('runs calls on connected ChatGPT subscription logins only, never on API keys', async () => {
		serve({ voice_model: CODEX });
		await mountPage();
		const link = screen.getByRole('button', { name: /Calls run on ChatGPT Plus/ });
		expect(link).toBeTruthy();
		expect(screen.queryByText('OpenAI API key')).toBeNull();
		expect(screen.queryByText('xAI API key')).toBeNull();
		await fireEvent.click(link);
		expect(settingsNav.category).toBe('providers');
	});

	it('lets the user pick between several ChatGPT logins and lists only those', async () => {
		providers.push({ id: 'chatgpt-2', name: 'ChatGPT Pro', provider_type: 'chatgpt' });
		serve({ voice_model: CODEX });
		await mountPage();
		const select = screen.getByRole('combobox', { name: 'Calls run on' }) as HTMLSelectElement;
		expect(Array.from(select.options).map((o) => o.textContent)).toEqual(['ChatGPT Plus', 'ChatGPT Pro']);
		await fireEvent.change(select, { target: { value: 'chatgpt-2' } });
		await waitFor(() => expect(transport.put).toHaveBeenLastCalledWith('/settings/voice', { voice_provider_id: 'chatgpt-2' }));
	});

	it('says a ChatGPT login is needed when none is connected, even with API keys present', async () => {
		providers = providers.filter((p) => p.provider_type !== 'chatgpt');
		serve({ voice_model: CODEX });
		await mountPage();
		const connect = await screen.findAllByRole('button', { name: /Connect ChatGPT/ });
		expect(rowOf(engine()).textContent).toContain('Needs a ChatGPT login');
		expect(screen.getByText(/Connect your ChatGPT login in Settings → Providers to use this\./)).toBeTruthy();
		expect(document.querySelector('.st-banner')).toBeNull();
		expect(document.body.textContent).not.toMatch(SCARY);
		await fireEvent.click(connect[0]);
		expect(settingsNav.category).toBe('providers');
		expect(screen.queryByText('OpenAI API key')).toBeNull();
	});

	it('shows the sanitized unavailable reason and a saved selection, not a traceback', async () => {
		runtimeAvailable = false;
		serve({ voice_model: CODEX });
		await mountPage();
		expect(engine().value).toBe(CODEX);
		const note = screen.getAllByRole('status').find((el) => el.textContent?.includes(UNAVAILABLE));
		expect(note).toBeTruthy();
		expect(note?.closest('.chatgpt-help')).toBeTruthy();
		expect(document.querySelector('.st-banner')).toBeNull();
		expect(rowOf(engine()).textContent).toContain('Not ready yet');
		expect(document.body.textContent).not.toMatch(/on this system|glibc|Traceback|Error:|ImportError|OSError/);
		expect(document.body.textContent).not.toMatch(SCARY);
	});

	it('does not show the unavailable notice while the runtime is available', async () => {
		serve({ voice_model: CODEX });
		await mountPage();
		expect(document.body.textContent).not.toContain('still installing');
		expect(rowOf(engine()).textContent).not.toContain('Not ready yet');
	});

	it('disables the sample button with the server preview note instead of requesting one', async () => {
		const module = await import('$lib/api/voice');
		const preview = vi.spyOn(module, 'previewVoice');
		try {
			serve({ voice_model: CODEX });
			await mountPage();
			const button = screen.getByRole('button', { name: `Samples are not available for ${LABEL}` }) as HTMLButtonElement;
			expect(button.disabled).toBe(true);
			expect(button.title).toBe(PREVIEW_NOTE);
			expect(rowOf(button).textContent).toContain('No samples for this engine');
			await fireEvent.click(button);
			expect(preview).not.toHaveBeenCalled();
		} finally {
			preview.mockRestore();
		}
	});

	it('lists all 25 ChatGPT voices and saves the one picked', async () => {
		serve({ voice_model: CODEX });
		await mountPage();
		const voice = screen.getByRole('combobox', { name: 'Voice' }) as HTMLSelectElement;
		expect(Array.from(voice.options).map((o) => [o.value, o.textContent])).toEqual(CODEX_VOICES.map((id) => [id, cap(id)]));
		expect(voice.value).toBe('cove');
		await fireEvent.change(voice, { target: { value: 'juniper' } });
		await waitFor(() => expect(transport.put).toHaveBeenLastCalledWith('/settings/voice', { voice_voice: 'juniper' }));
		await waitFor(() => expect(voice.value).toBe('juniper'));
	});

	it('saves the microphone gate for this engine and offers no opening words', async () => {
		serve({ voice_model: CODEX });
		await mountPage();
		expect(screen.queryByRole('textbox', { name: 'Opening words' })).toBeNull();
		const gate = screen.getByRole('spinbutton', { name: 'Microphone gate' }) as HTMLInputElement;
		expect(gate.value).toBe('-42');
		await fireEvent.input(gate, { target: { value: '-55' } });
		await fireEvent.blur(gate);
		await waitFor(() =>
			expect(transport.put).toHaveBeenLastCalledWith('/settings/voice', { voice_options: { mic_threshold_db: -55 }, voice_options_model: CODEX })
		);
		await waitFor(() => expect((screen.getByRole('spinbutton', { name: 'Microphone gate' }) as HTMLInputElement).value).toBe('-55'));
	});

	it('offers exactly the engines the server lists, including GPT-Live-1.1 when available', async () => {
		server.voice_engines = [
			{ id: 'gpt-live-1', label: 'GPT Live 1', funding: 'openai' },
			{ id: 'gpt-live-1.1', label: 'GPT-Live-1.1', funding: 'openai' },
			{ id: 'grok-voice-latest', label: 'Grok Voice', funding: 'xai' },
			{ id: CODEX, label: LABEL, funding: 'chatgpt' }
		];
		await mountPage();
		expect(Array.from(engine().options).map((o) => o.value)).toEqual(['gpt-live-1', 'gpt-live-1.1', 'grok-voice-latest', CODEX]);
		await fireEvent.change(engine(), { target: { value: 'gpt-live-1.1' } });
		await waitFor(() => expect(transport.put).toHaveBeenCalledExactlyOnceWith('/settings/voice', { voice_model: 'gpt-live-1.1', voice_provider_id: null }));
		await waitFor(() => expect(engine().value).toBe('gpt-live-1.1'));
		expect(screen.getByRole('button', { name: /Calls run on OpenAI API key/ })).toBeTruthy();
		expect((screen.getByRole('button', { name: 'Play marin sample' }) as HTMLButtonElement).disabled).toBe(false);
	});

	it('leaves the default engine unchanged: samples enabled, API-key access, OpenAI providers only', async () => {
		await mountPage();
		expect((screen.getByRole('button', { name: 'Play marin sample' }) as HTMLButtonElement).disabled).toBe(false);
		expect(screen.getAllByText('OpenAI API key').length).toBeGreaterThan(0);
		expect(screen.queryByRole('button', { name: /ChatGPT/ })).toBeNull();
		expect(screen.getByRole('button', { name: /Calls run on OpenAI API key/ })).toBeTruthy();
	});
});
