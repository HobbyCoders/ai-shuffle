<script lang="ts">
	import '$lib/components/ui/surfaces.css';
	/**
	 * UniversalModal - A universal modal component with tabbed interface
	 *
	 * Design inspired by modern settings dialogs with:
	 * - Header with icon and title
	 * - Optional tab navigation
	 * - Two-column layout option for wider content
	 * - Fullscreen on mobile (like sessions panel)
	 * - Footer with cancel/save actions
	 */
	import type { Snippet } from 'svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';

	interface Tab {
		id: string;
		label: string;
	}

	interface Props {
		open: boolean;
		title: string;
		icon?: string; // SVG path for the header icon
		tabs?: Tab[];
		activeTab?: string;
		onTabChange?: (tabId: string) => void;
		onClose: () => void;
		onSave?: () => void;
		saveLabel?: string;
		cancelLabel?: string;
		saveDisabled?: boolean;
		saving?: boolean;
		showFooter?: boolean;
		size?: 'sm' | 'md' | 'lg' | 'xl' | 'full';
		twoColumn?: boolean;
		children?: Snippet;
		left?: Snippet;
		right?: Snippet;
	}

	let {
		open,
		title,
		icon,
		tabs = [],
		activeTab = '',
		onTabChange,
		onClose,
		onSave,
		saveLabel = 'Save',
		cancelLabel = 'Cancel',
		saveDisabled = false,
		saving = false,
		showFooter = true,
		size = 'lg',
		twoColumn = false,
		children,
		left,
		right
	}: Props = $props();

	// Handle escape key
	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			e.preventDefault();
			onClose();
		}
	}

</script>

<svelte:window onkeydown={handleKeydown} />
{#if open}
 <div class="ui-overlay ui-surface" role="dialog" aria-modal="true" aria-labelledby="modal-title" tabindex="-1" use:focusTrap>
  <button class="ui-scrim" onclick={onClose} tabindex="-1" aria-label="Close modal"></button>
  <div class="ui-dialog" style:--ui-dialog-width={size === 'sm' ? '448px' : size === 'md' ? '520px' : size === 'lg' ? '672px' : size === 'xl' ? '896px' : '1152px'}>
   <header class="ui-dialog-header">
    <div class="modal-heading">
     {#if icon}<svg class="heading-icon" aria-hidden="true" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d={icon} /></svg>{/if}
     <h2 id="modal-title" class="ui-dialog-title">{title}</h2>
    </div>
    <button onclick={onClose} class="ui-button ghost icon" aria-label="Close"><svg width="16" height="16" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M6 18L18 6M6 6l12 12" /></svg></button>
   </header>
   {#if tabs.length > 0}
    <div class="modal-tabs" role="tablist">
     {#each tabs as tab}<button role="tab" aria-selected={activeTab === tab.id} onclick={() => onTabChange?.(tab.id)}>{tab.label}</button>{/each}
    </div>
   {/if}
   <div class="ui-dialog-body">
    {#if twoColumn}<div class="modal-columns"><div>{@render left?.()}</div><div>{@render right?.()}</div></div>
    {:else}{@render children?.()}{/if}
   </div>
   {#if showFooter}
    <footer class="ui-dialog-footer">
     <button onclick={onClose} class="ui-button">{cancelLabel}</button>
     {#if onSave}<button onclick={onSave} disabled={saveDisabled || saving} aria-busy={saving} class="ui-button primary">
      {#if saving}<svg class="animate-spin" width="16" height="16" fill="none" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2" stroke-dasharray="40 20" /></svg>{/if}{saveLabel}
     </button>{/if}
    </footer>
   {/if}
  </div>
 </div>
{/if}
<style>
 .modal-heading { display: flex; align-items: center; gap: 8px; min-width: 0; }
 .heading-icon { width: 18px; height: 18px; flex: none; color: var(--muted-foreground); }
 .modal-tabs { display: flex; gap: 16px; overflow-x: auto; padding: 8px 20px 0; border-bottom: 1px solid var(--border); }
 .modal-tabs button { background: transparent; color: var(--muted-foreground); border: 0; border-bottom: 2px solid transparent; padding: 8px 0; font: inherit; cursor: pointer; white-space: nowrap; }
 .modal-tabs button[aria-selected='true'] { border-bottom-color: var(--foreground); color: var(--foreground); }
 .modal-tabs button:hover { color: var(--foreground); }
 .modal-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
 .modal-columns > div { min-width: 0; }
 @media (max-width: 600px) { .modal-columns { grid-template-columns: 1fr; gap: 16px; } .modal-tabs button { min-height: 44px; } }
</style>
