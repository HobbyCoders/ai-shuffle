<script lang="ts">
	import type { Snippet } from 'svelte';
	import { apiBase, withAuth } from '$lib/api/runtime';
	import { LoaderCircle } from 'lucide-svelte';

	let { sessionId, remote = true, deviceName, onCaughtUp, children }: {
		sessionId: string | null;
		remote?: boolean;
		deviceName?: string | null;
		onCaughtUp: () => void;
		children: Snippet;
	} = $props();
	let syncState = $state<'checking' | 'syncing' | 'current' | 'offline'>('checking');
	let pending = $state(0);
	let checkedSession = $state<string | null>(null);
	const waiting = $derived(!!sessionId && remote && (checkedSession !== sessionId || syncState === 'checking' || syncState === 'syncing'));

	$effect(() => {
		const id = sessionId;
		if (!id || !remote) return;
		const controller = new AbortController();
		let timer: ReturnType<typeof setTimeout>;
		let wasBehind = false;
		checkedSession = null;
		syncState = 'checking';
		pending = 0;
		async function poll() {
			try {
				const response = await fetch(`${apiBase()}/sessions/${encodeURIComponent(id!)}/sync-status`, withAuth({ signal: controller.signal }));
				if (!response.ok) throw new Error('Sync status unavailable');
				const status = await response.json();
				if (controller.signal.aborted) return;
				if (!['checking', 'syncing', 'current', 'offline'].includes(status.state)) throw new Error('Invalid sync status');
				if (status.state === 'current' && (wasBehind || checkedSession !== id)) onCaughtUp();
				wasBehind = status.state !== 'current';
				syncState = status.state;
				pending = status.pending_updates ?? 0;
				checkedSession = id;
			} catch {
				// Keep a known backlog visible on a failed poll; never call it current.
			} finally {
				if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
			}
		}
		void poll();
		return () => { controller.abort(); clearTimeout(timer); };
	});
</script>

{#if waiting}
	<div class="mirror-sync" role="status" aria-live="polite">
		<LoaderCircle size={20} aria-hidden="true" />
		<strong>{syncState === 'syncing' ? 'Syncing from' : 'Checking sync with'} {deviceName || 'the owning worker'}…</strong>
		<p>Catching up before showing the current conversation, tasks and agents.</p>
		{#if syncState === 'syncing' && pending > 0}<p>{pending} updates remaining</p>{/if}
	</div>
{:else}
	{#if remote && syncState === 'offline'}
		<div class="mirror-offline" role="status">Owning worker offline. Showing the last saved conversation.</div>
	{/if}
	{@render children()}
{/if}

<style>
	.mirror-sync { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 2rem 1.25rem; text-align: center; color: var(--muted-foreground); }
	.mirror-sync strong { color: var(--foreground); font-size: 1rem; }
	.mirror-sync p { margin: 0; max-width: 42ch; font-size: 0.875rem; }
	.mirror-offline { padding: 0.75rem 1rem; color: var(--muted-foreground); font-size: 0.875rem; }
</style>
