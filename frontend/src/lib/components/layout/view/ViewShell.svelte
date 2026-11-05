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
No host (tests), an overlay sidebar (<=1080px), or the phone shell: inline.
Panel contexts and tokens travel automatically. Off-screen feature pages never claim.
-->
<script lang="ts">
	import { getAllContexts, getContext, onMount, untrack, type Snippet } from 'svelte';
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
		/** Optional observation, e.g. a page that auto-selects its first item only on desktop. */
		stacked?: boolean;
	}
	let { panel, main, label, detailOpen = false, onBack, manageHistory = true, inheritScope = false,
		stacked = $bindable(false) }: Props = $props();
	const contexts = getAllContexts();
	const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	const active = $derived(feature?.active ?? true);
	const token = Symbol('view-panel');
	const historyKey = $props.id();
	const docked = $derived(viewPanel.hostMounted && !layout.current.sidebarOverlay && !platform.isMobile);
	let width = $state(0);
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
		const open = active && stacked && detailOpen;
		if (manageHistory && open && !previousOpen) {
			untrack(() => {
				try {
					pushState('', { ...page.state, viewPanelDetail: historyKey } as App.PageState);
					pushed = true;
				} catch { /* Routerless tests still have an in-page back. */ }
			});
		}
		if (manageHistory && active && stacked && !detailOpen && previousOpen && pushed) {
			pushed = false;
			history.back();
		}
		previousOpen = open;
	});
	let firstHistory = true;
	$effect(() => {
		const entry = (page.state as { viewPanelDetail?: string } | undefined)?.viewPanelDetail;
		if (firstHistory) { firstHistory = false; return; }
		untrack(() => {
			if (manageHistory && active && stacked && pushed && entry !== historyKey) {
				pushed = false;
				onBack?.();
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
	data-pane={stacked ? (detailOpen ? 'detail' : 'panel') : undefined}>
	{#if !docked}
		<div class="inline-panel" hidden={stacked && detailOpen} inert={stacked && detailOpen}>
			{@render bridgedPanel()}
		</div>
	{/if}
	<section class="view-main-slot st-dpane st-scroll" aria-label="Details" hidden={stacked && !detailOpen} inert={stacked && !detailOpen}>
		{@render main(back)}
	</section>
</div>

<style>
	.view-shell { display: grid; grid-template-columns: var(--view-panel-width, 264px) minmax(0, 1fr); height: 100%; min-height: 0; min-width: 0; }
	.view-shell[data-docked], .view-shell[data-stacked] { grid-template-columns: minmax(0, 1fr); }
	.inline-panel { min-height: 0; min-width: 0; border-right: 1px solid var(--sidebar-border); }
	.view-main-slot { display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: auto; container: st-detail / inline-size; }
	.inline-panel[hidden], .view-main-slot[hidden] { display: none; }
	.view-shell[data-stacked] .inline-panel { border-right: 0; }
</style>
