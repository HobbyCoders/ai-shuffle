/**
 * Plays a short spoken sample of a text-to-speech voice through the same
 * endpoint chat read-aloud uses (POST /settings/tts/stream). One sample at a
 * time; playing again stops it.
 */
import { apiBase, withAuth } from '$lib/api/runtime';
import { notify } from '$lib/stores/notifications';

const SAMPLE_TEXT = 'Hi, this is how I sound when I read your messages aloud.';

export class TtsSample {
	/** Voice being fetched or played, or null. */
	active = $state<string | null>(null);
	loading = $state(false);

	#audio: HTMLAudioElement | null = null;
	#url: string | null = null;
	#abort: AbortController | null = null;

	async play(opts: { model: string; voice: string; speed?: number }): Promise<void> {
		const same = this.active === opts.voice;
		this.stop();
		if (same) return;
		this.active = opts.voice;
		this.loading = true;
		const abort = new AbortController();
		this.#abort = abort;
		try {
			const res = await fetch(
				`${apiBase()}/settings/tts/stream`,
				withAuth({
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({
						text: SAMPLE_TEXT,
						model: opts.model,
						voice: opts.voice,
						speed: opts.speed,
						output_format: 'mp3'
					}),
					signal: abort.signal
				})
			);
			if (!res.ok) throw new Error(`Sample failed (${res.status})`);
			const blob = await res.blob();
			if (abort.signal.aborted) return;
			this.#url = URL.createObjectURL(blob);
			const audio = new Audio(this.#url);
			this.#audio = audio;
			audio.onended = () => this.stop();
			audio.onerror = () => this.stop();
			this.loading = false;
			await audio.play();
		} catch (e) {
			if (abort.signal.aborted) return;
			this.stop();
			notify.error(e instanceof Error ? e.message : 'Could not play the sample');
		}
	}

	stop(): void {
		this.#abort?.abort();
		this.#abort = null;
		if (this.#audio) {
			this.#audio.pause();
			this.#audio.onended = null;
			this.#audio.onerror = null;
			this.#audio = null;
		}
		if (this.#url) URL.revokeObjectURL(this.#url);
		this.#url = null;
		this.active = null;
		this.loading = false;
	}
}
