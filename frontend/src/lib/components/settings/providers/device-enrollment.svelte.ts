/**
 * Device-code sign-in for the ChatGPT (Codex) and SuperGrok subscriptions.
 *
 * Two modes, fixed at construction:
 *   - DRAFT (no `existingProviderId`): creates a placeholder `model_providers`
 *     row and OWNS it. Cancel and "Remove draft" delete that row, so a bailed
 *     sign-in never leaves an orphan.
 *   - RECONNECT (`existingProviderId`): signs an existing row in again. It never
 *     creates a row, and bail paths never delete it.
 *
 * Phases:
 *   create    → create the placeholder row (draft mode only).
 *   starting  → `device/start` → a fresh user code every time.
 *   awaiting  → show the code; poll every 3s (5s after `slow_down`) until
 *               `complete`. setTimeout recursion, so polls never overlap.
 *   finishing → seed models when nobody picks them (never over a curated
 *               list on reconnect), refresh the providers store (which
 *               re-hydrates the model registry), then `onConnected` or `onDone`.
 *   error     → "Try again" restarts `device/start` on the SAME row (a consumed
 *               enrollment is never re-polled), or retries the create.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	codexDevicePoll,
	codexDeviceStart,
	createModelProvider,
	deleteModelProvider,
	updateModelProvider,
	xaiDevicePoll,
	xaiDeviceStart,
	type CodexDevicePoll,
	type CodexDeviceStart
} from '$lib/api/model-providers';
import { providers } from '$lib/stores/providers.svelte';

export type DeviceKind = 'codex' | 'xai';
export type DevicePhase = 'idle' | 'create' | 'starting' | 'awaiting' | 'finishing' | 'error';

export interface DeviceEnrollmentOptions {
	kind: DeviceKind;
	/** Reconnect this row instead of creating a draft. */
	existingProviderId?: string | null;
	/**
	 * Draft mode resuming a placeholder row an earlier view of this draft
	 * created: no second create, and Cancel still deletes it.
	 */
	draftRowId?: string | null;
	/** A new subscription continues to model selection; nothing is seeded. */
	onConnected?: (providerId: string) => void;
	/** Sign-in finished (reconnect, or a draft without a picker). */
	onDone?: () => void;
	/** Cancelled; a draft row is already deleted. */
	onCancel?: () => void;
	/** Busy across create/start/await/finish; false once the user must choose. */
	onBusyChange?: (busy: boolean) => void;
}

interface KindConfig {
	label: string;
	draft: { provider_type: string; name: string; base_url: string };
	start: (id: string) => Promise<CodexDeviceStart>;
	poll: (id: string) => Promise<CodexDevicePoll>;
}

export const DEVICE_KINDS: Record<DeviceKind, KindConfig> = {
	codex: {
		label: 'ChatGPT',
		draft: { provider_type: 'chatgpt', name: 'ChatGPT', base_url: 'https://chatgpt.com/backend-api/codex' },
		start: codexDeviceStart,
		poll: codexDevicePoll
	},
	xai: {
		label: 'Grok',
		draft: { provider_type: 'grok', name: 'Grok', base_url: 'https://api.x.ai/v1' },
		start: xaiDeviceStart,
		poll: xaiDevicePoll
	}
};

export class DeviceEnrollment {
	phase = $state<DevicePhase>('idle');
	userCode = $state('');
	verificationUri = $state('');
	errorMessage = $state('');
	/** The row being signed in, once known. */
	providerId = $state<string | null>(null);

	readonly kind: DeviceKind;
	/** True in draft mode: this enrollment created its row and deletes it on bail. */
	readonly ownsRow: boolean;

	#options: DeviceEnrollmentOptions;
	#config: KindConfig;
	#pollIntervalMs = 3000;
	#pollTimer: ReturnType<typeof setTimeout> | null = null;
	#disposed = false;
	#lastBusy = false;

	constructor(options: DeviceEnrollmentOptions) {
		this.#options = options;
		this.kind = options.kind;
		this.#config = DEVICE_KINDS[options.kind];
		this.ownsRow = !options.existingProviderId;
		const id = options.existingProviderId ?? options.draftRowId ?? null;
		if (id) this.providerId = id;
	}

	get label(): string {
		return this.#config.label;
	}

	get busy(): boolean {
		return this.phase !== 'error' && this.phase !== 'idle';
	}

	/** Begin: create the draft (draft mode) or go straight to device/start. */
	start(): void {
		if (this.providerId) void this.#startDevice();
		else void this.#createDraft();
	}

	/** Retry on the same row with a fresh device/start; retry the create when there is no row. */
	tryAgain(): void {
		if (this.providerId) void this.#startDevice();
		else void this.#createDraft();
	}

	/**
	 * Hand control back. Draft mode deletes the placeholder row (best-effort);
	 * reconnect mode leaves the enrolled row untouched.
	 */
	async cancel(): Promise<void> {
		this.#clearPollTimer();
		if (!this.ownsRow) {
			this.#setBusy(false);
			this.#options.onCancel?.();
			return;
		}
		this.phase = 'finishing';
		this.#setBusy(true);
		const id = this.providerId;
		this.providerId = null;
		if (id) {
			try {
				await deleteModelProvider(id);
			} catch {
				// Best-effort; the caller still returns.
			}
		}
		if (this.#disposed) return;
		this.#setBusy(false);
		this.#options.onCancel?.();
	}

	dispose(): void {
		this.#disposed = true;
		this.#clearPollTimer();
	}

	#setBusy(busy: boolean): void {
		if (busy === this.#lastBusy) return;
		this.#lastBusy = busy;
		this.#options.onBusyChange?.(busy);
	}

	#clearPollTimer(): void {
		if (this.#pollTimer !== null) {
			clearTimeout(this.#pollTimer);
			this.#pollTimer = null;
		}
	}

	async #createDraft(): Promise<void> {
		this.phase = 'create';
		this.errorMessage = '';
		this.#setBusy(true);
		try {
			const row = await createModelProvider({ ...this.#config.draft });
			if (this.#disposed) return;
			this.providerId = row.id;
			await this.#startDevice();
		} catch (e) {
			if (this.#disposed) return;
			this.#fail(getErrorMessage(e, `Could not create the ${this.label} provider.`));
		}
	}

	async #startDevice(): Promise<void> {
		const id = this.providerId;
		if (!id) return;
		this.phase = 'starting';
		this.errorMessage = '';
		this.#setBusy(true);
		try {
			const res = await this.#config.start(id);
			if (this.#disposed) return;
			this.userCode = res.user_code;
			this.verificationUri = res.verification_uri;
			this.#pollIntervalMs = 3000;
			this.phase = 'awaiting';
			this.#scheduleNextPoll();
		} catch (e) {
			if (this.#disposed) return;
			this.#fail(getErrorMessage(e, `Could not start ${this.label} sign-in.`));
		}
	}

	#scheduleNextPoll(): void {
		this.#clearPollTimer();
		this.#pollTimer = setTimeout(() => void this.#runPoll(), this.#pollIntervalMs);
	}

	async #runPoll(): Promise<void> {
		const id = this.providerId;
		if (this.#disposed || !id || this.phase !== 'awaiting') return;
		try {
			const res = await this.#config.poll(id);
			// A cancel or teardown may have fired while the request was in flight.
			if (this.#disposed || this.phase !== 'awaiting') return;
			if (res.status === 'complete') {
				await this.#finish();
				return;
			}
			if (res.status === 'slow_down') this.#pollIntervalMs = 5000;
			this.#scheduleNextPoll();
		} catch (e) {
			if (this.#disposed) return;
			// Terminal poll error (HTTP 400): the enrollment is consumed.
			this.#fail(getErrorMessage(e, `${this.label} sign-in failed. Please try again.`));
		}
	}

	async #finish(): Promise<void> {
		this.#clearPollTimer();
		this.phase = 'finishing';
		this.#setBusy(true);
		if (!this.#options.onConnected) await this.#seedModels();
		try {
			await providers.refreshProviders();
		} catch {
			// The row is enrolled regardless; a refresh hiccup must not block.
		}
		if (this.#disposed) return;
		this.#setBusy(false);
		this.phase = 'idle';
		const id = this.providerId;
		if (this.#options.onConnected && id) this.#options.onConnected(id);
		else this.#options.onDone?.();
	}

	/**
	 * Without a picker, seed from the live catalog (best-effort). A reconnect
	 * never clobbers a row that already has a curated model list.
	 */
	async #seedModels(): Promise<void> {
		const id = this.providerId;
		if (!id) return;
		if (!this.ownsRow) {
			const row = providers.current?.providers?.find((p) => p.id === id);
			if (row && row.models.length > 0) return;
		}
		try {
			const fetched = await providers.fetchProviderModels(id);
			await updateModelProvider(id, { models: fetched.models ?? [] });
		} catch {
			// Best-effort; the row is enrolled regardless.
		}
	}

	#fail(message: string): void {
		this.#clearPollTimer();
		this.errorMessage = message;
		this.phase = 'error';
		this.#setBusy(false);
	}
}
