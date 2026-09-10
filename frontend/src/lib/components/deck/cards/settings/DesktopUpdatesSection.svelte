<script lang="ts">
	import { onMount } from 'svelte';
	import { Monitor, RefreshCw } from 'lucide-svelte';
	import { desktop, type DesktopUpdateState } from '$lib/services/desktop';

	let updateState = $state<DesktopUpdateState | null>(null);
	let requestPending = $state(false);
	let requestError = $state('');
	const busy = $derived(requestPending || ['checking', 'available', 'downloading'].includes(updateState?.phase ?? ''));
	const status = $derived.by(() => {
		if (!updateState) return 'Loading update status…';
		switch (updateState.phase) {
			case 'checking': return 'Checking for updates…';
			case 'available': return `Version ${updateState.version} is available. Starting download…`;
			case 'downloading': return `Downloading version ${updateState.version}${updateState.percent === null ? '…' : ` — ${updateState.percent}%`}`;
			case 'ready': return `Version ${updateState.version} is ready. Restart to finish installing.`;
			case 'error': return 'Could not update AI Shuffle Desktop.';
			case 'unsupported': return 'In-app updates are disabled for this build. Preview and development builds do not receive updates.';
			default: return updateState.checkedAt ? 'You’re up to date.' : 'Check for a newer version of AI Shuffle Desktop.';
		}
	});

	onMount(() => {
		if (!desktop) return;
		let active = true;
		let receivedEvent = false;
		const unsubscribe = desktop.updates.onState((next) => {
			receivedEvent = true;
			updateState = next;
			requestError = '';
		});
		void desktop.updates.getState().then((initial) => {
			if (active && !receivedEvent) updateState = initial;
		}).catch((error) => {
			if (active && !receivedEvent) requestError = String(error);
		});
		return () => { active = false; unsubscribe(); };
	});

	async function check() {
		if (!desktop || busy) return;
		requestPending = true;
		requestError = '';
		try {
			await desktop.updates.check();
		} catch (error) {
			requestError = error instanceof Error ? error.message : String(error);
		} finally {
			requestPending = false;
		}
	}
</script>

{#if desktop}
	<section class="set-group" aria-labelledby="desktop-update-title">
		<div class="set-grouphead">
			<h3 id="desktop-update-title"><Monitor />AI Shuffle Desktop</h3>
		</div>
		<div class="set-row stack">
			<div class="lab">
				<h4>Installed version {desktop.version}</h4>
				<p role="status">{status}</p>
				{#if updateState?.checkedAt}<p>Last checked: {new Date(updateState.checkedAt).toLocaleString()}</p>{/if}
				{#if requestError || updateState?.error}<p class="update-error" role="alert">{requestError || updateState?.error}</p>{/if}
			</div>
			<div class="ctl">
				{#if updateState?.phase === 'ready'}
					<button class="ed-btn primary" onclick={() => desktop?.updates.install()}>Restart and update</button>
				{:else}
					<button class="ed-btn" onclick={check} disabled={busy || (!updateState && !requestError) || updateState?.phase === 'unsupported'}>
						<RefreshCw size={16} />{busy ? 'Updating…' : 'Check for updates'}
					</button>
				{/if}
			</div>
		</div>
	</section>
{/if}

<style>
	.update-error { color: var(--destructive, #dc2626); overflow-wrap: anywhere; }
	button { min-height: 44px; }
</style>
