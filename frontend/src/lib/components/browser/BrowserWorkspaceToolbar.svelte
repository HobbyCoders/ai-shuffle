<script lang="ts">
	import { Plus, X, RotateCw, Settings2 } from 'lucide-svelte';
	import { browserPanel } from '$lib/stores/browser-panel.svelte';
	import {
		ownsControl,
		validViewport,
		type BrowserWorkspaceState,
		type BrowserViewport,
	} from '$lib/browser-panel/workspace';
	let {
		mode = 'all',
		workspace,
		viewerId,
		connected,
		starting = false,
		onCommand,
	}: {
		mode?: 'all' | 'tabs' | 'controls';
		workspace: BrowserWorkspaceState | null;
		viewerId: string | null;
		connected: boolean;
		starting?: boolean;
		onCommand: (op: string, args: Record<string, unknown>) => void;
	} = $props();
	const owner = $derived(connected && ownsControl(workspace, viewerId));
	const pending = $derived(
		!!viewerId && workspace?.control.pending_viewer_id === viewerId,
	);
	const other = $derived(
		!!workspace &&
			((workspace.control.owner === 'human' && !owner) ||
				(!!workspace.control.pending_viewer_id && !pending)),
	);
	let customWidth = $state(1280);
	let customHeight = $state(800);
	let error = $state('');
	let settingsEl = $state<HTMLDetailsElement>();
	const viewportWidth = $derived(workspace?.viewport.width);
	const viewportHeight = $derived(workspace?.viewport.height);
	$effect(() => {
		if (viewportWidth !== undefined) customWidth = viewportWidth;
	});
	$effect(() => {
		if (viewportHeight !== undefined) customHeight = viewportHeight;
	});
	function apply(viewport: BrowserViewport) {
		if (!validViewport(viewport)) {
			error = 'Use width 240–3840 and height 240–2160, in whole pixels.';
			return;
		}
		error = '';
		onCommand('viewport', { ...viewport });
	}
	function preset(value: string) {
		if (value === 'fit')
			apply({
				mode: 'fit',
				width: workspace?.viewport.width ?? 1280,
				height: workspace?.viewport.height ?? 800,
			});
		else {
			const [width, height] = value.split('x').map(Number);
			apply({ mode: 'fixed', width, height });
		}
	}
</script>

<svelte:window onkeydown={(e) => {
	if (e.key === 'Escape' && settingsEl?.open && settingsEl.contains(e.target as Node)) {
		e.preventDefault();
		settingsEl.open = false;
		settingsEl.querySelector('summary')?.focus();
	}
}} />

<div class="workspace-tools" class:tabs-only={mode === 'tabs'}>
	{#if mode !== 'controls'}
	<nav class="tabs" aria-label="Shared browser tabs">
		{#if !workspace?.tabs.length}<div class="tab current"><span class="tab-select">New tab</span></div>{/if}
		{#each workspace?.tabs ?? [] as tab (tab.id)}
			<div class="tab" class:current={tab.id === workspace?.active_tab_id}>
				<button
					class="tab-select"
					aria-label={`Select ${tab.title || tab.url || 'New tab'}`}
					aria-current={tab.current ? 'page' : undefined}
					disabled={!owner}
					onclick={() =>
						onCommand('tabs', { action: 'select', tab_id: tab.id })}
					title={tab.url}>{tab.title || tab.url || 'New tab'}</button
				>
				{#if (workspace?.tabs.length ?? 0) > 1}<button
					class="icon"
					disabled={!owner || (workspace?.tabs.length ?? 0) <= 1}
					aria-label={`Close ${tab.title || 'tab'}`}
					onclick={() => onCommand('tabs', { action: 'close', tab_id: tab.id })}
					><X size={12} /></button
				>{/if}
			</div>
		{/each}
		<button
			class="new-tab"
			aria-label="New tab"
			title="New tab"
			disabled={starting || other}
			onclick={() => onCommand('tabs', { action: 'create' })}
			><Plus size={14} /></button
		>
	</nav>
	{/if}
	{#if mode !== 'tabs'}
	<div class="ownership">
		<span role="status" class="ownership-label"
			>{!connected
				? 'View only · connecting'
				: !workspace?.available
					? 'No browser runtime'
					: pending
						? 'Requesting control'
						: owner
							? 'You control'
							: other
								? 'Another viewer controls'
								: 'Agent control'}{workspace?.busy && pending
				? ' · waiting for current action'
				: ''}</span
		>
		{#if owner || pending}<button
				onclick={() => onCommand('release', {})}
				disabled={!connected}>Return control</button
			>
		{:else}<button
				disabled={!connected || !workspace?.available || other}
				onclick={() => onCommand('takeover', {})}>Take control</button
			>{/if}
	</div>
	<details class="viewport" bind:this={settingsEl}>
		<summary aria-label="Browser settings" title="Viewport and browser settings"><Settings2 size={15} /></summary>
		<div class="viewport-content">
			<label
				>Size <select
					aria-label="Viewport preset"
					disabled={!owner}
					value=""
					onchange={(e) => preset(e.currentTarget.value)}
					><option value="" disabled>Choose viewport</option><option value="fit"
						>Fit panel</option
					><option value="1440x900">Desktop · 1440 × 900</option><option
						value="768x1024">Tablet · 768 × 1024</option
					><option value="390x844">Mobile · 390 × 844</option></select
				></label
			>
			<form
				onsubmit={(e) => {
					e.preventDefault();
					apply({ mode: 'fixed', width: customWidth, height: customHeight });
				}}
			>
				<label
					>Width<input
						aria-label="Viewport width"
						type="number"
						min="240"
						max="3840"
						step="1"
						bind:value={customWidth}
						disabled={!owner}
					/></label
				><span>×</span><label
					>Height<input
						aria-label="Viewport height"
						type="number"
						min="240"
						max="2160"
						step="1"
						bind:value={customHeight}
						disabled={!owner}
					/></label
				>
				<button disabled={!owner}>Apply</button><button
					type="button"
					disabled={!owner}
					aria-label="Rotate viewport"
					onclick={() =>
						apply({ mode: 'fixed', width: customHeight, height: customWidth })}
					><RotateCw size={14} /></button
				>
			</form>
			{#if error}<p role="alert">{error}</p>{/if}
			<div class="defaults">
				<button
					disabled={!owner}
					onclick={() => apply(browserPanel.current.defaultViewport)}
					>Apply saved default</button
				><button
					disabled={!workspace?.available}
					onclick={() =>
						workspace && browserPanel.setDefaultViewport(workspace.viewport)}
					>Save current as default</button
				>
			</div>
			<label class="policy"
				><input
					type="checkbox"
					checked={browserPanel.current.autoOpen}
					onchange={(e) => browserPanel.setAutoOpen(e.currentTarget.checked)}
				/> Open panel when the agent uses the browser</label
			>
			<p>
				Fixed viewports keep their size when you resize this panel. Defaults
				apply only when you choose them.
			</p>
		</div>
	</details>
	{/if}
</div>

<style>
	.workspace-tools { display: contents; font-size: 11px; color: var(--foreground); }
	.workspace-tools.tabs-only { display: block; flex: 1; min-width: 0; }
	.viewport { position: static; }
	.viewport-content { position: absolute; top: 70px; right: 5px; width: min(340px, calc(100% - 10px)); box-sizing: border-box; z-index: 30; background: var(--background); border: 1px solid var(--border); border-radius: 8px; box-shadow: 0 8px 24px #0003; padding: 12px !important; }
	.ownership-label { display: none; }

	button,
	input,
	select {
		font: inherit;
		color: inherit;
	}
	button,
	select,
	input[type='number'] {
		border: 1px solid var(--border);
		border-radius: 5px;
		background: var(--background);
		padding: 5px 7px;
	}
	button {
		cursor: pointer;
		white-space: nowrap;
	}
	button:disabled,
	select:disabled,
	input:disabled {
		opacity: 0.45;
		cursor: default;
	}
	button:focus-visible,
	summary:focus-visible,
	input:focus-visible,
	select:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 2px;
	}
	.tabs {
		display: flex;
		gap: 4px;
		overflow: auto;
		padding: 3px 4px;
		background: var(--muted);
	}
	.tab {
		display: flex;
		align-items: center;
		min-width: 95px;
		max-width: 180px;
		border-radius: 7px 7px 0 0;
		border: 1px solid transparent;
	}
	.tab.current {
		background: var(--background);
		border-color: var(--border);
	}
	.tab-select {
		overflow: hidden;
		text-overflow: ellipsis;
		border: 0;
		background: transparent;
		flex: 1;
		text-align: left;
	}
	.icon {
		display: flex;
		border: 0;
		background: transparent;
		padding: 6px;
	}
	.new-tab {
		border-radius: 7px 7px 0 0;
		display: flex;
		align-items: center;
		gap: 4px;
		flex-shrink: 0;
	}
	.ownership {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 3px 4px;
	}
	.ownership > span {
		color: var(--muted-foreground);
	}
	.ownership > button {
		font-weight: 600;
	}
	summary {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 3px 4px;
		cursor: pointer;
	}
	.viewport-content {
		padding: 4px 10px 10px;
		display: grid;
		gap: 9px;
	}
	.viewport-content > label:first-child {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	select {
		flex: 1;
		min-width: 0;
	}
	form {
		display: flex;
		gap: 6px;
		align-items: end;
	}
	form label {
		display: grid;
		gap: 3px;
		min-width: 0;
		flex: 1;
	}
	input[type='number'] {
		width: 100%;
		box-sizing: border-box;
		min-width: 0;
	}
	form > span {
		align-self: center;
	}
	.defaults {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
	.policy {
		display: flex;
		align-items: center;
		gap: 6px;
	}
	p {
		margin: 0;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	@media (max-width: 600px) {
		button,
		select,
		summary {
			min-height: 28px;
		}
		.tabs {
			padding-block: 3px;
		}
		.ownership {
			padding-block: 3px;
		}
	}
</style>