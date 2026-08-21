<script lang="ts">
	import { tick } from 'svelte';
	import {
		ArrowLeft,
		Check,
		ChevronRight,
		Cpu,
		Monitor,
		ShieldCheck,
		SlidersHorizontal,
		Zap
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
		permissionOptions: ControlOption[];
		permissionValue: string | null;
		onPermissionSelect: (value: string) => void;
		computerUseEnabled: boolean;
		computerUseStopped: boolean;
		computerUseActive: boolean;
		computerUseBusy: boolean;
		onToggleComputerUse: () => void;
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
		permissionOptions,
		permissionValue,
		onPermissionSelect,
		computerUseEnabled,
		computerUseStopped,
		computerUseActive,
		computerUseBusy,
		onToggleComputerUse
	}: Props = $props();

	type SheetPage = 'overview' | 'models';
	let page = $state<SheetPage>('overview');
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

	const effortIndex = $derived.by(() => {
		const index = effortOptions.findIndex((option) => option.value === effortValue);
		return index < 0 ? 0 : index;
	});
	const selectedEffort = $derived(effortOptions[effortIndex] ?? effortOptions[0]);
	const effortProgress = $derived(
		effortOptions.length > 1 ? (effortIndex / (effortOptions.length - 1)) * 100 : 0
	);

	function closeSheet() {
		page = 'overview';
		dragging = false;
		dragOffset = 0;
		onClose();
	}

	async function openModels() {
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

	function handleEffortInput(event: Event) {
		const option = effortOptions[Number((event.currentTarget as HTMLInputElement).value)];
		if (option && option.value !== effortValue) onEffortSelect(option.value);
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
	<div use:portal class="sheet-layer" role="presentation">
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
					<section class="control-section">
						<div class="section-label"><SlidersHorizontal size={16} strokeWidth={1.8} /><span>Chat setup</span></div>
						<ContextPillBar sheetMode />
					</section>

					<section class="control-section">
						<div class="section-label"><Cpu size={15} strokeWidth={1.8} /><span>Model</span></div>
						<button type="button" class="disclosure-row" onclick={openModels}>
							<span class="disclosure-copy">
								<strong>{modelLabel}</strong>
								<small>{modelNotice ?? 'Model used for the next turn'}</small>
							</span>
							<ChevronRight size={18} strokeWidth={1.8} />
						</button>
					</section>

					<section class="control-section">
						<div class="section-label"><Zap size={15} strokeWidth={1.8} /><span>Reasoning effort</span></div>
						<div class="effort-slider-card">
							<div class="effort-current">
								<span>
									<strong>{selectedEffort?.label ?? 'Effort'}</strong>
									<small>{selectedEffort?.description ?? 'Reasoning depth'}</small>
								</span>
							</div>
							<input
								type="range"
								min="0"
								max={Math.max(effortOptions.length - 1, 0)}
								step="1"
								value={effortIndex}
								style={`--effort-progress: ${effortProgress}%`}
								aria-label="Reasoning effort"
								aria-valuetext={selectedEffort?.label ?? 'Effort'}
								oninput={handleEffortInput}
							/>
							<div
								class="effort-labels"
								style:grid-template-columns={`repeat(${Math.max(effortOptions.length, 1)}, minmax(0, 1fr))`}
								aria-hidden="true"
							>
								{#each effortOptions as option (option.value)}
									<span class:selected={option.value === effortValue}>{option.label}</span>
								{/each}
							</div>
						</div>
					</section>

					<section class="control-section">
						<div class="section-label"><ShieldCheck size={15} strokeWidth={1.8} /><span>Tool access</span></div>
						<div class="option-card">
							{#each permissionOptions as option (option.value)}
								<button
									type="button"
									class="choice-row"
									class:selected={option.value === permissionValue}
									onclick={() => onPermissionSelect(option.value)}
								>
									<span class="choice-copy">
										<span class="choice-title">
											{option.label}
										</span>
										<span class="choice-description">{option.description}</span>
									</span>
									{#if option.value === permissionValue}<Check size={18} strokeWidth={2.2} />{/if}
								</button>
							{/each}
						</div>
					</section>

					{#if computerUseEnabled}
						<section class="control-section">
							<div class="section-label"><SlidersHorizontal size={15} strokeWidth={1.8} /><span>Tools</span></div>
							<div class="option-card">
								<button
									type="button"
									class="tool-row"
									class:active-tool={!computerUseStopped}
									disabled={computerUseBusy}
									role="switch"
									aria-checked={!computerUseStopped}
									onclick={onToggleComputerUse}
								>
									<span class="tool-icon"><Monitor size={18} strokeWidth={1.8} /></span>
									<span class="tool-copy">
										<strong>Computer control</strong>
										<small>{computerUseActive && !computerUseStopped ? 'Agent is controlling the screen' : computerUseStopped ? 'Paused' : 'Allowed when needed'}</small>
									</span>
									<span class="switch" class:on={!computerUseStopped}><span></span></span>
								</button>
							</div>
						</section>
					{/if}
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
		min-height: 62px;
		padding: 4px 16px 12px;
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
		padding: 16px 14px calc(24px + env(safe-area-inset-bottom, 0px));
	}

	.control-section + .control-section {
		margin-top: 20px;
	}

	.disclosure-row,
	.option-card,
	.effort-slider-card {
		background: color-mix(in oklch, var(--muted) 55%, var(--card));
		border: 1px solid var(--border-subtle);
		border-radius: 16px;
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

	.disclosure-row {
		width: 100%;
		min-height: 64px;
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 14px;
		color: var(--foreground);
		cursor: pointer;
		text-align: left;
	}

	.disclosure-copy,
	.tool-copy,
	.choice-copy {
		min-width: 0;
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.disclosure-copy strong,
	.tool-copy strong {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.9rem;
		font-weight: 600;
	}

	.disclosure-copy small,
	.tool-copy small,
	.choice-description {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.74rem;
		color: var(--muted-foreground);
	}

	.effort-slider-card {
		padding: 12px 14px 10px;
	}

	.effort-current {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: 10px;
	}

	.effort-current > span {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.effort-current strong {
		font-size: 0.9rem;
		font-weight: 650;
		color: var(--foreground);
	}

	.effort-current small {
		font-size: 0.72rem;
		color: var(--muted-foreground);
	}

	.effort-slider-card input[type='range'] {
		width: 100%;
		height: 22px;
		margin: 0;
		appearance: none;
		-webkit-appearance: none;
		background: transparent;
		cursor: pointer;
	}

	.effort-slider-card input[type='range']::-webkit-slider-runnable-track {
		height: 5px;
		border-radius: 999px;
		background: linear-gradient(
			to right,
			var(--primary) 0 var(--effort-progress),
			color-mix(in oklch, var(--muted-foreground) 25%, transparent) var(--effort-progress) 100%
		);
	}

	.effort-slider-card input[type='range']::-moz-range-track {
		height: 5px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--muted-foreground) 25%, transparent);
	}

	.effort-slider-card input[type='range']::-moz-range-progress {
		height: 5px;
		border-radius: 999px;
		background: var(--primary);
	}

	.effort-slider-card input[type='range']::-webkit-slider-thumb {
		width: 20px;
		height: 20px;
		margin-top: -7.5px;
		appearance: none;
		-webkit-appearance: none;
		border: 3px solid var(--primary);
		border-radius: 50%;
		background: var(--card);
		box-shadow: 0 1px 5px color-mix(in oklch, #000 24%, transparent);
	}

	.effort-slider-card input[type='range']::-moz-range-thumb {
		width: 14px;
		height: 14px;
		border: 3px solid var(--primary);
		border-radius: 50%;
		background: var(--card);
		box-shadow: 0 1px 5px color-mix(in oklch, #000 24%, transparent);
	}

	.effort-slider-card input[type='range']:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
		border-radius: 999px;
	}

	.effort-labels {
		display: grid;
		grid-template-columns: repeat(5, minmax(0, 1fr));
		margin-top: 2px;
		color: var(--subtle-foreground);
		font-size: 0.62rem;
		font-weight: 550;
	}

	.effort-labels span {
		min-width: 0;
		text-align: center;
	}

	.effort-labels span:first-child {
		text-align: left;
	}

	.effort-labels span:last-child {
		text-align: right;
	}

	.effort-labels span.selected {
		color: var(--primary);
	}

	.option-card {
		overflow: hidden;
	}

	.choice-row,
	.tool-row {
		width: 100%;
		min-height: 58px;
		display: flex;
		align-items: center;
		gap: 11px;
		padding: 9px 13px;
		border: 0;
		border-bottom: 1px solid var(--border-subtle);
		background: transparent;
		color: var(--foreground);
		text-align: left;
		cursor: pointer;
	}

	.choice-row:last-child,
	.tool-row:last-child {
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

	.tool-icon {
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		border-radius: 11px;
		background: var(--card);
		color: var(--muted-foreground);
	}

	.tool-row.active-tool .tool-icon {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	.tool-row:disabled {
		opacity: 0.45;
		cursor: default;
	}

	.switch {
		width: 42px;
		height: 24px;
		padding: 3px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--muted-foreground) 28%, transparent);
		transition: background 160ms ease;
	}

	.switch span {
		display: block;
		width: 18px;
		height: 18px;
		border-radius: 50%;
		background: white;
		box-shadow: 0 1px 4px #0003;
		transition: transform 180ms cubic-bezier(0.2, 0.8, 0.2, 1);
	}

	.switch.on {
		background: var(--primary);
	}

	.switch.on span {
		transform: translateX(18px);
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
	.tool-row:active,
	.disclosure-row:active,
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

	@media (min-width: 640px) {
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
