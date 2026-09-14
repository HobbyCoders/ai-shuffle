<script lang="ts">
	import { onMount } from 'svelte';
	import { Download, RefreshCw, LoaderCircle, CircleAlert } from 'lucide-svelte';
	import { api, getErrorMessage } from '$lib/api/client';

	type Worker = { device_id: string; device_name: string; phase: string; supported: boolean; online: boolean; allow_updates: boolean; version?: string; percent?: number; message?: string; error?: string; reason?: string };
	type Snapshot = { allow_updates: boolean; update_all_workers: boolean; workers: Worker[] };
	type Receipt = { results: { device_name: string; accepted: boolean; reason: string }[] };
	let snapshot = $state<Snapshot | null>(null);
	let pending = $state(false);
	let requestError = $state('');
	let receipt = $state('');
	let active = false;
	let reading = false;
	const eligible = $derived(snapshot?.workers.filter(w => w.supported && w.online && w.allow_updates) ?? []);
	const downloading = $derived(eligible.some(w => ['checking', 'downloading'].includes(w.phase)));
	const available = $derived(eligible.some(w => ['available', 'error'].includes(w.phase) && !!w.version));
	const ready = $derived(!available && !downloading && eligible.some(w => w.phase === 'ready'));
	const error = $derived(requestError || eligible.find(w => w.error)?.error || '');
	const visible = $derived(!!snapshot?.allow_updates && (ready || available || downloading || !!receipt || !!requestError));
	const busy = $derived(pending || downloading);
	const action = $derived(ready ? 'Update and restart' : available ? 'Download update' : 'Check for updates');
	const scope = $derived(snapshot?.update_all_workers ? 'All online workers' : 'Current worker');
	const status = $derived.by(() => {
		const reports = snapshot?.workers.map(w => `${w.device_name}: ${w.reason || w.error || w.message || w.phase}`).join('; ') ?? '';
		if (error) return `${scope}: ${error}. ${reports}`;
		if (receipt) return `${scope}: ${receipt}. ${reports}`;
		if (busy) return `${scope}: ${ready ? 'Restarting…' : 'Preparing updates…'} ${reports}`;
		return `${scope}: ${ready ? 'Update ready. Click to install and restart.' : 'Update available. Click to download.'} ${reports}`;
	});
	async function refresh() {
		if (reading) return;
		reading = true;
		try {
			const next = await api.get<Snapshot>('/settings/application-updates/workers');
			if (active) snapshot = next;
		} catch { /* Unavailable/older workers must not enable an unsafe fallback. */ }
		finally { reading = false; }
	}
	onMount(() => {
		active = true; void refresh();
		const timer = setInterval(refresh, 5000);
		window.addEventListener('application-update-policy', refresh);
		return () => { active = false; clearInterval(timer); window.removeEventListener('application-update-policy', refresh); };
	});
	async function activate() {
		if (busy || !snapshot?.allow_updates) return;
		pending = true; requestError = ''; receipt = '';
		try {
			const result = await api.post<Receipt>('/settings/application-updates', { action: ready ? 'install' : available ? 'download' : 'check' });
			receipt = result.results.map(w => `${w.device_name}: ${w.reason}`).join('; ');
		} catch (cause) { requestError = getErrorMessage(cause, 'Could not request application update'); }
		finally { pending = false; void refresh(); }
	}
</script>

{#if visible}
	<button
		class="update-button"
		class:ready
		class:failed={!!error}
		type="button"
		onclick={activate}
		disabled={busy}
		aria-label={busy ? (ready ? 'Restarting AI Shuffle' : 'Downloading update') : action}
		title={status}
		aria-busy={busy}
	>
		{#if busy}<LoaderCircle size={16} strokeWidth={1.85} class="update-spinner" />
		{:else if error}<CircleAlert size={16} strokeWidth={1.85} />
		{:else if ready}<RefreshCw size={16} strokeWidth={1.85} />
		{:else}<Download size={16} strokeWidth={1.85} />{/if}
	</button>
	<span class="sr-only" role={error ? 'alert' : 'status'}>{status}</span>
{/if}

<style>
	.update-button {
		width: 28px;
		height: 28px;
		flex: none;
		display: grid;
		place-items: center;
		border: none;
		border-radius: 8px;
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		cursor: pointer;
		-webkit-app-region: no-drag;
	}
	.update-button:hover:not(:disabled) { background: color-mix(in oklch, var(--primary) 22%, transparent); }
	.update-button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.update-button:disabled { cursor: wait; }
	.update-button.ready { box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 35%, transparent); }
	.update-button.failed { color: var(--destructive); background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.update-button :global(.update-spinner) { animation: update-spin 1s linear infinite; }
	@keyframes update-spin { to { transform: rotate(360deg); } }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
	@media (hover: none) { .update-button { width: 44px; height: 44px; } }
	@media (prefers-reduced-motion: reduce) { .update-button :global(.update-spinner) { animation: none; } }
</style>
