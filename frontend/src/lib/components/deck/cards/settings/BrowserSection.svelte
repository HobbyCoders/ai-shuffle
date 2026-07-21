<script lang="ts">
	/**
	 * Admin settings panel for browser automation.
	 *
	 * The built-in `browser` MCP server is a login-less, spawned server that drives
	 * the supervised Chromium over loopback CDP — no account, extension, or pairing.
	 * So this card is deliberately minimal: a health dot, an enable/disable toggle,
	 * and a refresh. (A future browser-panel live view is out of scope; the sidecar
	 * CDP port is the documented seam for it.)
	 */
	import { onMount } from 'svelte';
	import { Globe, RefreshCw } from 'lucide-svelte';
	import { getBrowserStatus, toggleBrowser, type BrowserStatus } from '$lib/api/browser';

	let status = $state<BrowserStatus | null>(null);
	let loading = $state(true);
	let error = $state('');
	let toggling = $state(false);

	// Health dot: green = running, gray = disabled, red = enabled but unreachable.
	let health = $derived(
		!status ? 'off' : !status.enabled ? 'off' : status.browser_running ? 'ok' : 'err'
	);
	let healthLabel = $derived(
		!status || !status.enabled
			? 'Disabled'
			: status.browser_running
				? 'Running'
				: 'Enabled — browser unreachable'
	);

	async function load() {
		try {
			status = await getBrowserStatus();
			error = '';
		} catch (e: any) {
			error = e?.detail || 'Failed to load browser status';
		} finally {
			loading = false;
		}
	}

	async function onToggle() {
		if (!status || toggling) return;
		toggling = true;
		try {
			status = await toggleBrowser(!status.enabled);
			error = '';
		} catch (e: any) {
			error = e?.detail || 'Failed to update setting';
		} finally {
			toggling = false;
		}
	}

	onMount(load);
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><Globe />Browser automation</h3>
		<p>
			Agents drive a headless browser via the built-in browser tools. No account or setup
			required.
		</p>
	</div>

	{#if loading}
		<div class="set-row"><div class="lab"><p>Loading status…</p></div></div>
	{:else if !status}
		<div class="set-row"><div class="lab"><p class="msg err">{error || 'Status unavailable.'}</p></div></div>
	{:else}
		<div class="set-row">
			<div class="lab">
				<h4><span class="dot {health}" title={healthLabel}></span>Enable browser tools</h4>
				<p>Exposes the <code>mcp__browser__*</code> tools to every session. {healthLabel}.</p>
				{#if healthLabel === 'Enabled — browser unreachable' && status.detail}
					<p class="detail">{status.detail}</p>
				{/if}
			</div>
			<div class="ctl">
				<button type="button" class="ed-btn sm" onclick={load} title="Refresh status">
					<RefreshCw size={14} />
				</button>
				<button
					type="button"
					class="switch"
					class:on={status.enabled}
					role="switch"
					aria-checked={status.enabled}
					aria-label="Enable browser tools"
					disabled={toggling}
					onclick={onToggle}
				><i></i></button>
			</div>
		</div>
		{#if error}<div class="set-row"><div class="lab"><p class="msg err">{error}</p></div></div>{/if}
	{/if}
</div>

<style>
	.dot {
		display: inline-block;
		width: 8px;
		height: 8px;
		border-radius: 50%;
		margin-right: 7px;
		vertical-align: middle;
		background: var(--muted-foreground);
		opacity: 0.5;
	}
	.dot.ok {
		background: var(--success, #16a34a);
		opacity: 1;
	}
	.dot.err {
		background: var(--destructive);
		opacity: 1;
	}
	.ctl {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.msg {
		margin: 0;
		font-size: 12.5px;
	}
	.detail {
		margin: 4px 0 0;
		font-size: 12px;
		color: var(--muted-foreground);
		opacity: 0.85;
	}
	.msg.err {
		color: var(--destructive);
	}
	code {
		font-family: var(--font-mono, monospace);
		font-size: 12px;
	}
</style>
