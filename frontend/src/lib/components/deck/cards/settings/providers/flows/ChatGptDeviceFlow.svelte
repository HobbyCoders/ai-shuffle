<script lang="ts">
	/**
	 * ChatGptDeviceFlow — Codex/ChatGPT-subscription device-code enrollment.
	 *
	 * Chunk 6 of the Providers UI restructure (plan §1.3). Rendered by the parent
	 * `AddProviderModal` for the `chatgpt-sub` step; it owns ONLY the device flow
	 * and talks to a fixed props contract so the modal can drive it blindly.
	 *
	 * TWO modes, selected by the `existingProviderId` prop:
	 *   • DRAFT (default, `existingProviderId` absent): CREATES a placeholder
	 *     `chatgpt` row on mount and OWNS it — bail paths delete the row.
	 *   • RECONNECT (`existingProviderId` set): reuses an existing row (e.g. the
	 *     Providers-list "Reconnect" button); mount skips `createDraft` and starts
	 *     device/start directly, and bail paths NEVER delete the row.
	 *
	 * State machine (internal `phase`):
	 *   create    → create a placeholder `chatgpt` provider row, hold `providerId`.
	 *   starting  → `codexDeviceStart(providerId)`; "Preparing sign-in…".
	 *   awaiting  → show `user_code` + verification link; poll `codexDevicePoll`
	 *               every 3s (widen to 5s on `slow_down`) until `complete`.
	 *   finishing → `providers.refreshProviders()` (re-hydrates the model
	 *               registry), then `onDone()`.
	 *   error     → inline `role="alert"` + "Try again" (fresh `device/start` on
	 *               the SAME row, per ruling PR7 — a consumed enrollment is never
	 *               re-polled) + "Remove draft" (delete the placeholder, `onCancel`).
	 *
	 * Cancel/close from `awaiting` also deletes the draft so a bailed enrollment
	 * never leaves an orphan `model_providers` row (same path as "Remove draft").
	 *
	 * Design decisions (stated for the reviewer):
	 *   • MOUNT-TIME auto-start. Arriving at this component IS the user's "start"
	 *     action — they already chose the ChatGPT card in the parent chooser — so
	 *     `create → starting → awaiting` runs automatically on mount rather than
	 *     behind an extra "Start" click. Bail paths clean up the draft row.
	 *   • BUSY = true across create/starting/awaiting/finishing (the whole
	 *     uninterruptible enrollment), false only in `error` where the user must
	 *     choose. This matches the plan's "guarded while submitting or
	 *     mid-device-poll" — the parent guards its Esc/backdrop while we're busy;
	 *     graceful teardown is via this component's own Cancel affordance.
	 *   • setTimeout recursion (not setInterval) so the interval can widen on
	 *     `slow_down` and polls never overlap; a single `pollTimer` handle is
	 *     cleared on unmount (mirrors setup's `onDestroy` clearInterval discipline).
	 *
	 * L3-safe: uses only theme-token Tailwind utilities (McpServerForm precedent),
	 * never the shell-scoped `.ed-content` globals — it renders inside the modal.
	 *
	 * Endpoints/fields verified against `app/api/model_providers.py`:
	 *   • POST /model-providers                      → { id, … } (row we hold)
	 *   • POST /model-providers/{id}/codex/device/start → { user_code, verification_uri }
	 *   • POST /model-providers/{id}/codex/device/poll  → { status: 'pending'
	 *       | 'slow_down' | 'complete', provider? } (terminal failure = HTTP 400)
	 *   • DELETE /model-providers/{id}               → draft cleanup
	 */
	import { onMount, onDestroy } from 'svelte';
	import { Loader2, Copy, Check, ExternalLink, AlertCircle } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		createModelProvider,
		deleteModelProvider,
		updateModelProvider,
		codexDeviceStart,
		codexDevicePoll
	} from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		/** Enrollment complete → parent closes the modal. */
		onDone: () => void;
		/** Parent returns to its chooser step (draft already cleaned up here). */
		onCancel: () => void;
		/** Report while any request/poll is in flight (guards the parent close). */
		onBusyChange?: (busy: boolean) => void;
		/**
		 * RECONNECT mode: reuse this existing provider row instead of creating a
		 * draft. When set, mount skips `createDraft` and bail paths never delete the
		 * row. Absent/undefined = DRAFT mode (the original create-and-own behavior).
		 */
		existingProviderId?: string | null;
	}

	let { onDone, onCancel, onBusyChange, existingProviderId }: Props = $props();

	// Draft mode owns (creates + deletes) the row; reconnect mode does not.
	// A non-reactive const is fine — props don't change post-mount here.
	const ownsRow = !existingProviderId;

	type Phase = 'create' | 'starting' | 'awaiting' | 'finishing' | 'error';

	let phase = $state<Phase>('create');
	let userCode = $state('');
	let verificationUri = $state('');
	let errorMessage = $state('');
	let copied = $state(false);

	// Non-reactive latches (never rendered directly).
	let providerId: string | null = null;
	let pollIntervalMs = 3000;
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let copyTimer: ReturnType<typeof setTimeout> | null = null;
	let destroyed = false;
	let lastBusy = false;

	// ------------------------------------------------------------------------
	// Busy reporting (deduped so the parent isn't spammed on every poll tick)
	// ------------------------------------------------------------------------
	function setBusy(busy: boolean) {
		if (busy === lastBusy) return;
		lastBusy = busy;
		onBusyChange?.(busy);
	}

	function clearPollTimer() {
		if (pollTimer !== null) {
			clearTimeout(pollTimer);
			pollTimer = null;
		}
	}

	// ------------------------------------------------------------------------
	// Flow
	// ------------------------------------------------------------------------

	/** create → hold providerId → start. */
	async function createDraft() {
		phase = 'create';
		errorMessage = '';
		setBusy(true);
		try {
			const row = await createModelProvider({
				provider_type: 'chatgpt',
				name: 'ChatGPT',
				base_url: 'https://chatgpt.com/backend-api/codex'
			});
			if (destroyed) return;
			providerId = row.id;
			await startDevice();
		} catch (e) {
			if (destroyed) return;
			// No providerId yet — "Try again" retries the create from scratch.
			fail(getErrorMessage(e, 'Could not create the ChatGPT provider.'));
		}
	}

	/** starting → device/start → awaiting (fresh user_code every time, PR7). */
	async function startDevice() {
		if (!providerId) return;
		phase = 'starting';
		errorMessage = '';
		setBusy(true);
		try {
			const res = await codexDeviceStart(providerId);
			if (destroyed) return;
			userCode = res.user_code;
			verificationUri = res.verification_uri;
			pollIntervalMs = 3000;
			phase = 'awaiting';
			scheduleNextPoll();
		} catch (e) {
			if (destroyed) return;
			fail(getErrorMessage(e, 'Could not start ChatGPT sign-in.'));
		}
	}

	function scheduleNextPoll() {
		clearPollTimer();
		pollTimer = setTimeout(runPoll, pollIntervalMs);
	}

	async function runPoll() {
		if (destroyed || !providerId || phase !== 'awaiting') return;
		try {
			const res = await codexDevicePoll(providerId);
			// A cancel/teardown may have fired while the request was in flight.
			if (destroyed || phase !== 'awaiting') return;
			if (res.status === 'complete') {
				await finish();
				return;
			}
			if (res.status === 'slow_down') {
				pollIntervalMs = 5000;
			}
			scheduleNextPoll();
		} catch (e) {
			if (destroyed) return;
			// Terminal poll error (HTTP 400): the server-side enrollment is
			// consumed (PR7). Surface the error; "Try again" restarts device/start.
			fail(getErrorMessage(e, 'ChatGPT sign-in failed. Please try again.'));
		}
	}

	/** complete → seed the preset roster → refresh list + registry → onDone. */
	async function finish() {
		clearPollTimer();
		phase = 'finishing';
		setBusy(true);
		await seedModels();
		try {
			await providers.refreshProviders();
		} catch {
			// The row is enrolled regardless; a refresh hiccup must not block close.
		}
		if (destroyed) return;
		setBusy(false);
		onDone();
	}

	/**
	 * Auto-seed the ChatGPT provider's model roster on completion (mirrors
	 * OpenAiKeyFlow): fetch its live Codex catalog and persist the FULL returned list. Best-effort: a fetch/persist
	 * hiccup must not block the enrollment from completing.
	 *
	 * RECONNECT exception: never clobber a curated subset. When reconnecting an
	 * existing row that already carries models, skip seeding entirely.
	 */
	async function seedModels() {
		if (!providerId) return;
		if (!ownsRow && existingHasModels()) return;
		try {
			const fetched = await providers.fetchProviderModels(providerId);
			await updateModelProvider(providerId, { models: fetched.models ?? [] });
		} catch {
			// Best-effort — the row is enrolled regardless of a seeding failure.
		}
	}

	/** Whether the existing (reconnect) row already carries a curated model list. */
	function existingHasModels(): boolean {
		const row = providers.current?.providers?.find((p) => p.id === providerId);
		return !!row && row.models.length > 0;
	}

	function fail(message: string) {
		clearPollTimer();
		errorMessage = message;
		phase = 'error';
		setBusy(false);
	}

	// ------------------------------------------------------------------------
	// User actions
	// ------------------------------------------------------------------------

	/** PR7: retry against the SAME row via a fresh device/start; if the create
	 *  itself failed (no row yet), retry the create. */
	function tryAgain() {
		if (providerId) {
			startDevice();
		} else {
			createDraft();
		}
	}

	/** Hand control back to the parent. In DRAFT mode this deletes the placeholder
	 *  row (best-effort) so no orphan is left behind — serving both the awaiting
	 *  "Cancel" and error "Remove draft" affordances. In RECONNECT mode the row is
	 *  a real, enrolled provider, so it is left untouched. */
	async function removeDraft() {
		clearPollTimer();
		if (!ownsRow) {
			// RECONNECT: never delete an existing provider row on cancel/close.
			setBusy(false);
			onCancel();
			return;
		}
		phase = 'finishing';
		setBusy(true);
		const id = providerId;
		providerId = null;
		if (id) {
			try {
				await deleteModelProvider(id);
			} catch {
				// Best-effort — the parent still returns to the chooser.
			}
		}
		if (destroyed) return;
		setBusy(false);
		onCancel();
	}

	async function copyCode() {
		if (!userCode) return;
		try {
			await navigator.clipboard.writeText(userCode);
			copied = true;
			if (copyTimer !== null) clearTimeout(copyTimer);
			copyTimer = setTimeout(() => {
				copied = false;
			}, 2000);
		} catch {
			// Clipboard blocked (permissions/insecure context) — the code is
			// select-all-able, so this is a silent no-op.
		}
	}

	onMount(() => {
		if (existingProviderId) {
			// RECONNECT: reuse the existing row, skip create, go straight to device/start.
			providerId = existingProviderId;
			startDevice();
		} else {
			createDraft();
		}
	});

	onDestroy(() => {
		destroyed = true;
		clearPollTimer();
		if (copyTimer !== null) clearTimeout(copyTimer);
	});

	const primaryBtn =
		'inline-flex items-center justify-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50';
	const secondaryBtn =
		'inline-flex items-center justify-center gap-2 rounded-lg border border-border bg-background px-4 py-2 text-sm font-medium text-foreground transition-colors hover:bg-muted disabled:opacity-50';
</script>

<div class="space-y-4">
	{#if phase === 'create' || phase === 'starting' || phase === 'finishing'}
		<div class="flex flex-col items-center justify-center gap-3 py-8 text-center">
			<Loader2 size={24} strokeWidth={2} class="animate-spin text-muted-foreground" />
			<p class="text-sm text-muted-foreground">
				{phase === 'finishing' ? 'Finishing sign-in…' : 'Preparing sign-in…'}
			</p>
		</div>
	{:else if phase === 'awaiting'}
		<p class="text-sm text-muted-foreground">
			Sign in with your ChatGPT account to connect Codex. Enter the code below on the sign-in
			page.
		</p>
		<div class="space-y-3 rounded-lg border border-border bg-muted/30 p-4">
			<p class="text-center text-sm text-muted-foreground">Enter this code to sign in:</p>
			<div class="flex items-center justify-center gap-3">
				<code
					class="select-all font-mono text-2xl font-bold tracking-widest text-foreground"
				>
					{userCode}
				</code>
				<button
					type="button"
					onclick={copyCode}
					class="inline-flex items-center gap-1 rounded-lg border border-border bg-background px-2 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
				>
					{#if copied}
						<Check size={12} strokeWidth={2} />
						Copied
					{:else}
						<Copy size={12} strokeWidth={1.5} />
						Copy
					{/if}
				</button>
			</div>
			<a
				href={verificationUri}
				target="_blank"
				rel="noopener"
				class="{secondaryBtn} w-full py-2"
			>
				<ExternalLink size={14} strokeWidth={1.5} />
				Open sign-in page
			</a>
			<div
				class="flex items-center justify-center gap-2 text-xs text-muted-foreground"
			>
				<Loader2 size={12} strokeWidth={2} class="animate-spin" />
				Waiting for authorization…
			</div>
		</div>
		<div class="text-center">
			<button
				type="button"
				onclick={removeDraft}
				class="text-xs text-muted-foreground underline transition-colors hover:text-foreground"
			>
				Cancel
			</button>
		</div>
	{:else if phase === 'error'}
		<div
			role="alert"
			class="flex items-start gap-2 rounded-lg border border-destructive/20 bg-destructive/10 px-3 py-2.5 text-sm text-destructive"
		>
			<AlertCircle size={16} strokeWidth={2} class="mt-0.5 shrink-0" />
			<span>{errorMessage}</span>
		</div>
		<div class="flex flex-wrap justify-end gap-2">
			<button type="button" onclick={removeDraft} class={secondaryBtn}>
				{ownsRow ? 'Remove draft' : 'Close'}
			</button>
			<button type="button" onclick={tryAgain} class={primaryBtn}>Try again</button>
		</div>
	{/if}
</div>
