<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	/**
	 * GrokDeviceFlow — SuperGrok device-code enrollment.
	 *
	 * TWO modes, selected by the `existingProviderId` prop:
	 *   • DRAFT (default): CREATES a placeholder `grok` row on mount and OWNS it.
	 *   • RECONNECT: reuses an existing row; bail paths NEVER delete the row.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { Loader2, Copy, Check, ExternalLink, AlertCircle } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		createModelProvider,
		deleteModelProvider,
		updateModelProvider,
		xaiDeviceStart,
		xaiDevicePoll
	} from '$lib/api/model-providers';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		onDone: () => void;
		onConnected?: (providerId: string) => void;
		onCancel: () => void;
		onBusyChange?: (busy: boolean) => void;
		existingProviderId?: string | null;
	}

	let { onDone, onCancel, onBusyChange, existingProviderId, onConnected }: Props = $props();

	const ownsRow = !existingProviderId;

	type Phase = 'create' | 'starting' | 'awaiting' | 'finishing' | 'error';

	let phase = $state<Phase>('create');
	let userCode = $state('');
	let verificationUri = $state('');
	let errorMessage = $state('');
	let copied = $state(false);
	let copyError = $state('');

	let providerId: string | null = null;
	let pollIntervalMs = 3000;
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let copyTimer: ReturnType<typeof setTimeout> | null = null;
	let destroyed = false;
	let lastBusy = false;

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

	async function createDraft() {
		phase = 'create';
		errorMessage = '';
		setBusy(true);
		try {
			const row = await createModelProvider({
				provider_type: 'grok',
				name: 'Grok',
				base_url: 'https://api.x.ai/v1'
			});
			if (destroyed) return;
			providerId = row.id;
			await startDevice();
		} catch (e) {
			if (destroyed) return;
			fail(getErrorMessage(e, 'Could not create the Grok provider.'));
		}
	}

	async function startDevice() {
		if (!providerId) return;
		phase = 'starting';
		errorMessage = '';
		setBusy(true);
		try {
			const res = await xaiDeviceStart(providerId);
			if (destroyed) return;
			userCode = res.user_code;
			verificationUri = res.verification_uri;
			pollIntervalMs = 3000;
			phase = 'awaiting';
			scheduleNextPoll();
		} catch (e) {
			if (destroyed) return;
			fail(getErrorMessage(e, 'Could not start Grok sign-in.'));
		}
	}

	function scheduleNextPoll() {
		clearPollTimer();
		pollTimer = setTimeout(runPoll, pollIntervalMs);
	}

	async function runPoll() {
		if (destroyed || !providerId || phase !== 'awaiting') return;
		try {
			const res = await xaiDevicePoll(providerId);
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
			fail(getErrorMessage(e, 'Grok sign-in failed. Please try again.'));
		}
	}

	async function finish() {
		clearPollTimer();
		phase = 'finishing';
		setBusy(true);
		if (!onConnected) await seedModels();
		try {
			await providers.refreshProviders();
		} catch {
			// The row is enrolled regardless; a refresh hiccup must not block close.
		}
		if (destroyed) return;
		setBusy(false);
		if (onConnected && providerId) onConnected(providerId);
		else onDone();
	}

	async function seedModels() {
		if (!providerId) return;
		if (!ownsRow && existingHasModels()) return;
		try {
			const fetched = await providers.fetchProviderModels(providerId);
			await updateModelProvider(providerId, { models: fetched.models ?? [] });
		} catch {
			// Best-effort — the backend also seeds an empty roster on complete.
		}
	}

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

	function tryAgain() {
		if (providerId) {
			startDevice();
		} else {
			createDraft();
		}
	}

	async function removeDraft() {
		clearPollTimer();
		if (!ownsRow) {
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
		copied = false;
		copyError = '';
		try {
			await copyText(userCode);
			copied = true;
			if (copyTimer !== null) clearTimeout(copyTimer);
			copyTimer = setTimeout(() => {
				copied = false;
			}, 2000);
		} catch {
			copyError = COPY_ERROR_MESSAGE;
		}
	}

	onMount(() => {
		if (existingProviderId) {
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
			Sign in with SuperGrok to connect Grok. Enter the code below on the sign-in page.
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
			{#if copyError}<p role="alert" class="text-center text-sm text-destructive">{copyError}</p>{/if}
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
