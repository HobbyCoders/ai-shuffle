<script lang="ts">
	/**
	 * ControlsDock — the bare-text controls row at the foot of the composer box
	 * (mockup: docs/design/composer-split-chips.mockup.html).
	 *
	 * Desktop, left to right:
	 *   permission mode as plain text (opens the tool-access menu) · the live
	 *   browser toggle · the terminal toggle (when this viewer can open one).
	 *   Attach, dictate and Talk/Send are ChatInput's circles beside the text
	 *   (or at this row's two ends once the text wraps).
	 * Right, left to right:
	 *   model name as plain text (opens the model picker) · effort as a filled
	 *   tag (opens the compact effort popover) · the context-usage ring.
	 *
	 * The permission control shows in every session, on both viewports (#371
	 * step 4): Ask, Auto, Bypass and Away everywhere; Accept Edits only once a
	 * project is picked, since a session without one has no file edit to
	 * approve (session-scope.ts holds that rule). Everything else — dictate/call,
	 * model, effort, the usage ring and the mobile settings trigger — is
	 * present in every session too.
	 *
	 * Only one dock popover is open at a time; the ring's own popover closes
	 * itself on any outside click, so the two never stack.
	 *
	 * The phone shell hides this row and shows its quick bar instead
	 * (MobileComposerControlsSheet). The send button is not here on any
	 * viewport: it is ChatInput's.
	 *
	 * Computer use is a GLOBAL toggle (Settings > Security), not a composer
	 * control: the dock neither shows a toggle nor polls status. Halting it for a
	 * session is the Stop button's job.
	 */
	import { onDestroy, tick, type Snippet } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { standardContextCap } from '$lib/stores/session/context-window';
	import { contextCapRevision } from '$lib/stores/composer-preferences';
	import { notify } from '$lib/stores/notifications';
	import { getVoiceSessionConfig, loadVoiceSettings, usesDelegation, voiceModelLabel, type VoiceOptions } from '$lib/api/voice';
	import { watchResources } from '$lib/services/watch-resources';
	import ContextUsageBar from './ContextUsageBar.svelte';
	import EffortPopoverCard from './EffortPopoverCard.svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';
	import { resolveEffortModel, resolveModelEffort, reconcileModelEffort } from '$lib/constants/model-effort';
	// The controls sheet is the mobile app's; the web build registers none.
	import { composerShell } from './composer-shell';
	import { Globe, SlidersHorizontal, Lock, SquareTerminal } from 'lucide-svelte';
	import type { TerminalToggle } from './composer-shell';
	import {
		getModelLabel,
		getDefaultModel,
		getClaudeAuth,
		decodeProviderModel,
		supportsAutoPermission,
		isFastModeCapable,
		isReroute
	} from '$lib/constants/models';
	import { rerouteTooltip } from '$lib/utils/guardrail';
	import { getUsageLimits } from '$lib/api/client';
	import {
		rateLimit,
		resolveBucket,
		resolveBucketKey,
		isBucketStale,
		BUCKET_STALE_MS
	} from '$lib/stores/rate-limit';
	import { usageTargetInstance } from '$lib/stores/usage-target';
	import { awayMode } from '$lib/stores/away-mode.svelte';
	import { sessions } from '$lib/stores/app-data.svelte';
	import { accessModes, effectiveAccessMode } from './session-scope';

	interface Props {
		onBrowserToggle?: () => void;
		onBrowserPreload?: () => void;
		browserOpen?: boolean;
		/** The in-app terminal toggle (#1945); absent when this viewer can't open one. */
		terminal?: TerminalToggle;
		/** Keeps the mobile editor expanded while its integrated panel is open. */
		onSheetOpenChange?: (open: boolean) => void;
		/** Collapsed / expanding-tap: dock stays in the tree but cannot receive the expand click. */
		inertToolbar?: boolean;
		/** The start-a-call button, for a shell whose controls sheet carries it. */
		callAction?: Snippet;
	}

	let { callAction, onSheetOpenChange, inertToolbar = false, onBrowserToggle, onBrowserPreload, browserOpen = false, terminal }: Props = $props();

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
			e.preventDefault();
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
	export function setMobileSheetOpen(open: boolean) {
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
	// The Standard cap shown in the model picker: the chat's pin, else the
	// owner's per-model default (synced preferences bump the revision).
	const contextStandardTokens = $derived.by(() => {
		void $contextCapRevision;
		return standardContextCap($activeSession);
	});
	const autoPermission = $derived(supportsAutoPermission($activeSession.modelOverride || $activeSession.liveRequestedModel || $activeSession.liveModel || getDefaultModel()));
	// A session with a project ('code'; historical voice shares it) offers
	// Accept Edits too. The selected value reads as Ask when the menu does not
	// offer it, which is what the backend runs it as.
	const hasProject = $derived($activeSession.mode !== 'chat');
	const accessContext = $derived({ hasProject, autoSupported: autoPermission });
	// An opened chat whose own access / Fast have not arrived yet (a hidden Task
	// worker or a deep link, #418) shows them loading — never a made-up Ask or
	// Fast off — and a send leaves them out until they do.
	const permissionPending = $derived(!!$activeSession.sessionId && !!$activeSession.bindingUnhydrated?.permission);
	const fastPending = $derived(!!$activeSession.sessionId && !!$activeSession.bindingUnhydrated?.fast);
	const permissionValue = $derived(permissionPending ? null : effectiveAccessMode($activeSession.permissionModeOverride, accessContext));

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
	let voiceEngine = $state<string | null>(null);
	let voiceOptions = $state<VoiceOptions | undefined>(undefined);
	let voiceRevision = $state(0);
	watchResources(['settings'], () => { voiceRevision++; });
	const voiceLane = $derived($activeSession.mode === 'voice');
	// Depend on the binding, not the whole store: transcript/usage updates
	// must not clear and reload the voice engine (or close the model picker).
	const voiceSessionId = $derived($activeSession.sessionId);
	let configSessionId: string | null | undefined;
	const modelLocked = $derived(voiceLane && (!voiceEngine || !usesDelegation(voiceEngine, voiceOptions)));
	$effect(() => {
		const id = voiceSessionId;
		const voice = voiceLane;
		void voiceRevision;
		let stale = false;
		// Retain the resolved binding during a settings refresh, but never
		// carry it into another session or out of the voice lane.
		if (!voice || id !== configSessionId) {
			voiceEngine = null;
			voiceOptions = undefined;
		}
		configSessionId = id;
		if (voice) {
			const request = id ? getVoiceSessionConfig(id) : loadVoiceSettings().then(s => ({model:s.voice_model,options:s.voice_options}));
			void request.then(config => {
				if (stale) return;
				voiceEngine = config.model; voiceOptions = config.options;

			}).catch(() => {});
		}
		return () => { stale = true; };
	});
	$effect(() => { if (modelLocked && (openPopover === 'model' || openPopover === 'effort')) closePopover(); });

	// ── Fast service tier ───────────────────────────────────────────
	// Offered only on models that carry the tier (Claude Opus, GPT-5 and GPT-6 on
	// an OpenAI/Codex provider row). It rides the effort popover because both
	// are knobs on how hard — and how fast — the turn runs.
	const fastMode = $derived($activeSession.fastMode);
	const fastSupported = $derived(isFastModeCapable(modelValue));

	// Each model remembers its own pick (composerPreferences.fastForModel), which
	// a model switch restores. Switching to a model without the tier must not
	// leave a stale flag on the session — the toggle would be invisible while
	// every send still asked for it — and must not forget the owner's pick.
	$effect(() => {
		if (fastMode && !fastSupported) activeSession.setFastMode(false, false);
	});

	// Live reroute detection: the backend `model_info` note tells us which model
	// the MAIN lane is ACTUALLY running, and (newer backends) whether that is a
	// reroute (`rerouted`, with `reroute_reason`). The backend's verdict wins
	// whenever it is present, for every provider; without it the old FAMILY
	// comparison applies (exact-ID comparison false-flags dated snapshot ids),
	// and a provider session is never flagged on that heuristic (R1). Unknown
	// models never flag; a note missing either model never flags.
	const liveModel = $derived($activeSession.liveModel);
	const liveRequestedModel = $derived($activeSession.liveRequestedModel);
	// Compare actual vs what was REQUESTED for that same turn (both from the
	// model_info note) — never the live selection, which the user may have
	// changed since.
	const rerouted = $derived(
		isReroute(liveModel, liveRequestedModel, $activeSession.liveProvider, $activeSession.liveRerouted)
	);
	const hasModelRoute = $derived(Boolean($activeSession.sessionId || getClaudeAuth().auth_kind || decodeProviderModel(modelValue)));
	const selectedModelLabel = $derived(hasModelRoute ? getModelLabel(modelValue) : 'Select model');
	const actualModelLabel = $derived(getModelLabel(liveModel));
	const requestedModelLabel = $derived(getModelLabel(liveRequestedModel));
	// When rerouted the model text shows the ACTUAL model (what's really
	// running); otherwise it shows the selection.
	const modelButtonLabel = $derived(modelLocked ? (voiceEngine ? voiceModelLabel(voiceEngine) : 'Voice model…') : voiceLane ? selectedModelLabel : rerouted ? actualModelLabel : selectedModelLabel);
	const modelWarnTitle = $derived(
		rerouted
			? rerouteTooltip(actualModelLabel, requestedModelLabel, $activeSession.liveRerouteReason)
			: undefined
	);
	const modelNotice = $derived(
		rerouted
			? `${$activeSession.liveRerouteReason === 'refusal_fallback' ? 'Safety check: running' : 'Running'} ${actualModelLabel} instead of ${requestedModelLabel}`
			: undefined
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
	let usageOpen = false;
	function onUsageOpenChange(open: boolean) {
		usageOpen = open;
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

	// Provider quotas (Codex headers, for example) are captured from the
	// session's own responses and persisted, not broadcast. Re-read the cached
	// rows when a turn ends with the popover open so it does not keep showing
	// the reading from before the turn. A plain read: no provider request.
	let usageTurnStreaming = false;
	$effect(() => {
		const streaming = $activeSession.isStreaming === true;
		if (usageTurnStreaming && !streaming && usageOpen) void refreshUsageLimits();
		usageTurnStreaming = streaming;
	});

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
		// The merged reading when other installs share this one's Claude account.
		const bucket = resolveBucket(state, target);
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

	let permissionUpdating = $state(false);
	async function selectPermission(mode: string) {
		if (permissionUpdating) return;
		permissionUpdating = true;
		try {
			await activeSession.setPermissionModeOverride(mode);
			closePopover();
		} catch (error) {
			notify(error instanceof Error ? error.message : 'Could not change permissions', {
				severity: 'error', title: 'Permission change failed'
			});
		} finally {
			permissionUpdating = false;
		}
	}

	// Away mode: the agent takes recommended defaults instead of asking and
	// reports them at the end; approvals are deferred, never granted.
	const awaySessionId = $derived($activeSession.sessionId);
	const away = $derived(awayMode.isAway(awaySessionId));
	$effect(() => {
		void awayMode.load(awaySessionId);
	});
	// The session row reloads when the flag changes on this worker or arrives
	// from the worker that owns the session, so another device's toggle shows here.
	const awayRowValue = $derived($sessions.find((s) => s.id === awaySessionId)?.away_mode);
	$effect(() => {
		awayMode.observe(awaySessionId, awayRowValue);
	});
	async function toggleAway() {
		if (!awaySessionId || awayMode.busy[awaySessionId]) return;
		try {
			const result = await awayMode.set(awaySessionId, !away);
			if (result.released_questions) {
				notify(`Answered ${result.released_questions} waiting question${result.released_questions === 1 ? '' : 's'} with recommended defaults`, { severity: 'info' });
			}
		} catch (error) {
			notify(error instanceof Error ? error.message : 'Could not change Away mode', {
				severity: 'error', title: 'Away mode'
			});
		}
	}

	const permissionModes = $derived(accessModes(accessContext));

	const effortModel = $derived(voiceLane ? modelValue : resolveEffortModel(
		$activeSession.modelOverride, liveModel, liveRequestedModel, $activeSession.liveProvider,
		$activeSession.liveRerouted
	));
	const effortCapability = $derived(resolveModelEffort(effortModel));
	const effortLevels = $derived(effortCapability.options);
	$effect(() => {
		const reconciled = reconcileModelEffort(effortValue, effortCapability);
		if (reconciled !== effortValue) activeSession.setEffortLevelOverride(reconciled, false);
	});

	// Effort shows its pinned level when one is set, else "Auto" (the model's
	// own default); access always names the effective mode (it defaults to
	// 'default' → Ask, never blank).
	const effortLabel = $derived(
		!effortCapability.known ? (effortValue ?? 'Unknown') : !effortLevels.length ? 'Not adjustable' : effortValue ? (effortLevels.find((m) => m.value === effortValue)?.label ?? 'Auto') : 'Auto'
	);
	const permissionLabel = $derived(
		permissionPending ? 'Access' : permissionModes.find((m) => m.value === permissionValue)?.label ?? 'Ask'
	);
	const permissionDescription = $derived(
		permissionPending ? "loading this chat's own setting" : permissionModes.find((m) => m.value === permissionValue)?.description ?? ''
	);
</script>

<div
	class="controls-dock"
	bind:this={dockRef}
	inert={inertToolbar}
	style:--dock-pop-max={popoverRoom != null ? `${popoverRoom}px` : undefined}
>
	<!-- Left group: access mode · browser · terminal -->
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
			<div class="dock-pop-wrap desktop-controls" bind:this={permissionWrapRef} data-decision="access">
				<button
					type="button"
					class="dock-txt"
					class:open={openPopover === 'permission'}
					class:binding-pending={permissionPending}
					onclick={() => togglePopover('permission')}
					title={`Tool access: ${permissionLabel} — ${permissionDescription}`}
					aria-haspopup="menu"
					aria-expanded={openPopover === 'permission'}
					aria-busy={permissionPending || undefined}
					aria-label="Permission mode: {permissionPending ? 'loading' : permissionLabel}{away ? ', Away mode on' : ''}"
				>{permissionLabel}{#if away}<span class="away-badge" title="Away: agent decides with recommended defaults and reports them at the end">· Away</span>{/if}</button>

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
									disabled={permissionUpdating}
									onclick={() => selectPermission(mode.value)}
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
						<div class="access-list away-list">
							<button
								type="button"
								class="access-row away-row"
								class:selected={away}
								disabled={!awaySessionId || !!(awaySessionId && awayMode.busy[awaySessionId])}
								onclick={toggleAway}
								role="menuitemcheckbox"
								aria-checked={away}
								title={awaySessionId ? 'Away: agent decides with recommended defaults and reports them at the end' : 'Start the chat first'}
							>
								<span class="access-copy">
									<strong>Away</strong>
									<small>{awaySessionId ? 'Don\'t ask me; use recommended defaults and list them at the end' : 'Available once the chat has started'}</small>
								</span>
								{#if away}
									<svg class="w-3.5 h-3.5 access-check" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
									</svg>
								{/if}
							</button>
						</div>
					</div>
				{/if}
			</div>


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

		{#if terminal}
			<button
				type="button"
				class="dock-ico terminal-toggle-btn"
				class:active={terminal.open}
				class:muted={!!terminal.disabledReason}
				onclick={terminal.toggle}
				onpointerenter={terminal.preload}
				onfocus={terminal.preload}
				aria-label="Toggle terminal"
				title={terminal.disabledReason ?? 'Toggle terminal'}
				aria-pressed={terminal.open}
			>
				<SquareTerminal size={14} strokeWidth={1.8} aria-hidden="true" />
			</button>
		{/if}


	</div>

	<!-- One right group at every width. Only model/effort hide in the phone shell;
	     the context producer stays mounted, preserving its fetch and open popover. -->
	<div class="dock-group dock-right">
		<div class="dock-pop-wrap desktop-controls model-wrap" bind:this={modelWrapRef}>
			<button
				type="button"
				class="dock-txt model-btn"
				class:open={openPopover === 'model'}
				class:warn={rerouted && !voiceLane}
				disabled={modelLocked}
				onclick={() => { if (!modelLocked) togglePopover('model'); }}
				title={modelLocked ? `Voice model: ${modelButtonLabel}` : modelWarnTitle ?? `Chat model: ${modelButtonLabel}`}
				aria-haspopup="listbox"
				aria-expanded={openPopover === 'model'}
				aria-label="Model: {modelButtonLabel}"
			>{#if modelLocked}<Lock size={11} aria-hidden="true" />{/if}<span>{modelButtonLabel}</span></button>

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
						contextStandardTokens={contextStandardTokens}
						onContextStandardChange={(tokens) => activeSession.setContextStandardCap(tokens)}
						onSelect={(v) => {
							// Stay open so context-window controls remain reachable.
							activeSession.setModelOverride(v);
						}}
						onRequestClose={closePopover}
					/>
				</div>
			{/if}
		</div>

		<div class="dock-pop-wrap desktop-controls" bind:this={effortWrapRef}>
			<button
				type="button"
				class="dock-tag effort-btn"
				class:open={openPopover === 'effort'}
				disabled={modelLocked}
				onclick={() => { if (!modelLocked) togglePopover('effort'); }}
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
						{fastPending}
						onToggleFast={() => { if (!fastPending) activeSession.setFastMode(!fastMode); }}
					/>
				</div>
			{/if}
		</div>

		<!-- One producer across responsive web widths. The registered phone shell
		     owns its quick-bar ring; its CSS-hidden dock must not start another. -->
		{#if !ControlsSheet}
		<div class="mobile-controls">
			<ContextUsageBar onOpenChange={onUsageOpenChange} />
		</div>
		{/if}
	</div>
</div>

{#if ControlsSheet}
<ControlsSheet
	open={mobileSheetOpen}
	panelId={mobilePanelId}
	{inertToolbar}
	onClose={() => setMobileSheetOpen(false)}
	onOpenChange={setMobileSheetOpen}
	{modelOptions}
	modelGroups={modelCatalog.groups}
	{modelValue}
	modelLabel={modelButtonLabel}
		{modelLocked}
	modelNotice={modelNotice}
	onModelSelect={(value) => activeSession.setModelOverride(value)}
	contextChoice={$activeSession.contextWindowChoice}
	onContextChoiceSelect={(choice) => activeSession.setContextWindowChoice(choice)}
	contextStandardTokens={contextStandardTokens}
	onContextStandardChange={(tokens) => activeSession.setContextStandardCap(tokens)}
	effortOptions={effortLevels}
	effortExplanation={effortCapability.explanation}
	effortKnown={effortCapability.known}
	effortDefaultValue={effortCapability.defaultValue}
	{effortValue}
	onEffortSelect={(value) => activeSession.setEffortLevelOverride(value)}
	{fastSupported}
	fastEnabled={fastMode}
	{fastPending}
	onToggleFast={() => { if (!fastPending) activeSession.setFastMode(!fastMode); }}
	permissionOptions={permissionModes}
	permissionValue={permissionValue}
	{permissionPending}
	{permissionUpdating}
	onPermissionSelect={selectPermission}
	awayEnabled={away}
	awayAvailable={!!awaySessionId}
	awayBusy={!!(awaySessionId && awayMode.busy[awaySessionId])}
	onToggleAway={toggleAway}
	showCodeControls
	showSetup
	showEffort
	onUsageOpenChange={onUsageOpenChange}
	{callAction}
	{browserOpen}
	{onBrowserToggle}
	{onBrowserPreload}
	{terminal}
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
	.model-btn:disabled { display: inline-flex; align-items: center; gap: 4px; }
	.model-btn span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

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

	/* The chat's own value is on its way (#418): a quiet breathing label rather
	   than a made-up Ask. */
	.dock-txt.binding-pending {
		color: var(--subtle-foreground, var(--muted-foreground));
		animation: binding-pending 1.4s ease-in-out infinite;
	}

	@keyframes binding-pending {
		0%, 100% { opacity: 0.5; }
		50% { opacity: 1; }
	}

	@media (prefers-reduced-motion: reduce) {
		.dock-txt.binding-pending { animation: none; }
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

	.dock-ico.terminal-toggle-btn.muted:not(.active) {
		opacity: 0.55;
	}

	.dock-ico.browser-toggle-btn.active,
	.dock-ico.terminal-toggle-btn.active {
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
	}

	.dock-ico:disabled {
		opacity: 0.35;
		cursor: not-allowed;
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

	/* On a narrow screen the model button sits mid-row, so a 380px menu hung
	   off its right edge spills past the left of the screen. Anchor it to the
	   dock row instead and let it span the composer. */
	@media (max-width: 520px) {
		.model-wrap {
			position: static;
		}

		.model-menu {
			left: 0;
			right: 0;
			width: auto;
		}
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

	.away-list {
		border-top: 1px solid var(--border, rgba(127, 127, 127, 0.2));
		margin-top: 4px;
		padding-top: 4px;
	}
	.away-badge {
		margin-left: 4px;
		color: var(--warning, #d29922);
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
	.composer-mobile-only {
		display: none;
	}

	.mobile-controls {
		display: flex;
		align-items: center;
		gap: 3px;
		min-width: 0;
	}

	.mobile-settings-trigger.warn {
		color: var(--warning, var(--primary));
	}

</style>
