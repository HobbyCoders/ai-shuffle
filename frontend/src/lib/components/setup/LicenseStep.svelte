<script lang="ts">
	/**
	 * Wizard step: AI Shuffle Cloud, optional. Reads `GET /api/v1/license`.
	 *
	 * Local use is free and needs no account or key, so this step never blocks:
	 * - A worker on a paid plan shows the plan and continues.
	 * - Otherwise the step says local use is free, what a free account adds,
	 *   and offers an optional paid plan key. Both can be added later in
	 *   Settings > Cloud.
	 */
	import { onMount } from 'svelte';
	import { BadgeCheck, Loader2 } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { activateLicense, getLicense, planName, type LicenseStatus } from '$lib/api/license';

	interface Props {
		onContinue: () => void;
		onBack?: () => void;
	}
	let { onContinue, onBack }: Props = $props();

	let status = $state<LicenseStatus | null>(null);
	let loadError = $state('');
	let loading = $state(true);
	let key = $state('');
	let busy = $state(false);
	let error = $state('');

	const paid = $derived(status?.state === 'paid');
	const keyEntry = $derived(!!status && status.server_configured && !paid);

	async function load() {
		loading = true; loadError = '';
		try { status = await getLicense(); }
		catch (e) { loadError = getErrorMessage(e, 'Could not read the Cloud status.'); }
		finally { loading = false; }
	}

	async function activate(event: SubmitEvent) {
		event.preventDefault();
		const value = key.trim();
		if (!value || busy) return;
		busy = true; error = '';
		try {
			const result = await activateLicense(value);
			if (!result.activated) {
				// The server's message already says where to free a seat.
				error = result.detail;
				return;
			}
			const { activated: _, ...next } = result;
			status = next;
			key = '';
		} catch (e) {
			error = getErrorMessage(e, 'AI Shuffle Cloud could not be reached. Local use is unaffected; you can add the key later.');
		} finally {
			busy = false;
		}
	}

	onMount(load);
</script>

<div class="space-y-4" data-testid="setup-license">
	{#if loading}
		<p class="text-sm text-muted-foreground flex items-center gap-2" role="status"><Loader2 size={16} class="animate-spin" /> Reading the Cloud status…</p>
	{:else if loadError}
		<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{loadError}</div>
		<p class="text-sm text-muted-foreground">Local use does not need it. You can continue and check Settings > Cloud later.</p>
	{:else if paid && status}
		<div class="bg-success/10 border border-success/30 rounded-lg p-4 flex items-start gap-3" role="status">
			<BadgeCheck size={20} strokeWidth={1.75} class="text-success shrink-0 mt-0.5" />
			<div>
				<h2 class="text-sm font-semibold text-foreground mb-1">Paid Cloud plan active</h2>
				<p class="text-sm text-muted-foreground">
					Plan: {planName(status.plan)}{#if status.key_hint}, key ending {status.key_hint}{/if}.
					{status.remote_worker_limit} remote {status.remote_worker_limit === 1 ? 'worker' : 'workers'}.
				</p>
			</div>
		</div>
	{:else}
		<div class="bg-success/10 border border-success/30 rounded-lg p-4 flex items-start gap-3" role="status">
			<BadgeCheck size={20} strokeWidth={1.75} class="text-success shrink-0 mt-0.5" />
			<div>
				<h2 class="text-sm font-semibold text-foreground mb-1">Local use is free</h2>
				<p class="text-sm text-muted-foreground">
					Everything works on your own machines and network without an account or key. A free AI Shuffle Cloud
					account adds one remote worker, with no card. You can link it later in Settings > Cloud.
				</p>
			</div>
		</div>
		{#if keyEntry}
			<form class="space-y-3" onsubmit={activate}>
				<div>
					<label for="setup-license-key" class="block text-xs text-muted-foreground mb-1">Paid plan key (optional)</label>
					<input id="setup-license-key" class="input font-mono text-sm" autocomplete="off" spellcheck="false" maxlength="64"
						placeholder="ASH-XXXXX-XXXXX-XXXXX-XXXXX" bind:value={key} disabled={busy} />
				</div>
				{#if error}<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{error}</div>{/if}
				<button type="submit" class="btn btn-secondary w-full py-3" disabled={busy || !key.trim()}>
					{#if busy}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Activate
				</button>
			</form>
		{/if}
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		<!-- Never waits on the status: a stalled request must not block setup. -->
		<button type="button" onclick={onContinue} class="btn btn-primary flex-1 py-3">Continue</button>
	</div>
</div>
