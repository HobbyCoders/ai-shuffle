<script lang="ts">
	/**
	 * ControlsDock — the composer island's foot row (mockup `.footrow`).
	 *
	 * Left: the model / effort / permission override pills followed by the
	 * context-usage ring. Right: the computer-use toggle (when enabled) and the
	 * attach button, then whatever the parent passes in the `actions` snippet —
	 * ChatInput uses it for the dictate / voice-call / send cluster so the whole
	 * right-hand group shares one flex row.
	 */
	import type { Snippet } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { anyCredential } from '$lib/stores/auth.svelte';
	import { profiles } from '$lib/stores/app-data.svelte';
	import ContextUsageBar from './ContextUsageBar.svelte';
	import OverrideDropdown from './OverrideDropdown.svelte';
	import { Monitor } from 'lucide-svelte';
	import {
		getModels,
		getProviders,
		getModelLabel,
		getDefaultModel,
		encodeProviderModel,
		getProviderDisplay,
		getClaudeDisplay,
		getClaudeAuth,
		isReroute,
		type ModelFamily
	} from '$lib/constants/models';
	import { computerUse } from '$lib/stores/computerUse.svelte';
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
	}

	let { isUploading, onAttachClick, actions }: Props = $props();

	// Computer-use toolbar toggle. A single monitor icon: lit when the agent is
	// allowed to control the screen, dimmed when paused (the process-global
	// emergency stop halts all synthetic input immediately). Polling starts only
	// while the active profile enables the capability.
	let cuBusy = $state(false);

	async function toggleComputerUse() {
		if (cuBusy) return;
		cuBusy = true;
		try {
			await computerUse.toggleStopped();
		} finally {
			cuBusy = false;
		}
	}

	// Only one override dropdown may be open at a time — opening one closes the others.
	let openDropdown = $state<'model' | 'effort' | 'permission' | null>(null);

	// The effort/permission dropdowns reflect the *effective* value: the per-session
	// override if one is set, otherwise the active profile's setting. They list only
	// real SDK values (no synthetic "Default") — selecting the profile's own value
	// clears the override so the session keeps inheriting from the profile.
	const activeProfile = $derived($profiles.find((p) => p.id === $activeSession.profile));
	const profileEffort = $derived(activeProfile?.config?.effort ?? null);
	const profilePermissionMode = $derived(activeProfile?.config?.permission_mode ?? null);
	const effortValue = $derived($activeSession.effortLevelOverride ?? profileEffort);
	const permissionValue = $derived($activeSession.permissionModeOverride ?? profilePermissionMode);
	const computerUseEnabled = $derived(!!activeProfile?.config?.computer_use?.enabled);

	// ── Model override ──────────────────────────────────────────────
	// Terse, family-level blurbs — the dropdown surfaces one line of "why pick
	// this" without turning into a spec sheet.
	const MODEL_FAMILY_BLURB: Record<ModelFamily, string> = {
		haiku: 'Fastest, lightest',
		sonnet: 'Balanced speed and depth',
		opus: 'Deep reasoning',
		fable: 'Most capable'
	};
	// Live model-switch options — the FULL provider-grouped library (PA13). The
	// native/Claude group comes first, then one group per enabled provider, with
	// provider entries encoded as `prov:<providerId>:<modelId>` so the backend can
	// rebind the turn's provider from the pick alone. R7 (a provider session may
	// only hot-swap within its own provider) is superseded: cross-provider
	// switching is now a first-class mid-session action — the backend evicts and
	// reconnects when the resolved provider changes.
	// Group headings carry a BRAND NAME plus an auth-kind subtext ("OPENAI · API"
	// vs "OPENAI · PROLITE") from the shared naming rules in `constants/models`,
	// so all three model surfaces stay consistent. Groups are keyed by provider
	// ID, never by the displayed brand — the two OpenAI rows share a brand and
	// must stay separate groups.
	const NATIVE_GROUP_KEY = '__claude__';
	const modelOptions = $derived.by(() => {
		const out: {
			value: string;
			label: string;
			description: string;
			group: string;
			groupKey: string;
			groupSubtext: string | null;
		}[] = [];
		const claudeAuth = getClaudeAuth();
		const claude = getClaudeDisplay({
			planType: claudeAuth.plan_type,
			authKind: claudeAuth.auth_kind
		});
		for (const m of getModels()) {
			out.push({
				value: m.value,
				label: m.label,
				description: MODEL_FAMILY_BLURB[m.family] ?? '',
				group: claude.name,
				groupKey: NATIVE_GROUP_KEY,
				groupSubtext: claude.subtext
			});
		}
		for (const p of getProviders()) {
			const display = getProviderDisplay(p);
			for (const pm of p.models) {
				out.push({
					value: encodeProviderModel(p.id, pm.id),
					label: pm.label,
					description: pm.id,
					group: display.name,
					groupKey: p.id,
					groupSubtext: display.subtext
				});
			}
		}
		return out;
	});
	// Effective selection: the per-session override if set, else the profile's
	// model, else the registry default. Selecting the profile's own model clears
	// the override (mirrors the effort/permission pattern) — for a provider-bound
	// profile that means the profile's COMPOSITE, so the checkmark lands on the
	// right row and re-picking it still clears the override.
	const profileModel = $derived.by(() => {
		const model = activeProfile?.config?.model ?? getDefaultModel();
		const provider = activeProfile?.config?.provider ?? null;
		return provider ? encodeProviderModel(provider, model) : model;
	});
	const modelValue = $derived($activeSession.modelOverride ?? profileModel);

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
	function refreshUsageLimits(): Promise<void> {
		if (usageFetchInFlight) return usageFetchInFlight;
		usageFetchInFlight = getUsageLimits()
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

	// Poll computer-use status (shared store) only while the active profile
	// enables the capability; stop when it doesn't, to avoid idle polling.
	$effect(() => {
		if (!computerUseEnabled) return;
		computerUse.start();
		return () => computerUse.stop();
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

	// Desktop pill labels — the effective value's human label, with a neutral
	// fallback when nothing is set yet (no profile value + no override).
	const effortButtonLabel = $derived(
		effortLevels.find((m) => m.value === effortValue)?.label ?? 'Effort'
	);
	const permissionButtonLabel = $derived(
		permissionModes.find((m) => m.value === permissionValue)?.label ?? 'Access'
	);
</script>

<div class="controls-dock">
	<!-- Left: model / effort / permission pills, then the context ring -->
	<div class="controls-left">
		<!-- Model Override -->
		<OverrideDropdown
			modes={modelOptions}
			currentValue={modelValue}
			isOverridden={$activeSession.modelOverride !== null}
			isOpen={openDropdown === 'model'}
			onOpenChange={(open) => (openDropdown = open ? 'model' : null)}
			onSelect={(v) => activeSession.setModelOverride(v === profileModel ? null : v)}
			title="Model"
			buttonLabel={modelButtonLabel}
			buttonSubLabel={modelSubLabel}
			warn={rerouted}
			warnTitle={modelWarnTitle}
			notice={modelNotice}
		>
			{#snippet icon()}
				<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<rect x="4" y="4" width="16" height="16" rx="2" stroke-width="1.75" />
					<rect x="9" y="9" width="6" height="6" stroke-width="1.75" />
					<path stroke-linecap="round" stroke-width="1.75" d="M9 1.5v2.5M15 1.5v2.5M9 20v2.5M15 20v2.5M20 9h2.5M20 14h2.5M1.5 9H4M1.5 14H4" />
				</svg>
			{/snippet}
		</OverrideDropdown>

		<!-- Effort Level Override -->
		<OverrideDropdown
			modes={effortLevels}
			currentValue={effortValue}
			isOverridden={$activeSession.effortLevelOverride !== null}
			isOpen={openDropdown === 'effort'}
			onOpenChange={(open) => (openDropdown = open ? 'effort' : null)}
			onSelect={(v) => activeSession.setEffortLevelOverride(v === profileEffort ? null : v)}
			title="Effort Level"
			buttonLabel={effortButtonLabel}
		>
			{#snippet icon()}
				<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M13 10V3L4 14h7v7l9-11h-7z" />
				</svg>
			{/snippet}
		</OverrideDropdown>

		<!-- Permission Mode Override -->
		<OverrideDropdown
			modes={permissionModes}
			currentValue={permissionValue}
			isOverridden={$activeSession.permissionModeOverride !== null}
			isOpen={openDropdown === 'permission'}
			onOpenChange={(open) => (openDropdown = open ? 'permission' : null)}
			onSelect={(v) => activeSession.setPermissionModeOverride(v === profilePermissionMode ? null : v)}
			title="Permission Mode"
			buttonLabel={permissionButtonLabel}
		>
			{#snippet icon()}
				<svg class="w-[16px] h-[16px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.75" d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
				</svg>
			{/snippet}
		</OverrideDropdown>

		<!-- Context-usage ring (16px SVG arc + its popover) -->
		<ContextUsageBar />
	</div>

	<!-- Right: computer-use, attach, then the parent's action cluster -->
	<div class="controls-right">
		<!-- Computer Use toggle (only when the profile enables it). A single
		     monitor icon: lit = the agent may control the screen; dimmed =
		     paused. Clicking toggles control on/off. -->
		{#if computerUseEnabled}
			<button
				type="button"
				onclick={toggleComputerUse}
				disabled={cuBusy}
				class="dock-action-btn dock-action-btn--cu"
				class:cu-on={!computerUse.stopped}
				class:cu-active={computerUse.active && !computerUse.stopped}
				role="switch"
				aria-checked={!computerUse.stopped}
				title={computerUse.stopped
					? 'Computer control paused — click to allow the agent to control the screen'
					: computerUse.active
						? `Agent is controlling ${computerUse.displayLabel ?? 'your screen'} — click to pause`
						: 'Computer control enabled — click to pause'}
				aria-label={computerUse.stopped ? 'Enable computer control' : 'Pause computer control'}
			>
				<Monitor size={18} strokeWidth={1.75} />
			</button>
		{/if}

		<!-- Attach File Button -->
		<button
			type="button"
			onclick={onAttachClick}
			disabled={!$anyCredential || isUploading || isRemoteOnlyCompose($activeSession.project, $activeSession.projectV2Id)}
			class="dock-action-btn"
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

	/* `margin-left: auto` (not a second flex:1 half) so the pills on the left keep
	   their natural width and the action cluster hugs the right edge. */
	.controls-right {
		display: flex;
		align-items: center;
		gap: 2px;
		margin-left: auto;
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

	/* Computer-use toggle: dimmed when paused, lit (primary) when control is
	   allowed, and gently pulsing while the agent is actively driving a screen. */
	.dock-action-btn--cu {
		color: var(--muted-foreground);
	}

	.dock-action-btn--cu.cu-on {
		color: var(--primary);
	}

	.dock-action-btn--cu:hover:not(:disabled) {
		background: var(--scrim);
		color: var(--primary);
	}

	.dock-action-btn--cu.cu-active {
		color: var(--primary);
		animation: cu-pulse 1.4s ease-in-out infinite;
	}

	@keyframes cu-pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.45; }
	}
</style>
