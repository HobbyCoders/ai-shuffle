<script lang="ts">
	/**
	 * VaultChip — the Vault's one control in the composer row.
	 *
	 * It opens the panel VaultPanel owns (active access in this chat, and the way
	 * to the saved sign-ins) and says, without opening anything, whether something
	 * waits for the owner, access is active, or the Vault could not be read. It
	 * holds no data of its own: everything comes from the `vaultSummary` store,
	 * so it renders nothing until the panel has something to show.
	 */
	import { Vault } from 'lucide-svelte';
	import { vaultSummary } from '$lib/stores/vault';
	import { openHeaderDropdown } from '$lib/stores/header-dropdowns';

	/** `dock`: the desktop composer's icon row. `cell`: the phone quick bar. */
	let { variant = 'dock' }: { variant?: 'dock' | 'cell' } = $props();

	// The store describes the one mounted panel (the open chat's), which clears
	// its entry when it unmounts: the chip always controls the panel on screen.
	const summary = $derived($vaultSummary);
	const open = $derived($openHeaderDropdown === 'vault');
	const state = $derived(
		!summary ? 'idle' : summary.pending > 0 ? 'waiting' : summary.error ? 'error' : summary.active > 0 ? 'active' : 'idle'
	);
	const label = $derived(
		!summary ? 'Vault'
			: state === 'waiting' ? `Vault: ${summary.pending} waiting for you`
			: state === 'error' ? 'Vault: unavailable'
			: state === 'active' ? `Vault: ${summary.active} active`
			: 'Vault'
	);
</script>

{#if summary?.manageable && summary.toggle}
	<button
		type="button"
		class="vault-chip {variant}"
		class:open
		data-vault-chip
		data-state={state}
		aria-label={label}
		aria-haspopup="dialog"
		aria-expanded={open}
		aria-controls={summary.panelId}
		title={label}
		onclick={summary.toggle}
	>
		<span class="glyph" aria-hidden="true">
			<Vault size={16} strokeWidth={1.75} />
			{#if state === 'waiting'}
				<span class="pa-count">{summary.pending > 9 ? '9+' : summary.pending}</span>
			{:else if state !== 'idle'}
				<span class="pa-dot {state}"></span>
			{/if}
		</span>
	</button>
{/if}

<style>
	.vault-chip {
		display: grid;
		place-items: center;
		flex: none;
		width: 28px;
		height: 28px;
		padding: 0;
		border: 0;
		border-radius: var(--radius-control, 0.5rem);
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease;
		-webkit-tap-highlight-color: transparent;
	}
	.vault-chip:hover { background: var(--hover-overlay); color: var(--foreground); }
	.vault-chip:focus-visible { outline: 2px solid var(--ring); outline-offset: 1px; }
	.vault-chip.open, .vault-chip[data-state='waiting'] { color: var(--foreground); }
	.vault-chip.open { background: var(--muted); }
	/* The phone quick bar's cell size: a full 44px touch target. */
	.vault-chip.cell { width: 44px; height: 44px; min-height: 44px; border-radius: var(--radius, 0.75rem); }
	@media (max-width: 480px) { .vault-chip.dock { width: 40px; height: 44px; } }
	@media (max-width: 340px) { .vault-chip.dock { width: 32px; } }

	.glyph { position: relative; display: grid; place-items: center; width: 16px; height: 16px; }
	/* The ring in the surface's own colour keeps the mark legible on the icon. */
	.pa-count {
		position: absolute;
		top: -7px;
		left: 9px;
		box-sizing: border-box;
		min-width: 15px;
		height: 15px;
		padding: 0 4px;
		border-radius: 999px;
		background: var(--warning);
		color: var(--warning-foreground);
		box-shadow: 0 0 0 2px var(--card, var(--background));
		font-size: 0.59375rem;
		font-weight: 700;
		line-height: 15px;
		text-align: center;
		font-variant-numeric: tabular-nums;
	}
	.pa-dot {
		position: absolute;
		top: -2px;
		right: -3px;
		width: 7px;
		height: 7px;
		border-radius: 50%;
		box-shadow: 0 0 0 2px var(--card, var(--background));
	}
	.pa-dot.active { background: var(--success); }
	.pa-dot.error { background: var(--destructive); }

	@media (prefers-reduced-motion: reduce) { .vault-chip { transition: none; } }
</style>
