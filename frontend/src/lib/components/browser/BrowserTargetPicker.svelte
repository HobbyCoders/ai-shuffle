<script lang="ts">
	/**
	 * BrowserTargetPicker — which browser this chat's agent drives: the built-in
	 * one, or a Chrome or Edge connected through the AI Shuffle browser
	 * extension. It renders nothing until an extension browser is connected, so
	 * the panel looks the same for everyone who has not installed it. The
	 * choice applies from the agent's next browser call.
	 */
	import { getBrowserTarget, setBrowserTarget, type BrowserTarget } from '$lib/api/browser-panel';

	let { sessionId }: { sessionId: string } = $props();

	let target = $state<BrowserTarget | null>(null);
	let saving = $state(false);
	let error = $state('');

	$effect(() => {
		const sid = sessionId;
		const controller = new AbortController();
		target = null;
		error = '';
		getBrowserTarget(sid, controller.signal)
			.then((result) => { if (sid === sessionId) target = result; })
			.catch(() => { /* no extension support on this worker: stay hidden */ });
		return () => controller.abort();
	});

	const current = $derived(target?.browser_id ?? '');
	const offline = $derived(!!target?.browser_id && !target.browsers.some((b) => b.id === target?.browser_id));

	async function choose(event: Event) {
		const sid = sessionId;
		const value = (event.currentTarget as HTMLSelectElement).value;
		saving = true;
		error = '';
		try {
			const result = await setBrowserTarget(sid, value || null);
			if (sid === sessionId && result) target = result;
		} catch (e) {
			error = e instanceof Error ? e.message : 'Could not switch browser';
			(event.currentTarget as HTMLSelectElement | null)?.blur();
		} finally {
			saving = false;
		}
	}
</script>

{#if target && (target.browsers.length || target.browser_id)}
	<label class="target" title={error || (current ? 'The agent works in this browser, in a tab group for this chat' : 'The agent works in the built-in browser')}>
		<span class="sr-only">Browser for this chat</span>
		<select value={current} onchange={choose} disabled={saving} aria-label="Browser for this chat" class:error={!!error}>
			<option value="">Built-in browser</option>
			{#each target.browsers as browser (browser.id)}
				<option value={browser.id}>{browser.name}</option>
			{/each}
			{#if offline}<option value={current} disabled>Your browser (offline)</option>{/if}
		</select>
	</label>
{/if}

<style>
	.target { display: flex; align-items: center; padding: 0 4px; flex: none; }
	select {
		max-width: 160px;
		height: 24px;
		padding: 0 6px;
		font: inherit;
		font-size: 11px;
		color: var(--foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 4px);
		cursor: pointer;
	}
	select:disabled { opacity: 0.6; cursor: progress; }
	select.error { border-color: var(--destructive); }
	select:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
</style>
