<script lang="ts">
	import { tick } from 'svelte';
	import {
		ArrowLeft,
		Check,
		ChevronDown,
		ChevronRight,
		SlidersHorizontal
	} from 'lucide-svelte';
	import ContextPillBar from './ContextPillBar.svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
	import type { PickerGroup } from '$lib/constants/model-picker';
	import type { ContextWindowChoice } from '$lib/constants/model-context';

	interface ControlOption {
		value: string;
		label: string;
		description: string;
	}

	interface ModelOption extends ControlOption {
		group: string;
		groupKey: string;
		groupSubtext: string | null;
	}

	interface Props {
		open: boolean;
		onClose: () => void;
		modelOptions: ModelOption[];
		/** Provider groups backing the horizontal rail (T3-style picker). When
		 *  omitted the rail is derived from the options without brand glyphs. */
		modelGroups?: PickerGroup[];
		modelValue: string;
		modelLabel: string;
		modelNotice?: string;
		onModelSelect: (value: string) => void;
		contextChoice: ContextWindowChoice;
		onContextChoiceSelect: (choice: ContextWindowChoice) => void;
		effortOptions: ControlOption[];
		effortValue: string | null;
		onEffortSelect: (value: string) => void;
		/** Whether the picked model carries the fast service tier. */
		fastSupported?: boolean;
		fastEnabled?: boolean;
		onToggleFast?: () => void;
		/** Code and voice share project setup and tool access; chat omits them. */
		showCodeControls?: boolean;
		permissionOptions: ControlOption[];
		permissionValue: string | null;
		onPermissionSelect: (value: string) => void;
	}

	let {
		open,
		onClose,
		modelOptions,
		modelGroups,
		modelValue,
		modelLabel,
		modelNotice,
		onModelSelect,
		contextChoice,
		onContextChoiceSelect,
		effortOptions,
		effortValue,
		onEffortSelect,
		fastSupported = false,
		fastEnabled = false,
		onToggleFast,
		showCodeControls = true,
		permissionOptions,
		permissionValue,
		onPermissionSelect
	}: Props = $props();

	type SheetPage = 'overview' | 'models';
	type SheetMenu = 'effort' | 'permission' | null;
	let page = $state<SheetPage>('overview');
	let openMenu = $state<SheetMenu>(null);
	let sheetRef = $state<HTMLElement | null>(null);
	let dragging = $state(false);
	let dragOffset = $state(0);
	let dragStartY = 0;
	let dragStartedAt = 0;

	// ModelPickerPanel owns the catalog interaction everywhere. This fallback only
	// supplies neutral group metadata for isolated callers/tests that omit groups.
	const railGroups = $derived.by<PickerGroup[]>(() => {
		if (modelGroups && modelGroups.length > 0) return modelGroups;
		const seen = new Map<string, PickerGroup>();
		for (const option of modelOptions) {
			if (!seen.has(option.groupKey)) {
				seen.set(option.groupKey, {
					key: option.groupKey,
					name: option.group,
					subtext: option.groupSubtext,
					glyph: 'custom',
					isApi: false
				});
			}
		}
		return [...seen.values()];
	});

	const effortLabel = $derived(
		effortValue
			? (effortOptions.find((option) => option.value === effortValue)?.label ?? 'Effort')
			: 'Auto'
	);
	const permissionLabel = $derived(
		permissionOptions.find((option) => option.value === permissionValue)?.label ?? 'Ask'
	);

	function toggleMenu(menu: Exclude<SheetMenu, null>) {
		openMenu = openMenu === menu ? null : menu;
	}

	function closeSheet() {
		page = 'overview';
		openMenu = null;
		dragging = false;
		dragOffset = 0;
		onClose();
	}

	async function openModels() {
		openMenu = null;
		page = 'models';
		await tick();
		// Keep focus on the dialog so opening the picker does not summon a
		// software keyboard or immediately route typing into model search.
		sheetRef?.focus();
	}

	function handleKeydown(event: KeyboardEvent) {
		if (!open) return;
		if (event.key === 'Escape') {
			event.preventDefault();
			if (page === 'models') {
				page = 'overview';
			} else {
				closeSheet();
			}
			return;
		}

		if (event.key === 'Tab' && sheetRef) {
			const focusable = [...sheetRef.querySelectorAll<HTMLElement>(
				'button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex="-1"])'
			)].filter((element) => !element.hasAttribute('hidden'));
			if (focusable.length === 0) return;
			const first = focusable[0];
			const last = focusable[focusable.length - 1];
			if (event.shiftKey && document.activeElement === first) {
				event.preventDefault();
				last?.focus();
			} else if (!event.shiftKey && document.activeElement === last) {
				event.preventDefault();
				first?.focus();
			}
		}
	}

	function handleDragStart(event: PointerEvent) {
		if (event.button !== 0) return;
		dragging = true;
		dragStartY = event.clientY;
		dragStartedAt = performance.now();
		dragOffset = 0;
		(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
	}

	function handleDragMove(event: PointerEvent) {
		if (!dragging) return;
		dragOffset = Math.max(0, event.clientY - dragStartY);
	}

	function handleDragEnd(event: PointerEvent) {
		if (!dragging) return;
		const elapsed = Math.max(performance.now() - dragStartedAt, 1);
		const velocity = dragOffset / elapsed;
		const closeThreshold = Math.min(96, (sheetRef?.offsetHeight ?? 480) * 0.18);
		dragging = false;
		if ((event.currentTarget as HTMLElement).hasPointerCapture(event.pointerId)) {
			(event.currentTarget as HTMLElement).releasePointerCapture(event.pointerId);
		}
		if (dragOffset >= closeThreshold || velocity > 0.75) {
			closeSheet();
		} else {
			dragOffset = 0;
		}
	}

	function handleDragCancel() {
		dragging = false;
		dragOffset = 0;
	}

	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return {
			destroy() {
				node.remove();
			}
		};
	}

	$effect(() => {
		if (!open) return;
		const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
		const previousOverflow = document.body.style.overflow;
		document.body.style.overflow = 'hidden';
		tick().then(() => sheetRef?.focus());
		return () => {
			document.body.style.overflow = previousOverflow;
			previousFocus?.focus();
		};
	});
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<div use:portal class="sheet-layer" data-composer-owned-overlay="true" role="presentation">
		<button class="sheet-backdrop" type="button" aria-label="Close chat controls" onclick={closeSheet}></button>

		<div
			bind:this={sheetRef}
			class="mobile-sheet"
			class:dragging
			style:transform={dragOffset > 0 ? `translateY(${dragOffset}px)` : undefined}
			role="dialog"
			aria-modal="true"
			aria-labelledby="mobile-controls-title"
			tabindex="-1"
		>
			<div
				class="sheet-grabber"
				role="presentation"
				onpointerdown={handleDragStart}
				onpointermove={handleDragMove}
				onpointerup={handleDragEnd}
				onpointercancel={handleDragCancel}
			><span></span></div>

			<header class="sheet-header">
				{#if page === 'models'}
					<button
						type="button"
						class="header-button"
						aria-label="Back to chat controls"
						onclick={() => (page = 'overview')}
					>
						<ArrowLeft size={20} strokeWidth={1.8} />
					</button>
				{/if}
				<div class="header-copy">
					<h2 id="mobile-controls-title">{page === 'models' ? 'Choose model' : 'Chat controls'}</h2>
					<p>{page === 'models' ? 'Switch the model for this chat' : 'Settings for the next message'}</p>
				</div>
			</header>

			{#if page === 'models'}
				<div class="mobile-model-picker">
					<ModelPickerPanel
						groups={railGroups}
						options={modelOptions}
						value={modelValue}
						onSelect={onModelSelect}
						notice={modelNotice}
						{contextChoice}
						{onContextChoiceSelect}
						onRequestClose={() => (page = 'overview')}
					/>
				</div>
			{:else}
				<div class="sheet-scroll overview-scroll">
					{#if showCodeControls}
						<section class="control-section">
							<div class="section-label"><SlidersHorizontal size={16} strokeWidth={1.8} /><span>Chat setup</span></div>
							<ContextPillBar sheetMode />
						</section>
					{/if}

					<section class="control-section">
						<button
							type="button"
							class="compact-select"
							onclick={openModels}
							aria-label="Model: {modelLabel}"
						>
							<span class="compact-k">Model</span>
							<span class="compact-v">{modelLabel}</span>
							<ChevronRight size={16} strokeWidth={1.8} />
						</button>

						<div class="compact-field">
							<button
								type="button"
								class="compact-select"
								class:open={openMenu === 'effort'}
								onclick={() => toggleMenu('effort')}
								aria-haspopup="listbox"
								aria-expanded={openMenu === 'effort'}
								aria-label="Reasoning effort: {effortLabel}"
							>
								<span class="compact-k">Effort</span>
								<span class="compact-v">{effortLabel}</span>
								<ChevronDown size={16} strokeWidth={1.8} />
							</button>
							{#if openMenu === 'effort'}
								<div class="compact-menu" role="listbox" aria-label="Reasoning effort">
									{#each effortOptions as option (option.value)}
										<button
											type="button"
											class="choice-row"
											class:selected={option.value === effortValue}
											role="option"
											aria-selected={option.value === effortValue}
											onclick={() => {
												onEffortSelect(option.value);
												openMenu = null;
											}}
										>
											<span class="choice-copy">
												<span class="choice-title">{option.label}</span>
												<span class="choice-description">{option.description}</span>
											</span>
											{#if option.value === effortValue}<Check size={16} strokeWidth={2.2} />{/if}
										</button>
									{/each}
								</div>
							{/if}
						</div>

						{#if showCodeControls}
						<div class="compact-field">
							<button
								type="button"
								class="compact-select"
								class:open={openMenu === 'permission'}
								onclick={() => toggleMenu('permission')}
								aria-haspopup="listbox"
								aria-expanded={openMenu === 'permission'}
								aria-label="Tool access: {permissionLabel}"
							>
								<span class="compact-k">Tool access</span>
								<span class="compact-v">{permissionLabel}</span>
								<ChevronDown size={16} strokeWidth={1.8} />
							</button>
							{#if openMenu === 'permission'}
								<div class="compact-menu" role="listbox" aria-label="Tool access">
									{#each permissionOptions as option (option.value)}
										<button
											type="button"
											class="choice-row"
											class:selected={option.value === permissionValue}
											role="option"
											aria-selected={option.value === permissionValue}
											onclick={() => {
												onPermissionSelect(option.value);
												openMenu = null;
											}}
										>
											<span class="choice-copy">
												<span class="choice-title">{option.label}</span>
												<span class="choice-description">{option.description}</span>
											</span>
											{#if option.value === permissionValue}<Check size={16} strokeWidth={2.2} />{/if}
										</button>
									{/each}
								</div>
							{/if}
						</div>
						{/if}

						{#if fastSupported}
							<label class="fast-check">
								<input
									type="checkbox"
									checked={fastEnabled}
									onchange={() => onToggleFast?.()}
								/>
								<span class="fast-copy">
									<strong>Fast mode</strong>
									<small>Faster output at a higher price. Switching clears the prompt cache.</small>
								</span>
							</label>
						{/if}
					</section>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.sheet-layer {
		position: fixed;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: flex-end;
		justify-content: center;
		padding: 0;
	}

	.sheet-backdrop {
		position: absolute;
		inset: 0;
		border: 0;
		background: color-mix(in oklch, #000 52%, transparent);
		backdrop-filter: blur(3px);
		-webkit-backdrop-filter: blur(3px);
		animation: backdrop-in 180ms ease-out;
	}

	.mobile-sheet {
		position: relative;
		width: 100%;
		max-width: 42rem;
		max-height: min(82dvh, 48rem);
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--card) 96%, var(--background));
		border: 1px solid var(--border);
		border-bottom: 0;
		border-radius: 24px 24px 0 0;
		box-shadow: 0 -18px 60px color-mix(in oklch, #000 24%, transparent);
		outline: none;
		overflow: hidden;
		animation: sheet-in 240ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	.mobile-sheet.dragging {
		transition: none;
		user-select: none;
	}

	.sheet-grabber {
		height: 24px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		cursor: grab;
		touch-action: none;
	}

	.sheet-grabber:active {
		cursor: grabbing;
	}

	.sheet-grabber span {
		width: 36px;
		height: 4px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--muted-foreground) 35%, transparent);
	}

	.sheet-header {
		display: flex;
		align-items: center;
		gap: 10px;
		min-height: 48px;
		padding: 2px 16px 10px;
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.header-copy {
		min-width: 0;
		flex: 1;
	}

	.header-copy h2 {
		margin: 0;
		font-size: 1rem;
		font-weight: 650;
		letter-spacing: -0.01em;
		color: var(--foreground);
		line-height: 1.25;
	}

	.header-copy p {
		margin: 2px 0 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.35;
	}

	.header-button {
		width: 40px;
		height: 40px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: var(--muted);
		color: var(--foreground);
		cursor: pointer;
	}

	.sheet-scroll {
		min-height: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
		-webkit-overflow-scrolling: touch;
	}

	.overview-scroll {
		padding: 10px 14px calc(16px + env(safe-area-inset-bottom, 0px));
	}

	.control-section + .control-section {
		margin-top: 12px;
	}

	.compact-select,
	.compact-menu {
		background: color-mix(in oklch, var(--muted) 55%, var(--card));
		border: 1px solid var(--border-subtle);
		border-radius: 12px;
		box-shadow: 0 1px 0 color-mix(in oklch, var(--foreground) 4%, transparent);
	}

	.section-label {
		display: flex;
		align-items: center;
		gap: 7px;
		min-height: 20px;
		margin: 0 3px 8px;
		color: var(--muted-foreground);
		font-size: 0.76rem;
		font-weight: 600;
		line-height: 1;
	}

	.section-label :global(svg) {
		width: 16px;
		height: 16px;
		flex: 0 0 16px;
		color: var(--subtle-foreground);
	}

	.compact-field {
		margin-top: 8px;
	}

	.compact-select {
		width: 100%;
		height: 40px;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 12px;
		color: var(--foreground);
		cursor: pointer;
		text-align: left;
	}

	.compact-select.open {
		box-shadow: inset 0 0 0 1px var(--border-strong);
	}

	.compact-k {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.compact-v {
		min-width: 0;
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		text-align: right;
		font-size: 0.86rem;
		font-weight: 600;
	}

	.compact-select :global(svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.compact-menu {
		margin-top: 6px;
		overflow: hidden;
	}

	.fast-check {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		margin-top: 10px;
		padding: 4px 2px;
		cursor: pointer;
	}

	.fast-check input {
		width: 16px;
		height: 16px;
		margin-top: 3px;
		flex-shrink: 0;
		accent-color: var(--primary);
	}

	.fast-copy {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.fast-copy strong {
		font-size: 0.86rem;
		font-weight: 600;
	}

	.fast-copy small {
		font-size: 0.72rem;
		color: var(--muted-foreground);
		line-height: 1.35;
	}

	.choice-copy {
		min-width: 0;
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.choice-description {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.74rem;
		color: var(--muted-foreground);
	}

	.choice-row {
		width: 100%;
		min-height: 44px;
		display: flex;
		align-items: center;
		gap: 11px;
		padding: 8px 12px;
		border: 0;
		border-bottom: 1px solid var(--border-subtle);
		background: transparent;
		color: var(--foreground);
		text-align: left;
		cursor: pointer;
	}

	.choice-row:last-child {
		border-bottom: 0;
	}

	.choice-row.selected {
		background: color-mix(in oklch, var(--primary) 7%, transparent);
		color: var(--primary);
	}

	.choice-title {
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.86rem;
		font-weight: 600;
		color: var(--foreground);
	}

	/* The exact shared picker used by desktop fills the model subpage. Its own
	   list scrolls while search, provider rail, and context selector stay fixed. */
	.mobile-model-picker {
		flex: 1;
		min-height: 0;
		padding-bottom: env(safe-area-inset-bottom, 0px);
		--mpp-width: 100%;
		--mpp-height: 100%;
	}

	.choice-row:active,
	.header-button:active {
		transform: scale(0.985);
	}

	@keyframes backdrop-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	@keyframes sheet-in {
		from { transform: translateY(100%); }
		to { transform: translateY(0); }
	}

	@media (min-width: 1081px) {
		.sheet-layer {
			display: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.sheet-backdrop,
		.mobile-sheet {
			animation: none;
		}
	}
</style>
