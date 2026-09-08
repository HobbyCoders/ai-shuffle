<script lang="ts">
	/**
	 * ControlsDock — the bare-text controls row at the foot of the composer box
	 * (mockup: docs/design/composer-split-chips.mockup.html).
	 *
	 * Desktop, left to right:
	 *   permission mode as plain text (opens the tool-access menu) · the dictate
	 *   mic and voice-call buttons ChatInput passes in as the `voiceActions`
	 *   snippet · "+" attach.
	 * Right, left to right:
	 *   model name as plain text (opens the model picker) · effort as a filled
	 *   tag (opens the compact effort popover) · the context-usage ring.
	 *
	 * The permission control is CODE-LANE ONLY, on both viewports. Chat and voice
	 * sessions run a fixed tool surface the user cannot widen, so a tool-access
	 * menu there would offer a choice with nothing behind it. Attach is dropped
	 * only in the voice lane, which has no composer upload path. Everything else
	 * — dictate/call, model, effort, the usage ring and the mobile settings
	 * trigger — is present in every lane.
	 *
	 * Only one dock popover is open at a time; the ring's own popover closes
	 * itself on any outside click, so the two never stack.
	 *
	 * Mobile puts the settings trigger first on the left (same icon button as
	 * mic / call / attach), then those actions, and the usage ring on the right.
	 * The configuration matrix lives in MobileComposerControlsSheet. The send
	 * button is not here on any viewport: it sits at the top-right of the box,
	 * inside ChatInput's input area.
	 *
	 * Computer use is a GLOBAL toggle (Settings → Chat & Input), not a composer
	 * control: the dock neither shows a toggle nor polls status. Halting it for a
	 * session is the Stop button's job.
	 */
	import { onDestroy, tick, type Snippet } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { anyCredential } from '$lib/stores/auth.svelte';
	import ContextUsageBar from './ContextUsageBar.svelte';
	import EffortPopoverCard from './EffortPopoverCard.svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';
	import { resolveEffortModel, resolveModelEffort, reconcileModelEffort } from '$lib/constants/model-effort';
	// The controls sheet is the mobile app's; the web build registers none.
	import { composerShell } from './composer-shell';
	import { Globe, Plus, SlidersHorizontal } from 'lucide-svelte';
	import {
		getModelLabel,
		getDefaultModel,
		supportsAutoPermission,
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
		/** The dictate mic and voice-call buttons, rendered left of attach. */
		voiceActions?: Snippet;
		onBrowserToggle?: () => void;
		onBrowserPreload?: () => void;
		browserOpen?: boolean;
		/** Keeps the mobile editor expanded while its integrated panel is open. */
		onSheetOpenChange?: (open: boolean) => void;
		/** Collapsed / expanding-tap: dock stays in the tree but cannot receive the expand click. */
		inertToolbar?: boolean;
	}

	let { isUploading, onAttachClick, voiceActions, onSheetOpenChange, inertToolbar = false, onBrowserToggle, onBrowserPreload, browserOpen = false }: Props = $props();

	// ── Desktop popovers ──────────────────────────────────────────────
	// Three anchored popovers, one open at a time: the permission menu, the
	// model picker, and the compact effort card.
	type DockPopover = 'permission' | 'model' | 'effort';
	let openPopover = $state<DockPopover | null>(null);
	let dockRef = $state<HTMLDivElement | null>(null);
	// Vertical room above the row, measured when a popover opens. The popovers
	// open upward, and in the new-chat state the box sits mid-screen, so a
	// fixed height would push the tall ones past the top of the viewport.
	let popoverRoom = $state<number | null>(null);
	let permissionWrapRef = $state<HTMLDivElement | null>(null);
	let modelWrapRef = $state<HTMLDivElement | null>(null);
	let effortWrapRef = $state<HTMLDivElement | null>(null);

	function wrapFor(kind: DockPopover): HTMLDivElement | null {
		switch (kind) {
			case 'permission':
				return permissionWrapRef;
			case 'model':
				return modelWrapRef;
			case 'effort':
				return effortWrapRef;
		}
	}

	function measurePopoverRoom() {
		if (!dockRef) return;
		// Minus the 10px anchor gap and a 12px margin from the viewport edge.
		popoverRoom = Math.max(220, Math.floor(dockRef.getBoundingClientRect().top - 22));
	}

	function togglePopover(kind: DockPopover) {
		openPopover = openPopover === kind ? null : kind;
		if (openPopover) void tick().then(measurePopoverRoom);
	}

	function closePopover() {
		openPopover = null;
	}

	function handleDocumentClick(e: MouseEvent) {
		if (!openPopover) return;
		const wrap = wrapFor(openPopover);
		if (!wrap || !(e.target instanceof Node) || wrap.contains(e.target)) return;
		openPopover = null;
	}

	function handleWindowKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && openPopover) {
			e.stopPropagation();
			closePopover();
		}
	}

	$effect(() => {
		document.addEventListener('click', handleDocumentClick);
		window.addEventListener('keydown', handleWindowKeydown);
		return () => {
			document.removeEventListener('click', handleDocumentClick);
			window.removeEventListener('keydown', handleWindowKeydown);
		};
	});

	let mobileSheetOpen = $state(false);
	const mobilePanelId = $props.id();
	const ControlsSheet = composerShell().ControlsSheet;
	export function openMobileSheet() {
		setMobileSheetOpen(true);
	}
	function setMobileSheetOpen(open: boolean) {
		closePopover();
		if (!open && document.activeElement?.closest('.mobile-composer-controls')) {
			dockRef?.querySelector<HTMLButtonElement>('.mobile-settings-trigger')?.focus({ preventScroll: true });
		}
		mobileSheetOpen = open;
		onSheetOpenChange?.(open);
	}
	// The controls sheet belongs to the mobile shell, which CSS hides outside
	// the mobile platform attribute. The shell is fixed for the life of the
	// page, so there is no resize that can strand an open sheet on desktop.

	// Model / effort / permission are the SESSION's run binding, not an override
	// of the agent's: an agent is the configuration (prompt, tools, memory) and
	// carries no model. The user picks the binding when the chat is created —
	// seeded from whatever they picked last — and may change it mid-session;
	// every send carries it, and the backend stores it on the session row.
	const effortValue = $derived($activeSession.effortLevelOverride);
	const autoPermission = $derived(supportsAutoPermission($activeSession.modelOverride || $activeSession.liveRequestedModel || $activeSession.liveModel || getDefaultModel()));
	const permissionValue = $derived($activeSession.permissionModeOverride === 'auto' && !autoPermission ? 'default' : $activeSession.permissionModeOverride ?? 'default');
	// Chat and voice are projectless, web-only lanes with a fixed tool surface
	// (decided server-side), so there is no access to grant.
	const isCodeLane = $derived($activeSession.mode !== 'chat'); // voice shares code controls
	// Attach survives into the chat lane. Its uploads skip the project endpoint
	// for a shared workspace directory, and the chat ceiling carries a `Read`
	// scoped to that directory, so an attached file is genuinely readable there.
	// Voice is the one lane with no upload path — the call surface has no
	// composer to attach into.
	const showAttach = true;

	// Flipping lanes (or opening a chat session) unmounts the permission trigger.
	// `openPopover` would stay pinned to it, and `handleDocumentClick` bails when
	// the wrapper is gone — leaving the dock unable to open any other popover.
	$effect(() => {
		if (!isCodeLane && openPopover === 'permission') openPopover = null;
	});
	const attachDisabled = $derived(
		!$anyCredential || isUploading || isRemoteOnlyCompose($activeSession.project, $activeSession.projectV2Id)
	);

	// ── Model override ──────────────────────────────────────────────
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
	// Offered only on models that carry the tier (Claude Opus, GPT-5 and Astra on
	// an OpenAI/Codex provider row). It rides the effort popover because both
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
	// When rerouted the model text shows the ACTUAL model (what's really
	// running); otherwise it shows the selection.
	const modelButtonLabel = $derived(rerouted ? actualModelLabel : selectedModelLabel);
	const modelWarnTitle = $derived(
		rerouted
			? `Model rerouted: running ${actualModelLabel} (requested ${requestedModelLabel})`
			: undefined
	);
	const modelNotice = $derived(
		rerouted ? `Running ${actualModelLabel} instead of ${requestedModelLabel}` : undefined
	);

	// Normal requests dedupe, but opening during a mount request must still force
	// a refresh. Follow-up requests only read the progressively persisted rows.
	let usageFetchInFlight: Promise<boolean> | null = null;
	async function refreshUsageLimits(force = false): Promise<boolean> {
		if (usageFetchInFlight) {
			if (!force) return usageFetchInFlight;
			await usageFetchInFlight;
		}
		const request = getUsageLimits(force)
			.then((snap) => {
				rateLimit.applySnapshot(snap);
				return snap.provider_refreshing === true;
			})
			.catch(() => {
				handledTarget = undefined;
				return false;
			});
		usageFetchInFlight = request;
		try { return await request; }
		finally { if (usageFetchInFlight === request) usageFetchInFlight = null; }
	}

	let usagePollGeneration = 0;
	let usagePollTimer: ReturnType<typeof setTimeout> | undefined;
	function stopUsagePolling() {
		usagePollGeneration++;
		clearTimeout(usagePollTimer);
	}
	function onUsageOpenChange(open: boolean) {
		stopUsagePolling();
		if (!open) return;
		const generation = usagePollGeneration;
		const deadline = Date.now() + 120_000;
		async function poll(force: boolean) {
			if (force && usageFetchInFlight) await usageFetchInFlight;
			if (generation !== usagePollGeneration || Date.now() >= deadline) return;
			const refreshing = await refreshUsageLimits(force);
			if (generation !== usagePollGeneration || !refreshing || Date.now() >= deadline) return;
			usagePollTimer = setTimeout(() => {
				if (generation === usagePollGeneration && Date.now() < deadline) void poll(false);
			}, 1_000);
		}
		void poll(true);
	}
	onDestroy(stopUsagePolling);

	// Populate the ring on load AND keep it right when the viewed/composed session
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

	const permissionModes = $derived([
		{ value: 'default', label: 'Ask', description: 'Confirm before tools' },
		...(autoPermission ? [{ value: 'auto', label: 'Auto', description: 'Claude Code evaluates tool calls automatically' }] : []),
		{ value: 'acceptEdits', label: 'Accept Edits', description: 'Auto-approve edits' },
		{ value: 'bypassPermissions', label: 'Bypass', description: 'Execute all tools' }
	]);

	const effortModel = $derived(resolveEffortModel(
		$activeSession.modelOverride, liveModel, liveRequestedModel, $activeSession.liveProvider
	));
	const effortCapability = $derived(resolveModelEffort(effortModel));
	const effortLevels = $derived(effortCapability.options);
	$effect(() => {
		const reconciled = reconcileModelEffort(effortValue, effortCapability);
		if (reconciled !== effortValue) activeSession.setEffortLevelOverride(reconciled);
	});

	// Effort shows its pinned level when one is set, else "Auto" (the model's
	// own default); access always names the effective mode (it defaults to
	// 'default' → Ask, never blank).
	const effortLabel = $derived(
		!effortCapability.known ? (effortValue ?? 'Unknown') : !effortLevels.length ? 'Not adjustable' : effortValue ? (effortLevels.find((m) => m.value === effortValue)?.label ?? 'Auto') : 'Auto'
	);
	const permissionLabel = $derived(
		permissionModes.find((m) => m.value === permissionValue)?.label ?? 'Ask'
	);
	const permissionDescription = $derived(
		permissionModes.find((m) => m.value === permissionValue)?.description ?? ''
	);
</script>

<div
	class="controls-dock"
	bind:this={dockRef}
	inert={inertToolbar}
	style:--dock-pop-max={popoverRoom != null ? `${popoverRoom}px` : undefined}
>
	<!-- Left group: permission · voice · attach -->
	<div class="dock-group dock-left">
		<button
			type="button"
			class="dock-ico composer-mobile-only mobile-settings-trigger"
			class:warn={rerouted}
			onclick={() => setMobileSheetOpen(!mobileSheetOpen)}
			aria-controls={mobilePanelId}
			aria-expanded={mobileSheetOpen}
			aria-label={`${mobileSheetOpen ? 'Close' : 'Open'} chat controls. Current model: ${modelButtonLabel}`}
		>
			<SlidersHorizontal size={14} strokeWidth={1.8} aria-hidden="true" />
		</button>
		{#if isCodeLane}
			<div class="dock-pop-wrap desktop-controls" bind:this={permissionWrapRef}>
				<button
					type="button"
					class="dock-txt"
					class:open={openPopover === 'permission'}
					onclick={() => togglePopover('permission')}
					title={`Tool access: ${permissionLabel} — ${permissionDescription}`}
					aria-haspopup="menu"
					aria-expanded={openPopover === 'permission'}
					aria-label="Permission mode: {permissionLabel}"
				>{permissionLabel}</button>

				{#if openPopover === 'permission'}
					<div
						class="dock-menu permission-menu"
						role="menu"
						aria-label="Tool access"
						data-composer-owned-overlay="true"
					>
						<div class="access-list">
							{#each permissionModes as mode (mode.value)}
								<button
									type="button"
									class="access-row"
									class:selected={mode.value === permissionValue}
									onclick={() => {
										activeSession.setPermissionModeOverride(mode.value);
										closePopover();
									}}
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
					</div>
				{/if}
			</div>
		{/if}

		<!-- Dictate / voice call, supplied by ChatInput — left of attach. -->
		{@render voiceActions?.()}

		{#if onBrowserToggle}
			<button
				type="button"
				class="dock-ico browser-toggle-btn"
				class:active={browserOpen}
				onclick={onBrowserToggle}
				onpointerenter={onBrowserPreload}
				onfocus={onBrowserPreload}
				aria-label="Toggle live browser view"
				title="Toggle live browser view"
				aria-pressed={browserOpen}
			>
				<Globe size={14} strokeWidth={1.8} aria-hidden="true" />
			</button>
		{/if}

		<!-- Attach stays in the primary action cluster on every viewport, in the
		     code and chat lanes both. A code-lane upload lands in the project's
		     or worktree's uploads dir; a chat-lane one lands in the workspace's
		     shared chat-uploads dir. Either way it reaches the model as an @path
		     it has to Read — which is exactly why the chat tool ceiling carries a
		     Read pinned to that directory. Voice has no composer upload path. -->
		{#if showAttach}
		<button
			type="button"
			onclick={onAttachClick}
			disabled={attachDisabled}
			class="dock-ico attach-btn"
			aria-label="Attach file"
			title={isUploading
				? 'Uploading...'
				: (attachmentBlockReason(
						$activeSession.project,
						$activeSession.projectV2Id,
						$activeSession.mode
					) ?? 'Attach file')}
		>
			{#if isUploading}
				<svg class="w-[14px] h-[14px] animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
					<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
				</svg>
			{:else}
				<Plus size={14} strokeWidth={1.8} aria-hidden="true" />
			{/if}
		</button>
		{/if}

	</div>

	<!-- Mobile: usage ring on the right. Settings lives in dock-left. -->
	<div class="mobile-controls">
		<ContextUsageBar onOpenChange={onUsageOpenChange} />
	</div>

	<!-- Right group (desktop): model · effort · ring -->
	<div class="dock-group dock-right desktop-controls">
		<div class="dock-pop-wrap" bind:this={modelWrapRef}>
			<button
				type="button"
				class="dock-txt model-btn"
				class:open={openPopover === 'model'}
				class:warn={rerouted}
				onclick={() => togglePopover('model')}
				title={modelWarnTitle ?? `Model: ${modelButtonLabel}`}
				aria-haspopup="listbox"
				aria-expanded={openPopover === 'model'}
				aria-label="Model: {modelButtonLabel}"
			>{modelButtonLabel}</button>

			{#if openPopover === 'model'}
				<div
					class="dock-menu model-menu"
					role="dialog"
					aria-label="Choose model"
					data-composer-owned-overlay="true"
				>
					<ModelPickerPanel
						groups={modelCatalog.groups}
						options={modelCatalog.options}
						value={modelValue}
						notice={modelNotice}
						contextChoice={$activeSession.contextWindowChoice}
						onContextChoiceSelect={(choice) => activeSession.setContextWindowChoice(choice)}
						onSelect={(v) => {
							// Stay open so context-window controls remain reachable.
							activeSession.setModelOverride(v);
						}}
						onRequestClose={closePopover}
					/>
				</div>
			{/if}
		</div>

		<div class="dock-pop-wrap" bind:this={effortWrapRef}>
			<button
				type="button"
				class="dock-tag effort-btn"
				class:open={openPopover === 'effort'}
				onclick={() => togglePopover('effort')}
				title={effortCapability.explanation ?? (effortValue ? `Reasoning effort: ${effortLabel}` : 'Reasoning effort: model default')}
				aria-haspopup="dialog"
				aria-expanded={openPopover === 'effort'}
				aria-label="Effort: {effortLabel}"
			>{effortLabel}</button>

			{#if openPopover === 'effort'}
				<div
					class="dock-menu effort-menu"
					role="dialog"
					aria-label="Reasoning effort"
					data-composer-owned-overlay="true"
				>
					<EffortPopoverCard
						options={effortLevels}
						known={effortCapability.known}
						explanation={effortCapability.explanation}
						defaultValue={effortCapability.defaultValue}
						value={effortValue}
						onSelect={(v) => activeSession.setEffortLevelOverride(v)}
						{fastSupported}
						fastEnabled={fastMode}
						onToggleFast={() => activeSession.setFastMode(!fastMode)}
					/>
				</div>
			{/if}
		</div>

		<!-- Context-usage ring (16px SVG arc + its popover) -->
		<ContextUsageBar onOpenChange={onUsageOpenChange} />
	</div>
</div>

{#if ControlsSheet}
<ControlsSheet
	open={mobileSheetOpen}
	panelId={mobilePanelId}
	{inertToolbar}
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
	effortExplanation={effortCapability.explanation}
	effortKnown={effortCapability.known}
	{effortValue}
	onEffortSelect={(value) => activeSession.setEffortLevelOverride(value)}
	{fastSupported}
	fastEnabled={fastMode}
	onToggleFast={() => activeSession.setFastMode(!fastMode)}
	permissionOptions={permissionModes}
	permissionValue={permissionValue}
	onPermissionSelect={(value) => activeSession.setPermissionModeOverride(value)}
	showCodeControls={isCodeLane}
/>
{/if}

<style>
	/* ===== CONTROLS ROW (mockup `.controls`) =====
	   No surface of its own — the box above provides the hairline frame. */
	.controls-dock {
		/* Every popover in this row opens UPWARD over the input area directly
		   above it. That area sets `z-index: 20`, so the row is lifted above it
		   to keep its popovers fully visible. */
		position: relative;
		z-index: 30;
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 4px 10px 10px 14px;
		overflow: visible;
	}

	.dock-group {
		display: flex;
		align-items: center;
		gap: 4px;
		min-width: 0;
	}

	/* `margin-left: auto` (not a second flex:1 half) so the left group keeps
	   its natural width and the right group hugs the box edge. */
	.dock-right {
		margin-left: auto;
	}

	.dock-pop-wrap {
		position: relative;
		min-width: 0;
		display: flex;
		align-items: center;
	}

	/* ===== PLAIN-TEXT CONTROLS (mockup `.txt`) ===== */
	.dock-txt {
		display: inline-flex;
		align-items: center;
		max-width: 100%;
		min-width: 0;
		padding: 3px 5px;
		background: transparent;
		border: 0;
		border-radius: 4px;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 500;
		line-height: 1;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease;
	}

	.dock-txt:hover,
	.dock-txt.open {
		background: var(--muted);
		color: var(--foreground);
	}

	.model-btn {
		max-width: min(220px, 30vw);
		display: block;
	}

	/* Reroute: amber text — the same treatment the old model pill carried, so
	   the "running something else" signal keeps its established look. */
	.model-btn.warn,
	.model-btn.warn:hover,
	.model-btn.warn.open {
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 10%, transparent);
	}

	/* ===== ICON BUTTONS (mockup `.ico`) ===== */
	.dock-ico {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		padding: 0;
		background: transparent;
		border: 0;
		border-radius: 5px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 0.12s ease, color 0.12s ease;
	}

	.dock-ico:hover:not(:disabled) {
		background: var(--muted);
		color: var(--foreground);
	}

	.dock-ico.browser-toggle-btn.active {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	.dock-ico:disabled {
		opacity: 0.35;
		cursor: not-allowed;
	}

	/* The mic and voice-call buttons ChatInput passes in take the same 24px
	   footprint as the row's own icon buttons. */
	.dock-left :global(.voice-btn),
	.dock-left :global(.voice-call-btn) {
		width: 24px;
		height: 24px;
		border-radius: 5px;
	}

	.dock-left :global(.voice-btn-wrapper) {
		border-radius: 5px;
	}

	/* ===== EFFORT TAG (mockup `.tag`) ===== */
	.dock-tag {
		display: inline-flex;
		align-items: center;
		padding: 4px 7px;
		background: var(--muted);
		border: 0;
		border-radius: 4px;
		color: var(--foreground);
		font: inherit;
		font-size: 11.5px;
		font-weight: 500;
		line-height: 1;
		white-space: nowrap;
		cursor: pointer;
		transition: background-color 0.12s ease, box-shadow 0.12s ease;
	}

	.dock-tag:hover {
		background: color-mix(in oklch, var(--muted) 82%, var(--foreground));
	}

	.dock-tag.open {
		background: color-mix(in oklch, var(--muted) 82%, var(--foreground));
		box-shadow: inset 0 0 0 1px var(--border-strong);
	}

	.dock-txt:focus-visible,
	.dock-ico:focus-visible,
	.dock-tag:focus-visible {
		outline: 1px solid var(--primary);
		outline-offset: 2px;
	}

	/* ===== POPOVERS =====
	   All open UPWARD from the row — same anchor convention as the ring's
	   popover beside them. Left-group popovers hang off their left edge, the
	   right group's off their right edge, so nothing spills past the box. */
	.dock-menu {
		position: absolute;
		bottom: calc(100% + 10px);
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.8);
		box-shadow: var(--shadow-l);
		z-index: 60;
		animation: dock-popover-in 0.16s cubic-bezier(0.16, 1, 0.3, 1);
	}

	.permission-menu {
		left: 0;
		width: min(280px, calc(100vw - 24px));
		padding: 4px;
	}

	.model-menu {
		right: 0;
		width: min(380px, calc(100vw - 24px));
		padding: 6px;
		--mpp-width: 100%;
		/* The list scrolls above the pinned context footer in short viewports. */
		--mpp-height: min(400px, calc(var(--dock-pop-max, 454px) - 14px));
	}

	.effort-menu {
		right: -6px;
		width: min(280px, calc(100vw - 24px));
	}

	@keyframes dock-popover-in {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.access-list {
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--muted) 40%, var(--card));
		border: 1px solid var(--border-subtle);
		border-radius: calc(var(--radius, 0.75rem) * 0.9);
		overflow: hidden;
	}

	/* Inside the plain permission menu the list is the popover itself. */
	.permission-menu .access-list {
		background: transparent;
		border: 0;
		border-radius: 0;
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

	.permission-menu .access-row {
		border-bottom: 0;
		border-radius: calc(var(--radius, 0.75rem) * 0.6);
		padding: 7px 10px;
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

	/* ===== MOBILE ===== */
	.mobile-controls,
	.composer-mobile-only {
		display: none;
	}

	.mobile-controls {
		align-items: center;
		gap: 3px;
		min-width: 0;
	}

	.mobile-settings-trigger.warn {
		color: var(--warning, var(--primary));
	}

</style>
