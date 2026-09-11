<script lang="ts">
	import { onMount } from 'svelte';
	import { Download, RefreshCw, LoaderCircle, CircleAlert } from 'lucide-svelte';
	import { desktop, type DesktopUpdateState } from '$lib/services/desktop';

	let update = $state<DesktopUpdateState | null>(null);
	let pending = $state(false);
	let requestError = $state('');
	const ready = $derived(update?.phase === 'ready');
	const error = $derived(requestError || update?.error || '');
	const visible = $derived(!!desktop && !!update && (
		['available', 'downloading', 'ready'].includes(update.phase) ||
		(!!update.version && (update.phase === 'error' || update.phase === 'checking'))
	));
	const busy = $derived(pending || update?.phase === 'downloading' || update?.phase === 'checking');
	const action = $derived(ready ? 'Update and restart' : 'Download update');
	const status = $derived.by(() => {
		if (error) return `${error} Click to retry ${ready ? 'the restart' : 'the download'}.`;
		if (pending && ready) return 'Restarting AI Shuffle…';
		if (update?.phase === 'checking') return 'Checking for updates…';
		if (update?.phase === 'downloading' || pending) {
			const progress = update?.percent == null ? '' : ` (${Math.round(update.percent)}%)`;
			return `${update?.message || 'Downloading update…'}${progress}`;
		}
		return ready ? 'Update ready. Click to install and restart AI Shuffle.' : 'Update available. Click to download.';
	});

	onMount(() => {
		if (!desktop) return;
		let active = true;
		let receivedEvent = false;
		const unsubscribe = desktop.updates.onState((next) => {
			if (!active) return;
			receivedEvent = true;
			update = next;
			requestError = '';
		});
		void desktop.updates.getState().then((initial) => {
			if (active && !receivedEvent) update = initial;
		}).catch(() => { /* A later updater event can restore status without a header alert. */ });
		return () => { active = false; unsubscribe(); };
	});

	async function activate() {
		if (!desktop || busy) return;
		pending = true;
		requestError = '';
		try {
			if (ready) await desktop.updates.install();
			else await desktop.updates.download();
		} catch (cause) {
			requestError = cause instanceof Error ? cause.message : String(cause);
		} finally {
			pending = false;
		}
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
		aria-label={update?.phase === 'checking' ? 'Checking for updates' : busy ? (ready ? 'Restarting AI Shuffle' : 'Downloading update') : action}
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
