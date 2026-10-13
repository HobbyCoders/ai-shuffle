/**
 * Voice page state: the server's voice settings, the providers that can fund
 * a call, and the voice sample player.
 *
 * Ported from the old VoiceSection, with the same rules:
 *
 * - `loadVoiceSettings` is a shared, cached read; a settings resource change
 *   or an account boundary invalidates it, and `refresh()` re-reads it.
 * - A refresh never lands under a save in flight, and an older read never
 *   overwrites a newer one (`#request` generations).
 * - Every write is a partial PUT (`saveVoiceSettings`); the response is the
 *   settled object. A failed write keeps the last good settings.
 * - An editor that outlived an account change refuses to write: the owner
 *   epoch is captured when the page mounts.
 * - Free-text fields hold their own draft while focused (NumberField,
 *   TextField), so a refresh never snaps a half-typed value back.
 */
import { api } from '$lib/api/client';
import {
	loadVoiceSettings,
	previewVoice,
	saveVoiceSettings,
	type VoiceSettings,
	type VoiceSettingsUpdate
} from '$lib/api/voice';
import { isForbidden } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';
import { sessionOwnerEpoch } from '$lib/stores/session-owner';

/** `app.core.voice_models.FAL_PROVIDER_ID`: fal.ai funds media voices only, never GPT Live calls. */
export const FAL_VOICE_PROVIDER_ID = '__fal_voice__';

export interface VoiceProvider {
	id: string;
	name: string;
}

export type VoiceLoad = 'loading' | 'ok' | 'forbidden' | 'error';

function message(err: unknown, fallback: string): string {
	if (typeof err === 'object' && err !== null) {
		const detail = (err as { detail?: unknown }).detail ?? (err as { message?: unknown }).message;
		if (typeof detail === 'string' && detail) return detail;
	}
	return fallback;
}

export class VoiceState {
	settings = $state<VoiceSettings | null>(null);
	status = $state<VoiceLoad>('loading');
	/** Last load or save failure, shown under the page's groups. */
	error = $state('');
	saving = $state(false);
	/** OpenAI providers that can fund GPT Live calls; null until loaded or when the read failed. */
	providers = $state<VoiceProvider[] | null>(null);

	readonly #owner = sessionOwnerEpoch();
	#alive = true;
	#request = 0;

	/** False once the page unmounted or the signed-in account changed. */
	current(): boolean {
		return this.#alive && this.#owner === sessionOwnerEpoch();
	}

	/** The model's voices, only when the details describe the active model. */
	get details() {
		const s = this.settings;
		return s?.model_details && s.model_details.model === s.voice_model ? s.model_details : undefined;
	}

	async refresh(): Promise<void> {
		const generation = ++this.#request;
		try {
			const value = await loadVoiceSettings();
			if (this.current() && generation === this.#request && !this.saving) {
				this.settings = value;
				this.status = 'ok';
				this.error = '';
			}
		} catch (err) {
			if (!this.current() || generation !== this.#request) return;
			if (isForbidden(err)) this.status = 'forbidden';
			else if (!this.settings) {
				this.status = 'error';
				this.error = message(err, 'Could not load voice settings.');
			}
		}
	}

	async loadProviders(): Promise<void> {
		try {
			const rows = await api.get<VoiceProvider[]>('/voice/providers');
			if (!this.current()) return;
			this.providers = (rows ?? []).filter((p) => p.id !== FAL_VOICE_PROVIDER_ID);
		} catch {
			if (this.current()) this.providers = null;
		}
	}

	/** Partial update. Resolves true when the server accepted it. */
	async save(patch: VoiceSettingsUpdate): Promise<boolean> {
		if (!this.current()) return false;
		++this.#request;
		this.saving = true;
		this.error = '';
		try {
			const updated = await saveVoiceSettings(patch);
			if (!this.current()) return false;
			this.settings = updated;
			flashSaved();
			return true;
		} catch (err) {
			if (this.current()) {
				this.error = message(err, 'Could not save voice settings.');
			}
			return false;
		} finally {
			if (this.current()) {
				this.saving = false;
				void this.refresh();
			}
		}
	}

	destroy(): void {
		this.#alive = false;
		++this.#request;
	}
}

/**
 * Plays a short sample of a GPT Live voice. One sample at a time; each
 * downloaded sample is reused until the page unmounts. Playing never changes
 * the selected voice.
 */
export class VoicePreview {
	/** Voice id playing or loading. */
	playing = $state<string | null>(null);
	loading = $state(false);
	error = $state('');

	#abort: AbortController | null = null;
	#audio: HTMLAudioElement | null = null;
	#samples = new Map<string, string>();
	#alive = true;
	readonly #owner = sessionOwnerEpoch();

	#current(): boolean {
		return this.#alive && this.#owner === sessionOwnerEpoch();
	}

	stop(): void {
		this.#abort?.abort();
		this.#abort = null;
		this.#audio?.pause();
		this.#audio = null;
		this.playing = null;
		this.loading = false;
	}

	/** Play `voice`, or stop it when it is the one playing. */
	async toggle(voice: string): Promise<void> {
		const stopping = this.playing === voice;
		this.stop();
		this.error = '';
		if (stopping) return;
		const controller = new AbortController();
		this.#abort = controller;
		this.playing = voice;
		// Create the player inside the user gesture; reuse downloaded samples.
		const player = new Audio();
		this.#audio = player;
		try {
			let url = this.#samples.get(voice);
			if (!url) {
				this.loading = true;
				const blob = await previewVoice(voice, controller.signal);
				if (!this.#current() || controller.signal.aborted) return;
				url = URL.createObjectURL(blob);
				this.#samples.set(voice, url);
			}
			if (!this.#current() || controller.signal.aborted) return;
			this.loading = false;
			player.src = url;
			player.onended = () => {
				if (this.#audio === player) this.stop();
			};
			player.onerror = () => {
				if (this.#audio !== player) return;
				this.stop();
				this.error = 'Could not play this sample. Try again.';
			};
			await player.play();
		} catch (err) {
			if (controller.signal.aborted || !this.#current()) return;
			this.stop();
			this.error = message(err, 'Voice preview failed. Try again.');
		}
	}

	destroy(): void {
		this.#alive = false;
		this.stop();
		for (const url of this.#samples.values()) URL.revokeObjectURL(url);
		this.#samples.clear();
	}
}
