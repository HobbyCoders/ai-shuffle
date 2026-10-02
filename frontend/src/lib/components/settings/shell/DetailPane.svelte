<script lang="ts">
	/**
	 * DetailPane: the one anatomy every collection object uses.
	 *
	 *   1. Header: 42px tile with status dot, name (h2), kind badge, status,
	 *      subtitle, one primary action.
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
	import { ChevronLeft } from 'lucide-svelte';
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
	}

	let {
		name,
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
		children
	}: Props = $props();

	setInDetail();
	const access = getSettingsAccess();
	const uid = $props.id();

	const cat = $derived(category ?? settingsNav.category);
	const itemId = $derived(item ?? settingsNav.selectedItem(cat) ?? '');
	const tabList = $derived(tabs.map((t): DetailTab => (typeof t === 'string' ? { id: t } : t)));
	const current = $derived(
		settingsNav.tabOf(
			cat,
			itemId,
			tabList.map((t) => t.id)
		)
	);
	const showTabs = $derived(tabList.length > 1);
	const catLabel = $derived(categoryById(cat).label);
</script>

<div class="st-dwrap">
	<div class="st-dtop">
		<button type="button" class="st-back" aria-label="Back to {catLabel}" onclick={() => settingsNav.back()}>
			<ChevronLeft /><span>{catLabel}</span>
		</button>
		<SavedTick />
	</div>
	<div class="st-dh">
		{#if tile}{@render tile()}{:else}<Tile size="lg" {icon} text={glyph} {status} ring="bg" />{/if}
		<div class="st-dh-main">
			<div class="st-dhn">
				<h2 tabindex="-1" data-settings-heading="detail">{name}</h2>
				{#if badge}<span class="badge">{badge}</span>{/if}
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
			onselect={(t) => itemId && settingsNav.setTab(cat, itemId, t)}
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
		max-width: 700px;
		padding: 20px 32px 96px;
	}
	.st-dtop {
		display: flex;
		align-items: center;
		gap: 8px;
		min-height: 22px;
	}
	.st-dh {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		gap: 14px;
		align-items: center;
		padding: 2px 0 18px;
	}
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
		font-size: 17px;
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
		font-size: 12.5px;
		color: var(--st-mfg);
		margin-top: 4px;
		align-items: center;
	}
	.status {
		display: inline-flex;
		align-items: center;
		gap: 7px;
		white-space: nowrap;
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
