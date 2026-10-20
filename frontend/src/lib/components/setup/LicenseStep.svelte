<script lang="ts">
	/**
	 * Wizard step: AI Shuffle Cloud, optional. Reads `GET /api/v1/license`.
	 *
	 * Local use is free and needs no account, and there is no license key: a
	 * Cloud plan follows the account the worker signs in with. This step never
	 * blocks:
	 * - A worker whose account has a paid plan shows it and continues.
	 * - Otherwise the step says local use is free and what a free account
	 *   adds. The account can be linked later in Settings > Cloud.
	 */
	import { onMount } from 'svelte';
	import { BadgeCheck, Loader2 } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { getLicense, PAID_WORKERS, UNLIMITED_SEATS, type LicenseStatus } from '$lib/api/license';

	interface Props {
		onContinue: () => void;
		onBack?: () => void;
	}
	let { onContinue, onBack }: Props = $props();

	let status = $state<LicenseStatus | null>(null);
	let loadError = $state('');
	let loading = $state(true);

	const paid = $derived(status?.state === 'paid');
	const workers = $derived(status?.seats || status?.remote_worker_limit || PAID_WORKERS);

	async function load() {
		loading = true; loadError = '';
		try { status = await getLicense(); }
		catch (e) { loadError = getErrorMessage(e, 'Could not read the Cloud status.'); }
		finally { loading = false; }
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
					From your signed-in Cloud account: {workers >= UNLIMITED_SEATS ? 'unlimited' : workers} remote
					{workers === 1 ? 'worker' : 'workers'} and sync.
				</p>
			</div>
		</div>
	{:else}
		<div class="bg-success/10 border border-success/30 rounded-lg p-4 flex items-start gap-3" role="status">
			<BadgeCheck size={20} strokeWidth={1.75} class="text-success shrink-0 mt-0.5" />
			<div>
				<h2 class="text-sm font-semibold text-foreground mb-1">Local use is free</h2>
				<p class="text-sm text-muted-foreground">
					Everything works on your own machines and network without an account. A free AI Shuffle Cloud
					account adds two remote workers, with no card. A paid plan follows the account you sign in with, so
					there is no key to enter. You can link it later in Settings > Cloud.
				</p>
			</div>
		</div>
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		<!-- Never waits on the status: a stalled request must not block setup. -->
		<button type="button" onclick={onContinue} class="btn btn-primary flex-1 py-3">Continue</button>
	</div>
</div>
