<script lang="ts">
	/**
	 * ControlsDock — the composer island's foot row.
	 *
	 * Left: ONE summary chip ("Model · Effort · Access") that opens the desktop
	 * controls popover — chat-setup pills (agent / execution mode / branch),
	 * the model picker, the effort slider with the Fast tier, and tool access —
	 * followed by the context-usage ring with its own detail popover. Right:
	 * the attach button, then whatever the parent passes in the `actions`
	 * snippet — ChatInput uses it for the dictate / voice-call / send cluster
	 * so the whole right-hand group shares one flex row.
	 *
	 * Mobile keeps its primary actions inline and moves the configuration
	 * matrix into MobileComposerControlsSheet (the sliders trigger below).
	 *
	 * Computer use is agent configuration, not a composer control: both the
	 * capability switch and the emergency stop live in the agent editor's
	 * Computer use section, so the dock neither shows a toggle nor polls status.
	 */
	import type { Snippet } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { anyCredential } from '$lib/stores/auth.svelte';
	import { profiles } from '$lib/stores/app-data.svelte';
	import ContextUsageBar from './ContextUsageBar.svelte';
	import EffortSliderCard from './EffortSliderCard.svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';
	import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
	import ContextPillBar from './ContextPillBar.svelte';
	import { SlidersHorizontal } from 'lucide-svelte';
	import {
		getModelLabel,
		getDefaultModel,
		isFastModeCapable,
		isReroute
	} from '$lib/constants/models';
	import { getUsageLimits } from '$lib/api/client';
	import {
		rateLimit,
		resolveBucketKey,
		isBucketStale,
		BUCKET_STALE_MS
	} from '$lib/stores/rate-limit';
	import { usageTargetInstance } from '$lib/stores/usage-target';
	import { attachmentBlockReason, isRemoteOnlyCompose } from './attach-disable';

	interface Props {
		isUploading: boolean;
		onAttachClick: () => void;
		/** Trailing buttons for the right cluster (dictate / voice call / send). */
		actions?: Snippet;
		/** Lets ChatInput keep its compact mobile surface expanded while the sheet is open. */
		onSheetOpenChange?: (open: boolean) => void;
	}

	let { isUploading, onAttachClick, actions, onSheetOpenChange }: Props = $props();

	// Desktop: one popover hosts every run-binding control. Opening it closes the
	// context-usage popover implicitly — they are separate components, but only
	// one summary surface should be open at a time.
	let controlsPopoverOpen = $state(false);
	let summaryWrapRef = $state<HTMLDivElement | null>(null);
	function setControlsPopoverOpen(open: boolean) {
		controlsPopoverOpen = open;
	}
	function toggleControlsPopover() {
		controlsPopoverOpen = !controlsPopoverOpen;
	}

	function handlePopoverOutsideClick(e: MouseEvent) {
		if (!controlsPopoverOpen || !summaryWrapRef) return;
		if (!(e.target instanceof Node)) return;
		if (summaryWrapRef.contains(e.target)) return;
		controlsPopoverOpen = false;
	}

	function handlePopoverWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && controlsPopoverOpen) {
			e.stopPropagation();
			setControlsPopoverOpen(false);
		}
	}

	$effect(() => {
		document.addEventListener('click', handlePopoverOutsideClick);
		window.addEventListener('keydown', handlePopoverWindowKeydown);
		return () => {
			document.removeEventListener('click', handlePopoverOutsideClick);
			window.removeEventListener('keydown', handlePopoverWindowKeydown);
		};
	});

	let mobileSheetOpen = $state(false);
	function setMobileSheetOpen(open: boolean) {
		mobileSheetOpen = open;
		onSheetOpenChange?.(open);
	}

	// Model / effort / permission are the SESSION's run binding, not an override
	// of the agent's: an agent is the configuration (prompt, tools, memory) and
	// carries no model. The user picks the binding when the chat is created —
	// seeded from whatever they picked last — and may change it mid-session;
	// every send carries it, and the backend stores it on the session row.
	const activeProfile = $derived($profiles.find((p) => p.id === $activeSession.profile));
	const effortValue = $derived($activeSession.effortLevelOverride);
	const permissionValue = $derived($activeSession.permissionModeOverride ?? 'default');
	const attachDisabled = $derived(
		!$anyCredential || isUploading || isRemoteOnlyCompose($activeSession.project, $activeSession.projectV2Id)
	);

	// ── Model override ──────────────────────────────────────────────
	// Terse, family-level blurbs — the dropdown surfaces one line of "why pick
	// this" without turning into a spec sheet.
	// Live model-switch options — the FULL provider-grouped library (PA13), built
	// by the shared catalog helper so the dock, the mobile sheet and the
	// profile/agent editor can never disagree about naming or grouping. Provider
	// entries are encoded as `prov:<providerId>:<modelId>` so the backend can
	// rebind the turn's provider from the pick alone — cross-provider switching
	// is a first-class mid-session action (the backend evicts and reconnects
	// when the resolved provider changes).
	const modelCatalog = $derived(buildModelPickerCatalog({ familyBlurbs: true }));
	const modelOptions = $derived(modelCatalog.options);
	// The session's model. A chat always has one: the pick is seeded from the
	// last-used model on a new chat and restored from the session's own row
	// when an existing one is opened, so the registry default is only ever the
	// first-run floor.
	const modelValue = $derived($activeSession.modelOverride ?? getDefaultModel());

	// ── Fast service tier ───────────────────────────────────────────
	// Offered only on models that carry the tier (Claude Opus, OpenAI GPT-5 on
	// an OpenAI/Codex provider row). It rides the effort dropdown because both
	// are knobs on how hard — and how fast — the turn runs.
	const fastMode = $derived($activeSession.fastMode);
	const fastSupported = $derived(isFastModeCapable(modelValue));

	// Switching to a model without the tier must not leave a stale flag on the
	// session — the toggle would be invisible while every send still asked for it.
	$effect(() => {
		if (fastMode && !fastSupported) activeSession.setFastMode(false);
	});

	// Live reroute detection: the backend `model_info` note tells us which model
	// the SDK's MAIN lane is ACTUALLY running. Compare at FAMILY level — exact-ID
	// comparison false-flags when the SDK echoes a dated snapshot ID missing from
	// the legacy map (inferModelFamily still classifies those via substring
	// inference). Unknown/custom models never flag (deliberate: no reroute alarm
	// we can't substantiate); same-family version reroutes are intentionally not
	// flagged.
	const liveModel = $derived($activeSession.liveModel);
	const liveRequestedModel = $derived($activeSession.liveRequestedModel);
	// Compare actual vs what was REQUESTED for that same turn (both from the
	// model_info note) — never the live selection, which the user may have
	// changed since. A provider session (model_info carried a non-null
	// `provider`) is NEVER rerouted: its gateway model can't map to a Claude
	// family, so the family comparison is meaningless and would false-positive
	// (R1). `isReroute` short-circuits to false on a non-null provider.
	const rerouted = $derived(
		isReroute(liveModel, liveRequestedModel, $activeSession.liveProvider)
	);
	const selectedModelLabel = $derived(getModelLabel(modelValue));
	const actualModelLabel = $derived(getModelLabel(liveModel));
	const requestedModelLabel = $derived(getModelLabel(liveRequestedModel));
	// When rerouted the pill shows the ACTUAL model (what's really running);
	// otherwise it shows the selection.
	const modelButtonLabel = $derived(rerouted ? actualModelLabel : selectedModelLabel);
	const modelWarnTitle = $derived(
		rerouted
			? `Model rerouted: running ${actualModelLabel} (requested ${requestedModelLabel})`
			: undefined
	);
	const modelNotice = $derived(
		rerouted ? `Running ${actualModelLabel} instead of ${requestedModelLabel}` : undefined
	);
	// Provider sub-label on the model pill (mockup: "Fable 5 · Claude"). It is the
	// brand group the SELECTED model belongs to, reused verbatim from the dropdown
	// options so the pill and the menu can't name the same account differently.
	// Suppressed while rerouted — the pill then shows the ACTUAL model, whose
	// group we haven't resolved, and a stale brand would be worse than none.
	const modelSubLabel = $derived(
		rerouted ? null : modelOptions.find((o) => o.value === modelValue)?.group ?? null
	);

	// Single owner of usage-limit fetching. A shared in-flight promise dedupes
	// concurrent triggers (e.g. mount + a rapid target change) into ONE GET.
	let usageFetchInFlight: Promise<void> | null = null;
	function refreshUsageLimits(force = false): Promise<void> {
		if (usageFetchInFlight) return usageFetchInFlight;
		usageFetchInFlight = getUsageLimits(force)
			.then((snap) => rateLimit.applySnapshot(snap))
			.catch(() => {
				// Non-fatal — reset so a later target change / WS event retries.
				handledTarget = undefined;
			})
			.finally(() => {
				usageFetchInFlight = null;
			});
		return usageFetchInFlight;
	}

	// Populate the pill on load AND keep it right when the viewed/composed session
	// moves to a DIFFERENT install (e.g. opening an offloaded session), whose usage
	// bucket may be missing or stale — the snapshot broadcast can lag switching by
	// up to the poll interval. The FIRST run always fetches (initial populate,
	// before the first `usage_snapshot` WS event); later target changes only
	// refetch when that install's bucket is missing/unavailable/stale, so a plain
	// session switch on a fresh bucket does not refetch. Reading `rateLimit.current`
	// (a plain getter, not $state) deliberately does NOT make this effect depend on
	// the store, so it fires only on target changes. `undefined` = not yet handled
	// (distinct from a real `null`/self target).
	let handledTarget: string | null | undefined = undefined;
	$effect(() => {
		const target = $usageTargetInstance;
		if (target === handledTarget) return;
		const firstRun = handledTarget === undefined;
		handledTarget = target;
		const state = rateLimit.current;
		const key = resolveBucketKey(state, target);
		const bucket = key ? state.buckets[key] : null;
		const now = Date.now();
		const isStale =
			(key != null && (state.staleByInstance[key] ?? false)) ||
			(bucket != null && isBucketStale(bucket, now));
		// Provider buckets are provider-scoped, not install-scoped, so the Claude
		// staleness check above never covers them: without this, a provider's
		// credits/windows could sit stale for the whole session (the poller's
		// snapshot is not broadcast over WS — only the REST GET refreshes them).
		const providerStale = Object.values(state.providerBuckets ?? {}).some(
			(pb) => pb.fetchedAt != null && now - pb.fetchedAt > BUCKET_STALE_MS
		);
		if (firstRun || !bucket || !bucket.available || isStale || providerStale) {
			refreshUsageLimits();
		}
	});

	const permissionModes = [
		{ value: 'default', label: 'Ask', description: 'Confirm before tools' },
		{ value: 'auto', label: 'Auto', description: 'Model classifier decides per tool call' },
		{ value: 'acceptEdits', label: 'Accept Edits', description: 'Auto-approve edits' },
		{ value: 'plan', label: 'Plan Only', description: 'Plan without executing' },
		{ value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' }
	];

	const effortLevels = [
		{ value: 'low', label: 'Low', description: 'Quick, minimal reasoning' },
		{ value: 'medium', label: 'Medium', description: 'Balanced reasoning' },
		{ value: 'high', label: 'High', description: 'Thorough reasoning' },
		{ value: 'xhigh', label: 'XHigh', description: 'Extreme reasoning' },
		{ value: 'max', label: 'Max', description: 'Maximum reasoning depth' }
	];

	// The summary chip reads "Model · Effort · Access". Effort shows its
	// overridden level when one is set; access always names the effective mode
	// (it defaults to 'default' → Ask, never blank).
	const chipEffortLabel = $derived(
		effortValue ? (effortLevels.find((m) => m.value === effortValue)?.label ?? 'Effort') : 'Auto'
	);
	const chipPermissionLabel = $derived(
		permissionModes.find((m) => m.value === permissionValue)?.label ?? 'Ask'
	);
	function handleChipKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && controlsPopoverOpen) {
			e.stopPropagation();
			setControlsPopoverOpen(false);
		}
	}
</script>

<div class="controls-dock">
	<!-- Left: ONE summary chip, then the context ring -->
	<div class="controls-left desktop-controls">
		<div class="controls-summary-wrap" bind:this={summaryWrapRef}>
			<button
				type="button"
				class="controls-summary-chip"
				class:open={controlsPopoverOpen}
				class:warn={rerouted}
				onclick={toggleControlsPopover}
				onkeydown={handleChipKeydown}
				title={rerouted && modelWarnTitle
					? modelWarnTitle
					: `Model ${modelButtonLabel} · effort ${chipEffortLabel} · access ${chipPermissionLabel}`}
				aria-haspopup="dialog"
				aria-expanded={controlsPopoverOpen}
				aria-label={`Chat controls — model ${modelButtonLabel}, effort ${chipEffortLabel}, access ${chipPermissionLabel}`}
			>
				<span class="chip-label">{modelButtonLabel}</span>
				<span class="chip-sep">·</span>
				<span class="chip-label muted">{chipEffortLabel}</span>
				<span class="chip-sep">·</span>
				<span class="chip-label muted">{chipPermissionLabel}</span>
			</button>

			{#if controlsPopoverOpen}
				<!-- The desktop counterpart of the mobile sheet: every run-binding
				     control in one panel. Chat-setup pills first (agent / execution
				     mode / branch), then model, effort + Fast, and tool access. -->
				<div
					class="controls-popover"
					role="dialog"
					aria-label="Chat controls"
					data-composer-owned-overlay="true"
				>
					<section class="popover-section">
						<div class="section-label"><SlidersHorizontal size={14} strokeWidth={1.8} /><span>Chat setup</span></div>
						<div class="setup-pills"><ContextPillBar /></div>
					</section>

					<section class="popover-section">
						<div class="section-label"><span>Model</span></div>
						<ModelPickerPanel
							groups={modelCatalog.groups}
							options={modelCatalog.options}
							value={modelValue}
							notice={modelNotice}
							contextChoice={$activeSession.contextWindowChoice}
							onContextChoiceSelect={(choice) => activeSession.setContextWindowChoice(choice)}
							onSelect={(v) => {
								activeSession.setModelOverride(v);
								setControlsPopoverOpen(false);
							}}
							onRequestClose={() => setControlsPopoverOpen(false)}
						/>
					</section>

					<section class="popover-section">
						<div class="section-label"><span>Reasoning effort</span></div>
						<EffortSliderCard
							options={effortLevels}
							value={effortValue}
							onSelect={(v) => activeSession.setEffortLevelOverride(v)}
							{fastSupported}
							fastEnabled={fastMode}
							onToggleFast={() => activeSession.setFastMode(!fastMode)}
						/>
					</section>

					<section class="popover-section">
						<div class="section-label"><span>Tool access</span></div>
						<div class="access-list">
							{#each permissionModes as mode (mode.value)}
								<button
									type="button"
									class="access-row"
									class:selected={mode.value === permissionValue}
									onclick={() => activeSession.setPermissionModeOverride(mode.value)}
									role="menuitemradio"
									aria-checked={mode.value === permissionValue}
									title={mode.description}
								>
									<span class="access-copy">
										<strong>{mode.label}</strong>
										<small>{mode.description}</small>
									</span>
									{#if mode.value === permissionValue}
										<svg class="w-3.5 h-3.5 access-check" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
										</svg>
									{/if}
								</button>
							{/each}
						</div>
					</section>
				</div>
			{/if}
		</div>

		<!-- Context-usage ring (16px SVG arc + its popover) -->
		<ContextUsageBar onOpenChange={(o) => { if (o) void refreshUsageLimits(true); }} />
	</div>

	<!-- Mobile keeps the primary actions visible and moves the configuration
	     matrix into one native-style bottom sheet, matching T3's composer. -->
	<div class="mobile-controls">
		<button
			type="button"
			class="mobile-settings-trigger"
			class:warn={rerouted}
			onclick={() => setMobileSheetOpen(true)}
			aria-haspopup="dialog"
			aria-expanded={mobileSheetOpen}
			aria-label={`Open chat controls. Current model: ${modelButtonLabel}`}
		>
			<SlidersHorizontal size={16} strokeWidth={1.8} aria-hidden="true" />
		</button>
		<ContextUsageBar onOpenChange={(o) => { if (o) void refreshUsageLimits(true); }} />
	</div>

	<!-- Right: attach, then the parent's action cluster -->
	<div class="controls-right">
		<!-- Attach stays in the primary action cluster on every viewport. -->
		<button
			type="button"
			onclick={onAttachClick}
			disabled={attachDisabled}
			class="dock-action-btn"
			aria-label="Attach file"
			title={isUploading
				? 'Uploading...'
				: (attachmentBlockReason($activeSession.project, $activeSession.projectV2Id) ?? 'Attach file')}
		>
			{#if isUploading}
				<svg class="w-[18px] h-[18px] animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
				</svg>
			{:else}
				<svg class="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13" />
				</svg>
			{/if}
		</button>

		<!-- Dictate / voice call / send, supplied by ChatInput -->
		{@render actions?.()}
	</div>
</div>

<MobileComposerControlsSheet
	open={mobileSheetOpen}
	onClose={() => setMobileSheetOpen(false)}
	{modelOptions}
	modelGroups={modelCatalog.groups}
	{modelValue}
	modelLabel={modelButtonLabel}
	modelNotice={modelNotice}
	onModelSelect={(value) => activeSession.setModelOverride(value)}
	contextChoice={$activeSession.contextWindowChoice}
	onContextChoiceSelect={(choice) => activeSession.setContextWindowChoice(choice)}
	effortOptions={effortLevels}
	{effortValue}
	onEffortSelect={(value) => activeSession.setEffortLevelOverride(value)}
	{fastSupported}
	fastEnabled={fastMode}
	onToggleFast={() => activeSession.setFastMode(!fastMode)}
	permissionOptions={permissionModes}
	permissionValue={permissionValue}
	onPermissionSelect={(value) => activeSession.setPermissionModeOverride(value)}
/>

<style>
	/* ===== FOOT ROW (mockup `.footrow`) =====
	   No surface of its own — the island above provides the glass. */
	.controls-dock {
		/* The override dropdowns and the context-usage popover inside this row open
		   UPWARD, so they overlap the input area directly above them. That area sets
		   `z-index: 20` inside the island's stacking context (established by its
		   backdrop-filter), so without an explicit z-index here the whole row — and
		   its z-50 popups — would stack below the message box and render behind it.
		   Lifting the row above the input area keeps them fully visible on top. */
		position: relative;
		z-index: 30;
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 8px 8px 10px;
		overflow: visible;
	}

	.controls-left {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
	}

	/* ===== SUMMARY CHIP + CONTROLS POPOVER =====
	   The desktop counterpart of the mobile sheet's overview page: one pill
	   summarizing the session's run binding (model · effort · access), opening
	   a single panel that hosts every binding control. */
	.controls-summary-wrap {
		position: relative;
		min-width: 0;
	}

	.controls-summary-chip {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 26px;
		max-width: 100%;
		min-width: 0;
		padding: 0 10px;
		background: var(--scrim);
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--foreground);
		font-size: 12px;
		cursor: pointer;
		white-space: nowrap;
		transition: border-color 0.13s ease, color 0.13s ease, background-color 0.13s ease;
	}

	.controls-summary-chip:hover,
	.controls-summary-chip.open {
		border-color: var(--border-strong);
		background: color-mix(in oklch, var(--scrim) 70%, var(--popover));
	}

	.controls-summary-chip:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* Reroute: amber chip — the same treatment the old model pill carried, so the
	   "running something else" signal keeps its established look. */
	.controls-summary-chip.warn,
	.controls-summary-chip.warn:hover {
		background: color-mix(in oklch, var(--warning) 10%, transparent);
		border-color: color-mix(in oklch, var(--warning) 50%, var(--border));
		color: var(--warning);
	}

	.chip-label {
		overflow: hidden;
		text-overflow: ellipsis;
		line-height: 1;
		font-weight: 600;
		letter-spacing: 0.01em;
	}

	.chip-label.muted {
		color: var(--muted-foreground);
		font-weight: 400;
	}

	.controls-summary-chip.warn .chip-label.muted {
		color: color-mix(in oklch, var(--warning) 75%, transparent);
	}

	.chip-sep {
		color: var(--muted-foreground);
		opacity: 0.45;
		flex-shrink: 0;
	}

	/* Opens UPWARD from the foot row — same anchor convention as the override
	   dropdowns it replaces and the context-usage popover beside it. */
	.controls-popover {
		position: absolute;
		bottom: calc(100% + 8px);
		left: 0;
		width: min(380px, calc(100vw - 24px));
		max-height: min(640px, calc(100dvh - 140px));
		overflow-y: auto;
		overscroll-behavior: contain;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 1.3);
		box-shadow: var(--shadow-l);
		padding: 12px;
		z-index: 60;
		animation: controls-popover-in 0.16s cubic-bezier(0.16, 1, 0.3, 1);
		display: flex;
		flex-direction: column;
		gap: 14px;
	}

	@keyframes controls-popover-in {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.popover-section {
		display: flex;
		flex-direction: column;
		gap: 7px;
		min-width: 0;
	}

	.popover-section .section-label {
		display: flex;
		align-items: center;
		gap: 7px;
		margin: 0 2px;
		color: var(--muted-foreground);
		font-size: 10px;
		font-weight: 700;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	.popover-section .section-label :global(svg) {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
	}

	/* Chat-setup pills reuse ContextPillBar verbatim; the wrapper just keeps its
	   upward dropdowns inside the popover's scroll bounds visually. */
	.setup-pills :global(.context-pill-bar) {
		padding: 0;
		z-index: auto;
	}

	.setup-pills :global(.selector-dropdown),
	.setup-pills :global(.branch-dropdown),
	.setup-pills :global(.runner-dropdown) {
		position: static;
		width: 100%;
		min-width: 0;
		max-width: none;
		margin-top: 6px;
		box-shadow: none;
	}

	.access-list {
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--muted) 40%, var(--card));
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.9);
		overflow: hidden;
	}

	.access-row {
		display: flex;
		align-items: center;
		gap: 10px;
		width: 100%;
		padding: 8px 12px;
		border: 0;
		border-bottom: 1px solid var(--border-subtle);
		background: transparent;
		color: var(--foreground);
		text-align: left;
		cursor: pointer;
		transition: background-color 0.13s ease;
	}

	.access-row:last-child {
		border-bottom: 0;
	}

	.access-row:hover {
		background: var(--hover-overlay);
	}

	.access-row.selected {
		background: color-mix(in oklch, var(--primary) 7%, transparent);
	}

	.access-row.selected strong {
		color: var(--primary);
	}

	.access-copy {
		min-width: 0;
		flex: 1;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}

	.access-copy strong {
		font-size: 13px;
		font-weight: 600;
	}

	.access-copy small {
		font-size: 11px;
		color: var(--muted-foreground);
	}

	.access-check {
		flex-shrink: 0;
		color: var(--primary);
	}

	/* `margin-left: auto` (not a second flex:1 half) so the pills on the left keep
	   their natural width and the action cluster hugs the right edge. */
	.controls-right {
		display: flex;
		align-items: center;
		gap: 2px;
		margin-left: auto;
	}

	.mobile-controls {
		display: none;
		align-items: center;
		gap: 3px;
		min-width: 0;
	}

	.mobile-settings-trigger {
		display: flex;
		position: relative;
		width: 36px;
		height: 34px;
		flex: 0 0 36px;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 0;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--scrim);
		color: var(--muted-foreground);
		cursor: pointer;
	}

	.mobile-settings-trigger.warn {
		color: var(--warning, var(--primary));
		border-color: color-mix(in oklch, var(--warning, var(--primary)) 36%, var(--border));
	}

	.mobile-settings-trigger:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* ===== ICON BUTTONS (mockup `.fbtn`) ===== */
	.dock-action-btn {
		position: relative;
		display: flex;
		align-items: center;
		justify-content: center;
		width: 30px;
		height: 30px;
		background: transparent;
		border: none;
		border-radius: 9px;
		cursor: pointer;
		color: var(--subtle-foreground);
		transition: background-color 0.13s ease, color 0.13s ease;
	}

	.dock-action-btn:hover:not(:disabled) {
		background: var(--scrim);
		color: var(--foreground);
	}

	.dock-action-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.dock-action-btn:disabled {
		opacity: 0.3;
		cursor: not-allowed;
	}

	@media (max-width: 1080px) {
		.controls-dock {
			gap: 3px;
			padding: 5px 6px 7px;
		}

		.desktop-controls {
			display: none;
		}

		.mobile-controls {
			display: flex;
		}

		.controls-right {
			gap: 0;
		}

		.controls-right :global(.voice-btn),
		.controls-right :global(.voice-call-btn),
		.controls-right :global(.stop-btn) {
			width: 36px;
			height: 36px;
		}

		.dock-action-btn {
			width: 36px;
			height: 36px;
			border-radius: 11px;
		}
	}
</style>
