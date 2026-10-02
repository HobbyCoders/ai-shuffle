<script lang="ts">
	/**
	 * Wizard step: the license. Reads `GET /api/v1/license`.
	 *
	 * - Beta builds (no lease public key or license server built in) report
	 *   `mode: 'beta'`: nothing to enter, the step says so and continues.
	 * - A worker that already holds a lease shows its plan and continues.
	 * - Otherwise the owner enters the key from the license email. The key can
	 *   also be added later in Settings > System > License; until then the worker only
	 *   refuses new agent runs.
	 */
	import { onMount } from 'svelte';
	import { BadgeCheck, Loader2 } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { activateLicense, getLicense, LICENSE_CHANGED_EVENT, planName, type LicenseStatus } from '$lib/api/license';

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

	const licensed = $derived(!!status && status.mode === 'licensed' && status.allowed);
	const needsKey = $derived(!!status && status.mode === 'licensed' && !status.allowed);

	async function load() {
		loading = true; loadError = '';
		try { status = await getLicense(); }
		catch (e) { loadError = getErrorMessage(e, 'Could not read the license status.'); }
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
				error = `${result.detail} Free a seat on your account page at hobbycoders.com, or in Settings > System > License on the worker you no longer use, then activate again.`;
				return;
			}
			const { activated: _, ...next } = result;
			status = next;
			key = '';
			window.dispatchEvent(new Event(LICENSE_CHANGED_EVENT));
		} catch (e) {
			error = getErrorMessage(e, 'The license server could not be reached. Check this worker\'s internet connection and try again.');
		} finally {
			busy = false;
		}
	}

	onMount(load);
</script>

<div class="space-y-4" data-testid="setup-license">
	{#if loading}
		<p class="text-sm text-muted-foreground flex items-center gap-2" role="status"><Loader2 size={16} class="animate-spin" /> Reading the license status…</p>
	{:else if loadError}
		<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{loadError}</div>
		<p class="text-sm text-muted-foreground">You can continue and add your license later in Settings > System > License.</p>
	{:else if status?.mode === 'beta'}
		<div class="bg-success/10 border border-success/30 rounded-lg p-4 flex items-start gap-3" role="status">
			<BadgeCheck size={20} strokeWidth={1.75} class="text-success shrink-0 mt-0.5" />
			<div>
				<h2 class="text-sm font-semibold text-foreground mb-1">Beta: no license needed</h2>
				<p class="text-sm text-muted-foreground">This build is part of the beta, and every feature works without a key. When licensing starts, you add your key in Settings > System > License.</p>
			</div>
		</div>
	{:else if licensed && status}
		<div class="bg-success/10 border border-success/30 rounded-lg p-4 flex items-start gap-3" role="status">
			<BadgeCheck size={20} strokeWidth={1.75} class="text-success shrink-0 mt-0.5" />
			<div>
				<h2 class="text-sm font-semibold text-foreground mb-1">This worker is licensed</h2>
				<p class="text-sm text-muted-foreground">
					Plan: {planName(status.plan)}{#if status.key_hint}, key ending {status.key_hint}{/if}.
					{#if status.banner}{status.banner.message}{/if}
				</p>
			</div>
		</div>
	{:else if needsKey}
		<form class="space-y-3" onsubmit={activate}>
			<p class="text-sm text-muted-foreground">
				Enter the license key from your license email. Every license starts with a 14-day trial with every feature.
				One license covers up to 3 workers on your account.
			</p>
			<div>
				<label for="setup-license-key" class="block text-xs text-muted-foreground mb-1">License key</label>
				<input id="setup-license-key" class="input font-mono text-sm" autocomplete="off" spellcheck="false"
					placeholder="ASH-XXXXX-XXXXX-XXXXX-XXXXX" bind:value={key} disabled={busy} />
			</div>
			{#if error}<div role="alert" class="bg-destructive/10 border border-destructive text-destructive px-3 py-2 rounded-lg text-sm">{error}</div>{/if}
			<button type="submit" class="btn btn-primary w-full py-3" disabled={busy || !key.trim()}>
				{#if busy}<Loader2 size={16} class="inline-block animate-spin mr-2" />{/if}Activate
			</button>
			<p class="text-xs text-muted-foreground">No key yet? You can add it later in Settings > System > License. Until a license is active, agents do not start new runs; nothing else is affected.</p>
		</form>
	{/if}

	<div class="flex gap-3">
		{#if onBack}<button type="button" onclick={onBack} class="btn btn-secondary flex-1 py-3">Back</button>{/if}
		<button type="button" onclick={onContinue} class="btn {needsKey ? 'btn-secondary' : 'btn-primary'} flex-1 py-3" disabled={loading}>
			{needsKey ? 'Add it later' : 'Continue'}
		</button>
	</div>
</div>
