<script lang="ts">
	/**
	 * ProviderRow — one row in the static provider catalog.
	 *
	 * Presentational only: `ProviderList` owns every piece of state and hands this
	 * row a display model plus a `children` snippet that becomes the expanded
	 * panel. The row itself is a flat list entry — no card border, no background,
	 * just a bottom rule — so the catalog reads as one continuous list.
	 *
	 * Layout: a status dot pinned to the top-left corner of the brand glyph tile,
	 * then the name (with an optional type badge) over a one-line status subtitle.
	 * On the right: the expand control, and — for providers that can be switched
	 * off — an enable toggle.
	 *
	 * The whole header is the expand button, labelled "Show {title} settings" so
	 * the control has a stable accessible name regardless of the subtitle text.
	 * The toggle is a sibling of that button (never nested inside it) and stops
	 * propagation, so flipping it never expands the row.
	 */
	import type { Snippet } from 'svelte';
	import { ChevronDown, Loader2 } from 'lucide-svelte';
	import ProviderGlyph, {
		type ProviderGlyphKind
	} from '$lib/components/settings/ProviderGlyph.svelte';

	type Tone = 'ok' | 'warn' | 'off';

	interface Props {
		title: string;
		/** One-line status text under the title. */
		subtitle: string;
		/** Brand mark drawn in the tile. */
		glyph: ProviderGlyphKind;
		/** Status-dot colour. */
		tone: Tone;
		/** Whether the panel is open. */
		expanded: boolean;
		onToggleExpand: () => void;
		/** Small mono pill after the title (Subscription / API key / Gateway). */
		badge?: string;

		/** Enable toggle — rendered only when `onToggleEnabled` is supplied. */
		enabled?: boolean;
		onToggleEnabled?: () => void;
		/** Spinner in the toggle's slot while the flip is in flight. */
		toggling?: boolean;

		/** Panel body, rendered while `expanded`. */
		children?: Snippet;
	}

	let {
		title,
		subtitle,
		glyph,
		tone,
		expanded,
		onToggleExpand,
		badge,
		enabled = false,
		onToggleEnabled,
		toggling = false,
		children
	}: Props = $props();

	function onToggleClick(event: MouseEvent) {
		event.stopPropagation();
		onToggleEnabled?.();
	}
</script>

<div class="prov-row">
	<div class="prov-head">
		<button
			type="button"
			class="prov-headbtn"
			aria-expanded={expanded}
			aria-label="Show {title} settings"
			onclick={onToggleExpand}
		>
			<span class="prov-icwrap">
				<span class="prov-ic"><ProviderGlyph kind={glyph} size={17} /></span>
				<span class="prov-dot {tone}"></span>
			</span>

			<span class="prov-body">
				<span class="prov-name">
					<span class="prov-title">{title}</span>
					{#if badge}<span class="sb">{badge}</span>{/if}
				</span>
				<span class="prov-subtitle">{subtitle}</span>
			</span>

			<span class="prov-chev" class:open={expanded}><ChevronDown /></span>
		</button>

		<!-- The slot is always present, empty on rows that can't be switched off,
		     so the chevron sits in the same column down the whole list. -->
		<div class="prov-toggle">
			{#if onToggleEnabled}
				{#if toggling}
					<Loader2 class="mp-toggle-spin" />
				{:else}
					<button
						type="button"
						class="switch"
						class:on={enabled}
						role="switch"
						aria-checked={enabled}
						aria-label="Enable {title}"
						onclick={onToggleClick}><i></i></button
					>
				{/if}
			{:else}
				<span class="prov-toggle-gap" aria-hidden="true"></span>
			{/if}
		</div>
	</div>

	{#if expanded}
		<div class="prov-panel">{@render children?.()}</div>
	{/if}
</div>

<style>
	/* Flat list: rows are separated by a rule, not boxed. */
	.prov-row {
		border-bottom: 1px solid var(--border);
	}
	.prov-row:last-child {
		border-bottom: none;
	}
	.prov-head {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px 4px;
	}
	/* The header IS the expand control, so it spans the row. */
	.prov-headbtn {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 0;
		border: none;
		background: none;
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.prov-headbtn:focus-visible {
		outline: 2px solid var(--ring, var(--foreground));
		outline-offset: 3px;
		border-radius: 8px;
	}

	/* Glyph tile with the status dot pinned to its top-left corner. */
	.prov-icwrap {
		position: relative;
		flex-shrink: 0;
	}
	.prov-ic {
		width: 32px;
		height: 32px;
		border-radius: 8px;
		display: grid;
		place-items: center;
		background: var(--background);
		color: var(--foreground);
	}
	.prov-dot {
		position: absolute;
		top: -2px;
		left: -2px;
		width: 8px;
		height: 8px;
		border-radius: 999px;
		box-shadow: 0 0 0 2px var(--card, var(--background));
	}
	.prov-dot.ok {
		background: var(--success, #16a34a);
	}
	.prov-dot.warn {
		background: var(--warning, #d97706);
	}
	.prov-dot.off {
		background: var(--muted-foreground);
		opacity: 0.6;
	}

	.prov-body {
		flex: 1;
		min-width: 0;
		display: block;
	}
	.prov-name {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.prov-title {
		font-size: 13px;
		font-weight: 600;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		max-width: 100%;
	}
	.prov-subtitle {
		display: block;
		margin-top: 2px;
		font-size: 12px;
		line-height: 1.4;
		color: var(--muted-foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.prov-chev {
		flex-shrink: 0;
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		color: var(--muted-foreground);
		transition: transform 0.15s;
	}
	.prov-chev.open {
		transform: rotate(180deg);
	}
	.prov-chev :global(svg) {
		width: 15px;
		height: 15px;
	}
	@media (prefers-reduced-motion: reduce) {
		.prov-chev {
			transition: none;
		}
	}

	/* Same width as the `.switch` it holds (34x20, StudioEditorShell). */
	.prov-toggle {
		flex-shrink: 0;
		display: grid;
		place-items: center;
		width: 34px;
	}
	.prov-toggle-gap {
		display: block;
		width: 34px;
		height: 20px;
	}

	/* Panel body sits under the title, clear of the glyph tile. */
	.prov-panel {
		padding: 4px 0 14px 44px;
		font-size: 12px;
	}

	/* Type badge. */
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
		color: var(--muted-foreground);
		background: var(--muted);
	}

	/* Toggle-in-progress spinner sits where the .switch would be. */
	:global(.mp-toggle-spin) {
		width: 18px;
		height: 18px;
		color: var(--muted-foreground);
		animation: acct-spin 0.8s linear infinite;
	}
	@keyframes acct-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.mp-toggle-spin) {
			animation: none;
		}
	}

	@media (max-width: 560px) {
		.prov-head {
			/* Bigger touch target on handhelds. */
			padding: 14px 4px;
		}
		.prov-panel {
			padding-left: 8px;
		}
		.prov-subtitle {
			white-space: normal;
		}
	}
</style>
