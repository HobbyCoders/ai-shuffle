<script lang="ts">
	/**
	 * DetailPane: the one anatomy every collection object uses.
	 *
	 *   1. Header: 42px tile, name (h2), kind badge, status, subtitle, one
	 *      primary action. The tile carries the status dot only when there is
	 *      no status text (one dot per header).
	 *   2. Facts strip: up to 4 facts; facts without data are left out.
	 *   3. Tabs, only with 2+ (state lives in the navigation store, so search
	 *      can land on a tab).
	 *   4. Body: grouped rows (RowGroup renders detail sections here). The
	 *      body snippet receives the current tab.
	 *   5. Danger zone last.
	 *
	 * Read-only mode disables the primary action and the whole body.
	 */
	import type { Snippet } from 'svelte';
	import BackRow from './BackRow.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { categoryById, type SettingsIcon } from '$lib/settings/categories';
	import type { CategoryId, StatusTone } from '$lib/settings/types';
	import { setInDetail } from '../rows/row-context';
	import Tile from '../rows/Tile.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import SavedTick from './SavedTick.svelte';
	import FactsStrip, { type Fact } from './FactsStrip.svelte';
	import DetailTabs, { type DetailTab } from './DetailTabs.svelte';
	import DangerZone, { type DangerAction } from './DangerZone.svelte';

	interface Props {
		name: string;
		/** Optional editable heading; keeps the shared header geometry. */
		heading?: Snippet;
		/** Settings retains its measure; collection details share one centered reading column. */
		width?: 'settings' | 'reading' | 'data' | 'docs';
		/** Content-first destinations have no detail-history or Saved row. */
		contentFirst?: boolean;
		/** Only genuine identity art (an agent avatar or provider logo), never a generic decorative tile. */
		identity?: boolean;
		icon?: SettingsIcon;
		/** Tile text instead of an icon (provider glyph). */
		glyph?: string;
		/** Custom tile. */
		tile?: Snippet;
		status?: StatusTone;
		statusText?: string;
		/** Kind badge next to the name ("API key"). */
		badge?: string;
		subtitle?: string | Snippet;
		/** The one primary action. */
		primary?: Snippet;
		facts?: Fact[];
		/** Notice between facts and tabs. */
		banner?: Snippet;
		tabs?: (string | DetailTab)[];
		/** Defaults to the current category and its selected item. */
		category?: CategoryId;
		item?: string;
		danger?: DangerAction[];
		/** Custom danger rows (rendered inside the danger zone). */
		dangerRows?: Snippet;
		children?: Snippet<[tab: string | null]>;
		/**
		 * Controlled tabs for pages outside Settings (the Agents page): the
		 * current tab and its setter replace the navigation store.
		 */
		tab?: string | null;
		onTabChange?: (tab: string) => void;
		/** Stacked back button outside Settings. */
		onBack?: () => void;
		backLabel?: string;
		/** Replaces the Saved tick at the top right (a save state). */
		topStatus?: Snippet;
		/** Extra badges after the name (several, or with icons). */
		badges?: Snippet;
	}

	let {
		name,
		heading,
		width = 'settings',
		contentFirst = false,
		identity = false,
		icon,
		glyph,
		tile,
		status,
		statusText,
		badge,
		subtitle,
		primary,
		facts = [],
		banner,
		tabs = [],
		category,
		item,
		danger = [],
		dangerRows,
		children,
		tab,
		onTabChange,
		onBack,
		backLabel,
		topStatus,
		badges
	}: Props = $props();

	setInDetail();
	const access = getSettingsAccess();
	const uid = $props.id();

	const cat = $derived(category ?? settingsNav.category);
	const itemId = $derived(item ?? settingsNav.selectedItem(cat) ?? '');
	const tabList = $derived(tabs.map((t): DetailTab => (typeof t === 'string' ? { id: t } : t)));
	const controlled = $derived(onTabChange !== undefined);
	const current = $derived(
		controlled
			? (tabList.find((t) => t.id === tab)?.id ?? tabList[0]?.id ?? null)
			: settingsNav.tabOf(
					cat,
					itemId,
					tabList.map((t) => t.id)
				)
	);
	const showTabs = $derived(tabList.length > 1);
	const catLabel = $derived(backLabel ?? categoryById(cat).label);

	function selectTab(t: string) {
		if (onTabChange) onTabChange(t);
		else if (itemId) settingsNav.setTab(cat, itemId, t);
	}
</script>

<div class="st-dwrap" class:data={width === 'data'} class:reading={width === 'reading'} class:docs={width === 'docs'}>
	{#if !contentFirst}
		<BackRow label={catLabel} onclick={() => (onBack ? onBack() : settingsNav.stacked ? settingsNav.back() : settingsNav.clearSelection(cat))}>
			{#if topStatus}{@render topStatus()}{:else}<SavedTick />{/if}
		</BackRow>
	{/if}
	<div class="st-dh" class:has-identity={!!tile || identity || !!glyph}>
		{#if tile}{@render tile()}{:else if identity || glyph}<Tile size="lg" {icon} text={glyph} status={statusText ? undefined : status} ring="bg" />{/if}
		<div class="st-dh-main">
			<div class="st-dhn">
				{#if heading}{@render heading()}{:else}<h2 tabindex="-1" data-settings-heading="detail">{name}</h2>{/if}
				{#if status && !statusText && !tile && !identity && !glyph}<StatusDot tone={status} />{/if}
				{#if badge}<span class="badge">{badge}</span>{/if}
				{@render badges?.()}
			</div>
			{#if (status && statusText) || subtitle}
				<div class="st-dhs">
					{#if status && statusText}<span class="status"><StatusDot tone={status} />{statusText}</span>{/if}
					{#if typeof subtitle === 'string'}<span>{subtitle}</span>{:else if subtitle}{@render subtitle()}{/if}
				</div>
			{/if}
		</div>
		{#if primary}
			<fieldset class="st-dha" disabled={access.readOnly}>{@render primary()}</fieldset>
		{/if}
	</div>
	<FactsStrip {facts} />
	{@render banner?.()}
	{#if showTabs}
		<DetailTabs
			tabs={tabList}
			{current}
			label="{name} sections"
			idPrefix={uid}
			onselect={selectTab}
		/>
	{/if}
	<div
		role={showTabs ? 'tabpanel' : undefined}
		id={showTabs ? `${uid}-panel` : undefined}
		aria-labelledby={showTabs ? `${uid}-tab-${current}` : undefined}
	>
		<fieldset class="st-dbody" disabled={access.readOnly}>
			{@render children?.(current)}
			<DangerZone items={danger} children={dangerRows} />
		</fieldset>
	</div>
</div>

<style>
	.st-dwrap {
		position: relative;
		width: 100%;
		max-width: calc(var(--st-reading-width) + 2 * var(--st-gutter));
		margin-inline: auto;
		padding: var(--st-page-top) var(--st-gutter) 96px;
	}
	.st-dwrap.docs { max-width: calc(var(--st-prose-width) + var(--st-toc-width) + var(--st-toc-gap) + 2 * var(--st-gutter)); }
	.st-dwrap.data { max-width: calc(var(--st-data-width) + 2 * var(--st-gutter)); }
	.st-dh {
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		gap: 14px;
		align-items: center;
		padding: 0 0 18px;
	}
	.st-dh.has-identity { grid-template-columns: auto minmax(0, 1fr) auto; }
	.st-dh-main {
		min-width: 0;
	}

	.st-dhn {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
	}
	h2 {
		font-size: 24px;
		font-weight: 600;
		letter-spacing: -0.01em;
		margin: 0;
		line-height: 1.25;
		overflow-wrap: anywhere;
	}
	h2:focus {
		outline: none;
	}
	.badge {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 11.5px;
		padding: 1px 8px;
		border-radius: 999px;
		background: var(--st-muted);
		color: var(--st-mfg);
		white-space: nowrap;
		font-weight: 500;
	}
	.st-dhs {
		display: flex;
		gap: 4px 12px;
		flex-wrap: wrap;
		font-size: 13px;
		color: var(--st-mfg);
		margin-top: 4px;
		align-items: center;
	}
	.status {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		/* Moves to its own line first (.st-dhs wraps); only a pane narrower
		   than the status itself wraps its words instead of scrolling (#2118). */
		min-width: 0;
	}
	.st-dha {
		display: flex;
		gap: 8px;
		align-items: center;
		flex-wrap: wrap;
		justify-content: flex-end;
		border: 0;
		margin: 0;
		padding: 0;
		min-width: 0;
	}
	.st-dbody {
		border: 0;
		margin: 0;
		padding: 0;
		min-width: 0;
	}
</style>
