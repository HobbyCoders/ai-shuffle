<!--
Usage (all kit components export from $lib/components/layout/view):
<script lang="ts">
  import { ViewShell, ViewPanel, ViewMain, PanelIconButton, ListItem, DetailPane, EmptyDetail } from '$lib/components/layout/view';
  import { Plus } from 'lucide-svelte';
  let selected = $state(false);
</script>
<ViewShell label="Notes" detailOpen={selected} onBack={() => selected = false}>
  {#snippet panel()}
    <ViewPanel title="Notes">
      {#snippet actions()}<PanelIconButton label="New note" icon={Plus} onclick={() => selected = true} />{/snippet}
      <ListItem id="one" name="A note" selected={selected} onselect={() => selected = true} />
    </ViewPanel>
  {/snippet}
  {#snippet main(back)}
    {#if selected}
      <DetailPane name="A note" onBack={back}><p>Note content.</p></DetailPane>
    {:else}
      <ViewMain width="reading"><EmptyDetail title="Pick a note" message="Choose one from the list.">
        <button class="st-btn primary" onclick={() => selected = true}>New note</button>
      </EmptyDetail></ViewMain>
    {/if}
  {/snippet}
</ViewShell>
Pass main's `back` to DetailPane's onBack (or your detail's back control).
Keep detailOpen true when an item is selected; Shell owns phone push/pop history.
No host (tests), below the desktop rail (<768px), or the phone shell: inline.
Panel contexts and tokens travel automatically. Off-screen feature pages never claim.
-->
<script lang="ts" module>
	// Retained destinations share the browser stack: retire the old step before
	// another destination may push its own. A pop is asynchronous.
	const detailHistory = $state({ retiring: null as string | null });
</script>
<script lang="ts">
	import { getAllContexts, getContext, onMount, tick, untrack, type Snippet } from 'svelte';
	import { page } from '$app/state';
	import { pushState } from '$app/navigation';
	import { layout } from '$lib/stores/layout.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { viewPanel } from '$lib/stores/view-panel.svelte';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '../feature-panel-context';
	import PanelContext from './PanelContext.svelte';
	import '../../settings/shell/settings.css';

	interface Props {
		panel: Snippet;
		main: Snippet<[back: () => void]>;
		label: string;
		detailOpen?: boolean;
		onBack?: () => void;
		/** Settings already owns its multi-level history; other pages leave this on. */
		manageHistory?: boolean;
		/** Existing Settings/Agents provide their own stacked token scope. */
		inheritScope?: boolean;
		/** Legacy Settings owns inner page/detail panes; its outer slot is not a detail pane. */
		nestedPanes?: boolean;
		/** Content-first data destinations keep their main visible on phones, without a detail Back step. */
		contentFirst?: boolean;
		/** Optional observation, e.g. a page that auto-selects its first item only on desktop. */
		stacked?: boolean;
	}
	let { panel, main, label, detailOpen = false, onBack, manageHistory = true, inheritScope = false, nestedPanes = false, contentFirst = false,
		stacked = $bindable(false) }: Props = $props();
	const contexts = getAllContexts();
	const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	const active = $derived(feature?.active ?? true);
	const token = Symbol('view-panel');
	const historyKey = $props.id();
	let width = $state(0);
	const docked = $derived(viewPanel.hostMounted && !platform.isMobile && (!layout.current.sidebarOverlay || width >= 768));
	onMount(() => {
		const measure = () => { width = window.innerWidth; };
		measure();
		window.addEventListener('resize', measure);
		return () => window.removeEventListener('resize', measure);
	});
	$effect(() => { stacked = !docked && (platform.isMobile || (width > 0 && width <= 600)); });

	$effect(() => {
		if (!active || !docked) return;
		viewPanel.claim(token, bridgedPanel);
		return () => viewPanel.release(token);
	});

	let pushed = false;
	let previousOpen = false;
	$effect(() => {
		const open = active && stacked && detailOpen && !contentFirst;
		const retiring = detailHistory.retiring;
		if (!manageHistory) return;
		// Leaving a retained destination must remove its invisible Back stop.
		if (!active && pushed) {
			pushed = false;
			if (untrack(() => (page.state as { viewPanelDetail?: string }).viewPanelDetail) === historyKey) {
				detailHistory.retiring = historyKey;
				history.back();
			}
		}
		if (active && stacked && !detailOpen && previousOpen && pushed) {
			pushed = false;
			history.back();
		}
		previousOpen = open;
		if (open && !pushed && !retiring) {
			let cancelled = false;
			// Let every retained destination retire first, irrespective of effect order.
			void tick().then(() => {
				if (cancelled || detailHistory.retiring) return;
				try {
					if ((page.state as { viewPanelDetail?: string }).viewPanelDetail !== historyKey) {
						pushState('', { ...page.state, viewPanelDetail: historyKey } as App.PageState);
					}
					pushed = true;
				} catch { /* Routerless tests still have an in-page back. */ }
			});
			return () => { cancelled = true; };
		}
	});
	let firstHistory = true;
	$effect(() => {
		const entry = (page.state as { viewPanelDetail?: string } | undefined)?.viewPanelDetail;
		if (detailHistory.retiring && entry !== detailHistory.retiring) detailHistory.retiring = null;
		if (firstHistory) { firstHistory = false; return; }
		untrack(() => {
			if (manageHistory && pushed && entry !== historyKey) {
				// A pop while wide/off screen still releases our step; it must not block
				// a fresh claim the next time this selected detail becomes stacked.
				pushed = false;
				if (active && stacked) onBack?.();
			}
		});
	});
	function back() {
		if (manageHistory && stacked && pushed) {
			history.back();
			return;
		}
		onBack?.();
	}
</script>

{#snippet bridgedPanel()}<PanelContext {contexts}>{@render panel()}</PanelContext>{/snippet}

<div class="view-shell" class:settings-shell={!inheritScope} data-view-shell={label}
	style:--view-panel-width={`${(layout.current?.sidebarWidth ?? 320) - 56}px`}
	data-docked={docked || undefined} data-stacked={stacked ? '' : undefined}
	data-content-first={contentFirst || undefined}
	data-pane={stacked ? (contentFirst || detailOpen ? 'detail' : 'panel') : undefined}>
	{#if !docked}
		<div class="inline-panel" hidden={stacked && (contentFirst || detailOpen)} inert={stacked && (contentFirst || detailOpen)}>
			{@render bridgedPanel()}
		</div>
	{/if}
	<section class="view-main-slot st-scroll" class:settings-viewport={nestedPanes} class:st-dpane={!nestedPanes} aria-label="Details" hidden={stacked && !contentFirst && !detailOpen} inert={stacked && !contentFirst && !detailOpen}>
		<div class="view-titlebar" aria-hidden="true"></div>
		{@render main(back)}
	</section>
</div>

<style>
	.view-shell { display: grid; grid-template-columns: var(--view-panel-width, 264px) minmax(0, 1fr); height: 100%; min-height: 0; min-width: 0; }
	.view-shell[data-docked], .view-shell[data-stacked] { grid-template-columns: minmax(0, 1fr); }
	.inline-panel { min-height: 0; min-width: 0; border-right: 1px solid var(--sidebar-border); }
	.view-main-slot { position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: auto; scrollbar-gutter: stable; container: st-detail / inline-size; }
	/* One stable, invisible window-drag band, including when no object is
	   selected. Sticky so it stays at the top while the detail scrolls; the
	   negative margin keeps it out of layout. It precedes the controls in
	   document order so their Electron no-drag regions win. */
	.view-titlebar { position: sticky; top: 0; flex: none; height: var(--titlebar-height, 52px); margin-bottom: calc(-1 * var(--titlebar-height, 52px)); pointer-events: none; }
	.view-shell[data-stacked] .view-titlebar { display: none; }
	.inline-panel[hidden], .view-main-slot[hidden] { display: none; }
	.view-shell[data-stacked] .inline-panel { border-right: 0; }
	.view-shell[data-stacked] .view-main-slot { scrollbar-gutter: auto; }
	.view-shell[data-stacked][data-content-first] :global(.st-dtop .st-back) { display: none; }
</style>
