<script lang="ts">
	/**
	 * ProviderRow — one unified row in the Providers list (D2 contract, plan §1.1).
	 *
	 * Presentational only: the parent (`ProviderList`) owns all state and hands
	 * this row its display model + action callbacks. Reuses the `.acct-*` row
	 * system relocated from `ConnectionsSection` so the visual style is unchanged.
	 *
	 * A row shows: name, type badge, connection-status pill, and an actions
	 * cluster. Gateway rows (OpenRouter/OpenAI/custom) carry an enable toggle;
	 * native-auth rows (Claude subscription, ChatGPT subscription) show their
	 * status pill instead of a toggle (§1.1).
	 *
	 * "Manage models" and "Sign in" are rendered as buttons wired to no-op
	 * callbacks in this chunk — Chunks 5/6/7 wire the modal/drawer behind them.
	 */
	import { Server, Pencil, Trash2, SlidersHorizontal, LogIn, RotateCcw } from 'lucide-svelte';

	type Tone = 'ok' | 'warn' | 'off';

	interface Props {
		title: string;
		/** Type badge text (rendered uppercase by `.sb`). */
		badge: string;
		/** Connection-status pill. */
		status: { label: string; tone: Tone };
		/** Which leading icon to render. */
		iconKind?: 'server' | 'claude';
		/** Optional secondary meta line (e.g. "base_url · N models"). */
		meta?: string;
		/** Row highlights green when true (enabled gateway / active auth). */
		highlight?: boolean;

		/** "Manage models" — no-op in this chunk (Chunk 7 wires the drawer). */
		onManageModels?: () => void;
		/** Open the connected ChatGPT provider's banked reset inventory. */
		onBankedResets?: (opener: HTMLButtonElement) => void;
		/** "Sign in" / "Reconnect" — no-op in this chunk (Chunk 5/6 wires it). */
		onSignIn?: () => void;
		signInLabel?: string;

		/** Edit (gateway rows). */
		onEdit?: () => void;

		/** Remove with inline confirm (state owned by the parent). */
		onRemove?: () => void;
		confirmingDelete?: boolean;
		onConfirmDelete?: () => void;
		onCancelDelete?: () => void;
	}

	let {
		title,
		badge,
		status,
		iconKind = 'server',
		meta,
		highlight = false,
		onManageModels,
		onBankedResets,
		onSignIn,
		signInLabel = 'Sign in',
		onEdit,
		onRemove,
		confirmingDelete = false,
		onConfirmDelete,
		onCancelDelete
	}: Props = $props();
</script>

<li class="acct-row prov-row" class:active={highlight}>
	<div class="acct-ic prov-ic" aria-hidden="true">
		{#if iconKind === 'claude'}
			<svg viewBox="0 0 24 24" fill="currentColor">
				<path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 17.93c-3.95-.49-7-3.85-7-7.93 0-.62.08-1.21.21-1.79L9 15v1c0 1.1.9 2 2 2v1.93zm6.9-2.54c-.26-.81-1-1.39-1.9-1.39h-1v-3c0-.55-.45-1-1-1H8v-2h2c.55 0 1-.45 1-1V7h2c1.1 0 2-.9 2-2v-.41c2.93 1.19 5 4.06 5 7.41 0 2.08-.8 3.97-2.1 5.39z"/>
			</svg>
		{:else}
			<Server />
		{/if}
	</div>

	<div class="acct-body">
		<div class="acct-name">
			<span class="acct-title">{title}</span>
			<span class="sb off">{badge}</span>
			<span class="sb {status.tone}">{status.label}</span>
		</div>
		{#if meta}
			<p class="acct-meta"><span class="prov-meta-txt">{meta}</span></p>
		{/if}
	</div>

	<div class="acct-acts">
		{#if confirmingDelete}
			<span class="acct-confirm">Delete?</span>
			<button type="button" class="ed-btn danger acct-sm" onclick={onConfirmDelete}>Delete</button>
			<button type="button" class="ed-btn acct-sm" onclick={onCancelDelete}>Cancel</button>
		{:else}
			{#if onManageModels}
				<button type="button" class="ed-btn acct-sm" onclick={onManageModels}>
					<SlidersHorizontal />Manage models
				</button>
			{/if}

			{#if onBankedResets}
				<button
					type="button"
					class="ed-btn acct-sm"
					onclick={(event) => onBankedResets?.(event.currentTarget)}
				>
					<RotateCcw />Banked resets
				</button>
			{/if}

			{#if onSignIn}
				<button type="button" class="ed-btn acct-sm" onclick={onSignIn}>
					<LogIn />{signInLabel}
				</button>
			{/if}

			{#if onEdit}
				<button type="button" class="ed-btn acct-sm" onclick={onEdit}><Pencil />Edit</button>
			{/if}

			{#if onRemove}
				<button type="button" class="acct-iconbtn danger" title="Remove" aria-label="Remove provider" onclick={onRemove}><Trash2 /></button>
			{/if}
		{/if}
	</div>
</li>

<style>
	/* Row system relocated from ConnectionsSection (mechanics/visuals unchanged).
	   These render inside `.ed-content`, so shell globals (.ed-btn/.switch/.inp)
	   still theme the buttons — the styles below only cover the row layout. */
	.acct-row {
		display: flex;
		gap: 12px;
		align-items: center;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.acct-row.active {
		border-color: color-mix(in oklch, var(--success, #16a34a) 45%, var(--border));
		background: color-mix(in oklch, var(--success, #16a34a) 7%, var(--muted));
	}
	.acct-ic {
		flex-shrink: 0;
		width: 32px;
		height: 32px;
		border-radius: 8px;
		display: grid;
		place-items: center;
		background: var(--background);
		color: var(--foreground);
		font-weight: 600;
		font-size: 14px;
	}
	.prov-ic :global(svg) {
		width: 17px;
		height: 17px;
	}
	.acct-body {
		flex: 1;
		min-width: 0;
	}
	.acct-name {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.acct-title {
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}
	.acct-meta {
		margin: 2px 0 0;
		font-size: 11.5px;
		color: var(--muted-foreground);
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}
	.prov-meta-txt {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}
	.acct-acts {
		flex-shrink: 0;
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	.acct-sm {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		padding: 5px 10px;
		font-size: 12px;
	}
	.acct-sm :global(svg) {
		width: 13px;
		height: 13px;
	}
	.acct-confirm {
		font-size: 12px;
		color: var(--warning, #d97706);
		font-weight: 600;
	}
	.acct-iconbtn {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.acct-iconbtn :global(svg) {
		width: 15px;
		height: 15px;
	}
	.acct-iconbtn:hover {
		color: var(--foreground);
		border-color: var(--foreground);
	}
	.acct-iconbtn.danger:hover {
		color: var(--destructive);
		border-color: var(--destructive);
	}
	.acct-iconbtn:focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 1px;
	}
	/* Status/type pill. */
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.sb.off {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.sb.warn {
		color: var(--warning, #d97706);
		background: color-mix(in oklch, var(--warning, #d97706) 16%, transparent);
	}
	/* Toggle-in-progress spinner sits where the .switch would be. */
	:global(.mp-toggle-spin) {
		width: 18px;
		height: 18px;
		color: var(--muted-foreground);
		animation: acct-spin 0.8s linear infinite;
	}
	@keyframes acct-spin {
		to { transform: rotate(360deg); }
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.mp-toggle-spin) {
			animation: none;
		}
	}

	@media (max-width: 560px) {
		.acct-row {
			flex-wrap: wrap;
		}
		.acct-acts {
			width: 100%;
			justify-content: flex-start;
			padding-left: 44px;
		}
	}
</style>
