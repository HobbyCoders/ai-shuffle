<script lang="ts">
	import { tick } from 'svelte';
	import { NO_ADJUSTABLE_EFFORT } from '$lib/constants/model-effort';
	import { isMobileUi } from '$lib/stores/platform.svelte';
	import { ArrowLeft, ChevronRight, Zap, Lock } from 'lucide-svelte';
	import ContextPillBar from '$lib/components/chat/ContextPillBar.svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
	import type { PickerGroup } from '$lib/constants/model-picker';
	import type { ContextWindowChoice } from '$lib/constants/model-context';

	// The prop contract is owned by the shared seam so ControlsDock (in $lib)
	// and this sheet (mobile app) cannot drift apart.
	import type {
		ComposerSheetProps as Props,
		ComposerControlOption as ControlOption,
		ComposerModelOption as ModelOption
	} from '$lib/components/chat/composer-shell';
	let {
		open, onClose, panelId = 'mobile-composer-panel', inertToolbar = false,
		modelOptions, modelGroups, modelValue, modelLabel, modelNotice, onModelSelect, modelLocked = false,
		contextChoice, onContextChoiceSelect, effortOptions, effortValue, onEffortSelect,
		effortExplanation = NO_ADJUSTABLE_EFFORT, effortKnown = true,
		fastSupported = false, fastEnabled = false, onToggleFast, showCodeControls = true,
		showSetup = true, showEffort = true,
		permissionOptions, permissionValue, permissionUpdating = false, onPermissionSelect
	}: Props = $props();

	let page = $state<'overview' | 'models'>('overview');
	$effect(() => { if (modelLocked) page = 'overview'; });
	let panelRef = $state<HTMLElement | null>(null);
	let modelButtonRef = $state<HTMLButtonElement | null>(null);
	let panelHeight = $state(0);

	const railGroups = $derived.by<PickerGroup[]>(() => {
		if (modelGroups?.length) return modelGroups;
		const groups = new Map<string, PickerGroup>();
		for (const option of modelOptions) {
			if (!groups.has(option.groupKey)) groups.set(option.groupKey, {
				key: option.groupKey, name: option.group, subtext: option.groupSubtext,
				glyph: 'custom', isApi: false
			});
		}
		return [...groups.values()];
	});
	const permissionDescription = $derived(permissionOptions.find(o => o.value === permissionValue)?.description);
	const permissionLabel = $derived(permissionOptions.find(o => o.value === permissionValue)?.label ?? 'Ask');

	function closePanel() { onClose(); }
	async function openPage(next: 'models') {
		if (modelLocked) return;
		page = next;
		await tick();
		panelRef?.focus({ preventScroll: true });
	}
	async function back() {
		page = 'overview';
		await tick();
		modelButtonRef?.focus({ preventScroll: true });
	}
	function handleKeydown(event: KeyboardEvent) {
		if (event.defaultPrevented || event.key !== 'Escape' || !open) return;
		event.preventDefault();
		event.stopPropagation();
		if (page !== 'overview') void back(); else closePanel();
	}
	// Only the empty mobile surface needs a measured budget. Subtract the
	// actual editor, attachments, toolbar and dock gutters, not a fixed estimate.
	function emptyPanelBudget(node: HTMLElement, expanded: boolean) {
		const surface = node.closest<HTMLElement>('.chat-view');
		const slot = node.closest<HTMLElement>('.input-slot');
		const reveal = node.parentElement;
		if (!surface || !slot || !reveal) return;
		let revealFrame = 0;
		function revealFocus() {
			cancelAnimationFrame(revealFrame);
			revealFrame = requestAnimationFrame(() => {
				const focused = document.activeElement;
				if (slot?.hasAttribute('data-panel-flow') && focused instanceof HTMLElement && slot.contains(focused)) {
					focused.scrollIntoView({ block: 'nearest' });
				}
			});
		}
		function measure() {
			if (!surface || !slot || !reveal) return;
			if (!expanded || !isMobileUi() || !surface.classList.contains('is-empty')) {
				node.style.removeProperty('--empty-panel-limit');
				slot.removeAttribute('data-panel-flow');
				return;
			}
			const style = getComputedStyle(slot);
			const gutters = (parseFloat(style.marginTop) || 0) + (parseFloat(style.marginBottom) || 0);
			const nonPanelHeight = slot.getBoundingClientRect().height - reveal.getBoundingClientRect().height + gutters;
			// A sticky dock must leave a real window onto the scrollable hero.
			// Below a 160px touch panel + 96px hero window, scroll the whole dock
			// in normal flow instead of trapping either region behind the other.
			const available = surface.clientHeight - nonPanelHeight - 96;
			const normalFlow = available < 160;
			const wasNormalFlow = slot.hasAttribute('data-panel-flow');
			slot.toggleAttribute('data-panel-flow', normalFlow);
			node.style.setProperty('--empty-panel-limit', `${Math.min(390, Math.max(160, normalFlow ? surface.clientHeight - nonPanelHeight : available))}px`);
			if (normalFlow && !wasNormalFlow) revealFocus();
		}
		const observer = new ResizeObserver(measure);
		for (const element of [surface, slot, reveal]) observer.observe(element);
		const classes = new MutationObserver(measure);
		classes.observe(surface, { attributes: true, attributeFilter: ['class'] });
		window.addEventListener('resize', measure);
		slot.addEventListener('focusin', revealFocus);
		measure();
		return {
			update(value: boolean) { expanded = value; measure(); },
			destroy() {
				observer.disconnect();
				classes.disconnect();
				cancelAnimationFrame(revealFrame);
				slot.removeAttribute('data-panel-flow');
				slot.removeEventListener('focusin', revealFocus);
				window.removeEventListener('resize', measure);
			}
		};
	}
	$effect(() => {
		if (!open) page = 'overview';
	});
</script>

<!-- Escape bubbles from panel controls; this group adds no tab stop. -->
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="mobile-composer-controls" onkeydown={handleKeydown} role="group" aria-label="Composer controls">
	<div
		class="panel-reveal"
		style:height={`${open ? panelHeight : 0}px`}
		inert={!open || inertToolbar}
		aria-hidden={!open}
	>
		<section
			id={panelId}
			bind:this={panelRef}
			use:emptyPanelBudget={open}
			bind:clientHeight={panelHeight}
			class="controls-panel"
			class:models={page === 'models'}
			aria-label={page === 'models' ? 'Choose model' : 'Chat controls'}
			tabindex="-1"
		>
			{#if page === 'overview'}
				<button bind:this={modelButtonRef} type="button" class="model-control" disabled={modelLocked} onclick={() => openPage('models')} aria-label="Model: {modelLabel}">
					<span class="control-label">Model</span><strong>{modelLabel}</strong>{#if modelLocked}<Lock size={16} aria-hidden="true" />{:else}<ChevronRight size={16} aria-hidden="true" />{/if}
				</button>
				{#if modelNotice}<p class="model-notice">{modelNotice}</p>{/if}
				{#if open && showSetup}
					<ContextPillBar sheetMode />
				{/if}
				{#if showEffort}
				<div class="quick-setting" role="group" aria-label="Reasoning effort">
					<div class="setting-label"><span>Reasoning effort</span><small>{!effortKnown ? (effortValue ?? 'Unknown') : !effortOptions.length ? 'Not adjustable' : effortValue ? '' : 'Auto'}</small></div>
					{#if !effortOptions.length}
						<p class="setting-description">{effortExplanation}</p>
					{:else}
					<div class="effort-choices" style={`grid-template-columns: repeat(${Math.min(effortOptions.length, 4)}, minmax(0, 1fr))`}>
						{#each effortOptions as option (option.value)}
							<button type="button" class="quick-choice" disabled={modelLocked} aria-pressed={option.value === effortValue}
								title={option.description} onclick={() => onEffortSelect(option.value)}>{option.label}</button>
						{/each}
					</div>
					{/if}
				</div>
				{/if}
				{#if showCodeControls}
					<div class="quick-setting" role="group" aria-label="Tool access" aria-busy={permissionUpdating}>
						<div class="setting-label"><span>Tool access</span></div>
						<div class="access-choices">
							{#each permissionOptions as option (option.value)}
								<button type="button" class="quick-choice" disabled={permissionUpdating} aria-pressed={option.value === permissionValue}
									aria-label={`${option.label}: ${option.description}`} title={option.description}
									onclick={() => onPermissionSelect(option.value)}>{option.label}</button>
							{/each}
						</div>
						<p class="setting-description" aria-live="polite">{permissionUpdating ? 'Saving tool access…' : `${permissionLabel} · ${permissionDescription}`}</p>
					</div>
				{/if}
				{#if showEffort && fastSupported && !modelLocked}
					<div class="fast-row">
						<div class="fast-copy"><span><Zap size={14} aria-hidden="true" />Fast mode</span><small id={`${panelId}-fast-help`}>Higher price; switching clears the prompt cache.</small></div>
						<button type="button" class="fast-switch" role="switch" aria-label="Fast mode" aria-describedby={`${panelId}-fast-help`} aria-checked={fastEnabled} onclick={() => onToggleFast?.()}><span></span></button>
					</div>
				{/if}
			{:else}
				<header class="panel-header">
					<button type="button" class="back-button" aria-label="Back to chat controls" onclick={back}><ArrowLeft size={18} aria-hidden="true" /></button>
					<h2>Choose model</h2>
				</header>
				<div class="mobile-model-picker">
					<ModelPickerPanel groups={railGroups} options={modelOptions} value={modelValue}
						onSelect={(value) => { onModelSelect(value); void back(); }} notice={modelNotice}
						{contextChoice} {onContextChoiceSelect} onRequestClose={back} />
				</div>
			{/if}
		</section>
	</div>
</div>

<style>
	.mobile-composer-controls { display: none; }
	/* The integrated controls sheet exists only in the mobile shell. */
	:global(html[data-platform='mobile']) .mobile-composer-controls { display: block; min-width: 0; }
	.panel-reveal { overflow: hidden; transition: height 280ms cubic-bezier(0.22, 1, 0.36, 1); border-radius: 0 0 20px 20px; background: var(--popover); }
	.panel-reveal[aria-hidden='false'] { box-shadow: inset 0 1px 0 var(--border); }
	.controls-panel {
		--mobile-panel-limit: min(calc(var(--app-viewport-height, 100dvh) * 0.48), max(72px, calc(var(--app-viewport-height, 100dvh) - 420px)), 390px);
		--composer-control-bg: var(--card);
		--composer-control-radius: 10px;
		display: flex; flex-direction: column; gap: 14px; padding: 10px 14px 14px;
		box-sizing: border-box; max-height: var(--empty-panel-limit, var(--mobile-panel-limit)); overflow-y: auto; overscroll-behavior: contain;
		-webkit-overflow-scrolling: touch; touch-action: pan-y; color: var(--foreground); outline: none;
	}
	.controls-panel > :global(*) { flex-shrink: 0; }
	.model-control { display: flex; align-items: center; gap: 8px; width: 100%; min-width: 0; min-height: 44px; border: 0; border-bottom: 1px solid var(--border-subtle); border-radius: 0; padding: 0 2px 8px; background: transparent; color: inherit; cursor: pointer; font: inherit; text-align: left; }
	.model-control strong { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.83rem; font-weight: 600; text-align: right; }
	.control-label, .setting-label { font-size: 0.72rem; font-weight: 500; line-height: 1.4; color: var(--muted-foreground); }
	.setting-label { display: flex; justify-content: space-between; align-items: center; margin: 0 2px 6px; }
	.setting-label small { font: inherit; }
	.effort-choices, .access-choices { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 2px; padding: 3px; border: 1px solid var(--border-subtle); border-radius: 11px; background: var(--card); }
	.quick-choice { min-width: 0; min-height: 44px; padding: 4px 2px; border: 0; border-radius: 7px; background: transparent; color: var(--muted-foreground); font: inherit; font-size: 0.8rem; line-height: 1.25; cursor: pointer; transition: background 140ms ease, color 140ms ease; }
	.quick-choice[aria-pressed='true'] { background: color-mix(in oklch, var(--primary) 11%, var(--card)); box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 30%, transparent); color: var(--primary); font-weight: 600; }
	.setting-description { min-height: 2.6em; margin: 6px 2px 0; font-size: 0.68rem; line-height: 1.3; color: var(--muted-foreground); }
	.fast-row { display: flex; align-items: center; gap: 12px; padding: 0 2px; flex-shrink: 0; }
	.fast-copy { flex: 1; min-width: 0; }
	.fast-copy > span { display: flex; align-items: center; gap: 6px; font-size: 0.78rem; font-weight: 600; }
	.fast-copy small { display: block; margin-top: 3px; color: var(--muted-foreground); font-size: 0.68rem; line-height: 1.35; }
	.fast-switch { display: flex; align-items: center; width: 48px; height: 44px; padding: 0 4px; flex-shrink: 0; border: 0; border-radius: 10px; background: transparent; cursor: pointer; }
	.fast-switch > span { position: relative; width: 40px; height: 24px; border-radius: 999px; background: var(--muted-foreground); transition: background 160ms ease; }
	.fast-switch > span::after { content: ''; position: absolute; top: 3px; left: 3px; width: 18px; height: 18px; background: var(--card); border-radius: 50%; transition: transform 160ms ease; }
	.fast-switch[aria-checked='true'] > span { background: var(--primary); }
	.fast-switch[aria-checked='true'] > span::after { transform: translateX(16px); background: var(--primary-foreground); }
	.panel-header { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
	.panel-header h2 { margin: 0; font-size: 0.85rem; font-weight: 600; }
	.back-button { width: 44px; height: 44px; display: grid; place-items: center; border: 0; border-radius: 12px; background: var(--muted); color: inherit; cursor: pointer; }
	.controls-panel.models {
		--mobile-panel-limit: min(390px, max(160px, calc(var(--chat-surface-height, var(--app-viewport-height, 100dvh)) - 168px)));
		height: var(--empty-panel-limit, var(--mobile-panel-limit)); gap: 8px;
	}
	/* A keyboard may leave less space than the search + context footer need.
	   Keep a usable list, and let this picker panel scroll to expose its footer. */
	.controls-panel.models > .mobile-model-picker { flex: 1 0 210px; min-height: 210px; --mpp-width: 100%; --mpp-height: 100%; }
	.mobile-model-picker :global(.mpp-list), .mobile-model-picker :global(.mpp-rail) { touch-action: pan-y; -webkit-overflow-scrolling: touch; }
	.model-notice { margin: 0; color: var(--warning); font-size: 0.72rem; }
	.access-choices button:disabled { opacity: 0.55; cursor: wait; }
	button:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	button:active { filter: brightness(1.1); }
	@media (prefers-reduced-motion: reduce) {
		.panel-reveal, .quick-choice, .fast-switch > span, .fast-switch > span::after { transition: none; }
	}
</style>
