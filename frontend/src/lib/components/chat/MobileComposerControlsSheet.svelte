<script lang="ts">
	import { tick } from 'svelte';
	import {
		ArrowLeft,
		Check,
		ChevronRight,
		Cpu,
		Monitor,
		Search,
		ShieldCheck,
		SlidersHorizontal,
		Star,
		Zap
	} from 'lucide-svelte';
	import ProviderGlyph from '$lib/components/settings/ProviderGlyph.svelte';
	import ContextPillBar from './ContextPillBar.svelte';
	import type { PickerGroup } from '$lib/constants/model-picker';
	import {
		getFavoriteModels,
		isFavoriteModel,
		toggleFavoriteModel
	} from '$lib/stores/modelFavorites.svelte';

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
		modelDefaultValue: string;
		modelOverridden: boolean;
		modelNotice?: string;
		onModelSelect: (value: string) => void;
		effortOptions: ControlOption[];
		effortValue: string | null;
		effortDefaultValue: string | null;
		effortOverridden: boolean;
		onEffortSelect: (value: string) => void;
		permissionOptions: ControlOption[];
		permissionValue: string | null;
		permissionDefaultValue: string | null;
		permissionOverridden: boolean;
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
		modelDefaultValue,
		modelOverridden,
		modelNotice,
		onModelSelect,
		effortOptions,
		effortValue,
		effortDefaultValue,
		effortOverridden,
		onEffortSelect,
		permissionOptions,
		permissionValue,
		permissionDefaultValue,
		permissionOverridden,
		onPermissionSelect,
		computerUseEnabled,
		computerUseStopped,
		computerUseActive,
		computerUseBusy,
		onToggleComputerUse
	}: Props = $props();

	type SheetPage = 'overview' | 'models';
	let page = $state<SheetPage>('overview');
	let searchQuery = $state('');
	let sheetRef = $state<HTMLElement | null>(null);
	let dragging = $state(false);
	let dragOffset = $state(0);
	let dragStartY = 0;
	let dragStartedAt = 0;

	const FAVORITES = '__favorites__';

	const isSearching = $derived(searchQuery.trim().length > 0);

	// Rail entries: one per provider group (from `modelGroups` when the parent
	// supplies them, else derived from the options), with brand glyphs.
	const railGroups = $derived.by<PickerGroup[]>(() => {
		if (modelGroups && modelGroups.length > 0) return modelGroups;
		const seen = new Map<string, PickerGroup>();
		for (const o of modelOptions) {
			if (!seen.has(o.groupKey)) {
				seen.set(o.groupKey, {
					key: o.groupKey,
					name: o.group,
					subtext: o.groupSubtext,
					glyph: 'custom'
				});
			}
		}
		return [...seen.values()];
	});
	function groupFor(option: ModelOption): PickerGroup | undefined {
		return railGroups.find((g) => g.key === option.groupKey);
	}

	const favoriteOptions = $derived(
		getFavoriteModels()
			.map((v) => modelOptions.find((o) => o.value === v))
			.filter((o): o is ModelOption => o != null)
	);
	const hasFavorites = $derived(favoriteOptions.length > 0);

	// Rail selection — favorites when any exist, else the current model's
	// provider, else the first group. Re-validated against the live catalog.
	let railKey = $state<string | null>(null);
	const currentGroupKey = $derived(
		modelOptions.find((o) => o.value === modelValue)?.groupKey ?? null
	);
	const effectiveRailKey = $derived.by(() => {
		if (railKey === FAVORITES && hasFavorites) return FAVORITES;
		if (railKey && railGroups.some((g) => g.key === railKey)) return railKey;
		if (hasFavorites) return FAVORITES;
		if (currentGroupKey) return currentGroupKey;
		return railGroups[0]?.key ?? null;
	});
	const showRail = $derived(!isSearching && (railGroups.length > 1 || hasFavorites));

	const visibleModels = $derived.by<ModelOption[]>(() => {
		if (isSearching) {
			const query = searchQuery.trim().toLowerCase();
			return modelOptions.filter((option) =>
				[option.label, option.description, option.group, option.groupSubtext ?? '']
					.join(' ')
					.toLowerCase()
					.includes(query)
			);
		}
		if (effectiveRailKey === FAVORITES) return favoriteOptions;
		return modelOptions.filter((o) => o.groupKey === effectiveRailKey);
	});
	const showProviderLine = $derived(
		isSearching || effectiveRailKey === FAVORITES || !showRail
	);
	const effortIndex = $derived.by(() => {
		const index = effortOptions.findIndex((option) => option.value === effortValue);
		return index < 0 ? 0 : index;
	});
	const selectedEffort = $derived(effortOptions[effortIndex] ?? effortOptions[0]);
	const effortProgress = $derived(
		effortOptions.length > 1 ? (effortIndex / (effortOptions.length - 1)) * 100 : 0
	);

	function toggleFavorite(e: MouseEvent, value: string) {
		e.stopPropagation();
		toggleFavoriteModel(value);
	}

	function closeSheet() {
		page = 'overview';
		searchQuery = '';
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
				searchQuery = '';
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
						onclick={() => {
							page = 'overview';
							searchQuery = '';
						}}
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
				<div class="search-wrap">
					<Search size={17} strokeWidth={1.8} aria-hidden="true" />
					<input
						bind:value={searchQuery}
						type="search"
						placeholder="Search models"
						aria-label="Search models"
					/>
				</div>

				{#if showRail}
					<div class="provider-rail" role="tablist" aria-label="Providers">
						{#if hasFavorites}
							<button
								type="button"
								role="tab"
								class="rail-chip"
								class:selected={effectiveRailKey === FAVORITES}
								aria-selected={effectiveRailKey === FAVORITES}
								onclick={() => (railKey = FAVORITES)}
							>
								<Star size={14} strokeWidth={2} class="rail-star" />
								<span>Favorites</span>
							</button>
						{/if}
						{#each railGroups as g (g.key)}
							<button
								type="button"
								role="tab"
								class="rail-chip"
								class:selected={effectiveRailKey === g.key}
								aria-selected={effectiveRailKey === g.key}
								title={g.subtext ? `${g.name} · ${g.subtext}` : g.name}
								onclick={() => (railKey = g.key)}
							>
								<ProviderGlyph kind={g.glyph} size={14} />
								<span>{g.name}</span>
								{#if g.subtext}<small>{g.subtext}</small>{/if}
							</button>
						{/each}
					</div>
				{/if}

				<div class="sheet-scroll model-scroll">
					{#if visibleModels.length === 0}
						<div class="empty-results">
							{#if isSearching}
								<Cpu size={22} strokeWidth={1.6} />
								<p>No models match “{searchQuery}”.</p>
							{:else if effectiveRailKey === FAVORITES}
								<Star size={22} strokeWidth={1.6} />
								<p>No favorites yet — star a model to pin it here.</p>
							{:else}
								<Cpu size={22} strokeWidth={1.6} />
								<p>No models available.</p>
							{/if}
						</div>
					{:else}
						<div class="option-card">
							{#each visibleModels as option (option.value)}
								{@const g = groupFor(option)}
								{@const fav = isFavoriteModel(option.value)}
								<div
									class="choice-row model-row"
									class:selected={option.value === modelValue}
									role="option"
									aria-selected={option.value === modelValue}
									tabindex="0"
									onclick={() => onModelSelect(option.value)}
									onkeydown={(e) => {
										if (e.key === 'Enter' || e.key === ' ') {
											e.preventDefault();
											onModelSelect(option.value);
										}
									}}
								>
									<span class="choice-copy">
										<span class="choice-title">
											{option.label}
											{#if option.value === modelDefaultValue}<em>Profile</em>{/if}
										</span>
										{#if showProviderLine && g}
											<span class="choice-description model-provider">
												<ProviderGlyph kind={g.glyph} size={12} />
												<span>{g.name}{#if g.subtext} · {g.subtext}{/if}</span>
											</span>
										{:else}
											<span class="choice-description">{option.description}</span>
										{/if}
									</span>
									<button
										type="button"
										class="fav-btn"
										class:on={fav}
										aria-label={fav ? `Remove ${option.label} from favorites` : `Add ${option.label} to favorites`}
										onclick={(e) => toggleFavorite(e, option.value)}
									>
										<Star size={16} strokeWidth={2} />
									</button>
									{#if option.value === modelValue}<Check size={18} strokeWidth={2.2} />{/if}
								</div>
							{/each}
						</div>
					{/if}
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
							{#if modelOverridden}<span class="override-badge">Override</span>{/if}
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
								{#if selectedEffort?.value === effortDefaultValue}<em>Profile</em>{/if}
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
						{#if effortOverridden}<p class="section-note">Overridden for this chat</p>{/if}
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
											{#if option.value === permissionDefaultValue}<em>Profile</em>{/if}
										</span>
										<span class="choice-description">{option.description}</span>
									</span>
									{#if option.value === permissionValue}<Check size={18} strokeWidth={2.2} />{/if}
								</button>
							{/each}
						</div>
						{#if permissionOverridden}<p class="section-note">Overridden for this chat</p>{/if}
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

	.override-badge,
	.choice-title em {
		padding: 3px 6px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
		font-size: 0.62rem;
		font-style: normal;
		font-weight: 650;
		white-space: nowrap;
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

	.effort-current em {
		padding: 3px 7px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
		font-size: 0.62rem;
		font-style: normal;
		font-weight: 650;
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

	.section-note {
		margin: 6px 5px 0;
		font-size: 0.68rem;
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

	.search-wrap {
		margin: 10px 14px 6px;
		height: 42px;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 12px;
		border: 1px solid var(--border-subtle);
		border-radius: 13px;
		background: var(--muted);
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.search-wrap input {
		min-width: 0;
		flex: 1;
		border: 0;
		outline: 0;
		background: transparent;
		color: var(--foreground);
		font-size: 0.88rem;
	}

	.model-scroll {
		padding: 6px 14px calc(18px + env(safe-area-inset-bottom, 0px));
	}

	/* Horizontal provider rail (the T3 icon rail, laid sideways for a phone). */
	.provider-rail {
		display: flex;
		gap: 6px;
		padding: 4px 14px 8px;
		overflow-x: auto;
		scrollbar-width: none;
		flex-shrink: 0;
	}
	.provider-rail::-webkit-scrollbar {
		display: none;
	}
	.rail-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 32px;
		padding: 0 11px;
		border: 1px solid var(--border-subtle);
		border-radius: 999px;
		background: var(--muted);
		color: var(--muted-foreground);
		font-size: 0.8rem;
		font-weight: 550;
		white-space: nowrap;
		cursor: pointer;
		flex-shrink: 0;
	}
	.rail-chip small {
		font-size: 0.7rem;
		font-weight: 450;
		opacity: 0.7;
	}
	.rail-chip.selected {
		background: var(--selection-bg);
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
		color: var(--foreground);
	}
	.rail-chip :global(.rail-star) {
		fill: currentColor;
	}

	.model-row {
		cursor: pointer;
	}
	.model-provider {
		display: inline-flex;
		align-items: center;
		gap: 5px;
	}
	.fav-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 32px;
		height: 32px;
		border: 0;
		border-radius: 9px;
		background: transparent;
		color: var(--muted-foreground);
		opacity: 0.6;
		cursor: pointer;
		flex-shrink: 0;
	}
	.fav-btn.on {
		opacity: 1;
		color: #eab308;
	}
	.fav-btn.on :global(svg) {
		fill: currentColor;
	}

	.empty-results {
		min-height: 180px;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 10px;
		color: var(--muted-foreground);
		font-size: 0.85rem;
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
