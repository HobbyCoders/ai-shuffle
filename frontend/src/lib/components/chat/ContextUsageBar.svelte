<script lang="ts">
	/**
	 * ContextUsageBar — the single chat-toolbar usage pill. Merges the former
	 * ContextUsageBar (context-window %) and UsageLimitIndicator (plan usage
	 * limits) into ONE glass pill + ONE popover.
	 *
	 * The pill is a single circular progress ring (context-window fraction),
	 * coloured by the current chat's context severity. No inline text — the
	 * percentage rides on the button's aria-label. It is ALWAYS visible.
	 *
	 * The popover is a wide, compact panel. It stacks, top → bottom:
	 *   1. Context window — a collapsible header (synced per account, cached under
	 *      `aihub.contextBreakdownOpen`, default expanded) over ONE segmented bar.
	 *      Expanded: a contiguous segment per category (the bare track tail is the
	 *      free space), plus per-category rows (swatch + label + tokens + percent)
	 *      followed by a trailing, muted Model
	 *      row (no swatch, no percent — just the effective model label). Collapsed:
	 *      a single severity-coloured fill sized to the total percent — no
	 *      per-category colour breakdown. The breakdown is fetched from the SDK
	 *      (`/context-usage`) on session load and refreshed at turn end (never
	 *      polled mid-stream) so the pill ring can prefer it; `source: "persisted"`
	 *      (final snapshot from the session's last turn end) is treated as a valid
	 *      rich breakdown. The poller estimate is the fallback before the first
	 *      fetch lands — a fresh session shows em-dash rows.
	 *   2. Plan usage limits — the resolved install's rate-limit windows (from the
	 *      `rateLimit` store, resolved via `usage-target.ts`), each with a bar, %,
	 *      and reset text inline. "No usage data yet" when the bucket is empty.
	 *
	 * Usage comes from $activeSession (contextUsed + per-type token fields), all
	 * set by the SSE-fed reducer. Rate-limit windows come from the `rateLimit`
	 * store (fed by the backend poller + live SDK events), resolved to the install
	 * that OWNS the viewed/composed session — an offloaded session shows the
	 * worker's usage.
	 *
	 * Popover interaction: a wrapper with `bind:this`, a document-level click +
	 * Escape listener that closes when the target is outside — works for desktop
	 * click and mobile tap (tap-away closes).
	 */
	import { onMount, tick } from 'svelte';
	import { contextBreakdown, usageSections, hiddenUsageProviders } from '$lib/stores/expansion-preferences';
	import { EyeOff, Eye } from 'lucide-svelte';
	import { createUsageNumberAnimation } from './usage-number-animation';
	import { activeSession } from '$lib/stores/active-session';
	import { getContextMaxForModel } from '$lib/constants/chat';
	import {
		getDefaultModel,
		getModelLabel,
		decodeProviderModel,
		isPresetModel,
		getProviders,
		getProviderDisplay,
		getClaudeDisplay,
		getClaudeAuth,
		type ProviderDisplay
	} from '$lib/constants/models';
	import { getContextUsage, type SdkContextUsage } from '$lib/api/sessions';
	import {
		computeContextBreakdown,
		mapSdkContextUsage,
		decideBreakdownRefresh,
		formatTokenCount,
		usageSeverity,
		type ContextBreakdown,
		type BreakdownRefreshState,
		type UsageSeverity,
	} from './context-usage';
	import {
		rateLimit,
		displayWindows,
		windowLabel,
		effectivePct,
		reportedRemainingPct,
		remainingFromUsedPct,
		formatCountdown,
		resolveBucket,
		resolveBucketKey,
		selfBucketKey,
		type UsageBucket,
		type ProviderUsageBucket,
		type UsageWindow,
	} from '$lib/stores/rate-limit';
	import { usageTargetInstance } from '$lib/stores/usage-target';
	import { claudeAuthenticated } from '$lib/stores/auth.svelte';

	const usageNumber = createUsageNumberAnimation();

	interface Props {
		onOpenChange?: (open: boolean) => void;
	}

	let { onOpenChange }: Props = $props();

	// ── Effective model → context window (200k / 1M) ──────────────────────
	// The session's model pick may be a `prov:<providerId>:<modelId>` composite
	// (PA13's cross-provider switch) — decode it once and let BOTH the model id
	// and the provider id come from the pick, mirroring the backend's resolution.
	const overrideComposite = $derived(decodeProviderModel($activeSession.modelOverride));
	const effectiveModel = $derived.by(() => {
		if (overrideComposite) return overrideComposite.modelId;
		if ($activeSession.modelOverride) return $activeSession.modelOverride;
		return getDefaultModel();
	});
	// The label reads the RAW selection so a composite round-trips to its
	// provider-scoped label rather than a bare id.
	const effectiveModelLabel = $derived(
		getModelLabel($activeSession.modelOverride ?? effectiveModel)
	);
	// Provider id for the context-window lookup (null = native Claude), resolved
	// with the same precedence the backend uses: an explicit composite pick
	// wins; a plain native-preset pick CLEARS the binding for the turn; else
	// the session's live provider (an agent carries none to fall back to).
	const effectiveProviderId = $derived.by(() => {
		if (overrideComposite) return overrideComposite.providerId;
		if ($activeSession.modelOverride && isPresetModel($activeSession.modelOverride)) return null;
		return $activeSession.liveProvider ?? null;
	});
	const contextMax = $derived(getContextMaxForModel(effectiveModel, effectiveProviderId));

	// Context usage from activeSession — set by onPollApplied after each poll.
	const rawContextUsed = $derived($activeSession.contextUsed ?? 0);

	const breakdown = $derived(
		computeContextBreakdown({
			rawContextUsed,
			inputTokens: $activeSession.totalTokensIn ?? 0,
			outputTokens: $activeSession.totalTokensOut ?? 0,
			cacheCreationTokens: $activeSession.totalCacheCreationTokens ?? 0,
			cacheReadTokens: $activeSession.totalCacheReadTokens ?? 0,
			contextMax,
		})
	);

	// ── Popover open/close (click + tap-away) ─────────────────────────────
	let open = $state(false);
	let wrapperRef = $state<HTMLDivElement | null>(null);
	let popoverEl = $state<HTMLDivElement | null>(null);
	let popShift = $state(0);
	let popResizeObserver: ResizeObserver | null = null;

	function measurePopoverShift() {
		const el = popoverEl;
		if (!el || !open) {
			popShift = 0;
			return;
		}
		// The browser panel reduces the clipping chat column, not the viewport.
		const column = wrapperRef?.closest(".chat-view")?.getBoundingClientRect();
		const minLeft = Math.max(0, column?.left ?? 0) + 12;
		const maxRight = Math.min(window.innerWidth, column?.right ?? window.innerWidth) - 12;
		el.style.setProperty("--usage-popover-room", `${Math.max(0, maxRight - minLeft)}px`);
		const r = el.getBoundingClientRect();
		// Bounds already include the current translation; solve from the unshifted position.
		const left = r.left - popShift;
		const right = r.right - popShift;
		let shift = 0;
		if (right > maxRight) shift = maxRight - right;
		if (left + shift < minLeft) shift = minLeft - left;
		popShift = Math.round(shift);
	}

	function attachPopShiftListener() {
		detachPopShiftListener();
		window.addEventListener('resize', measurePopoverShift);
		if (typeof ResizeObserver === 'undefined') return;
		popResizeObserver = new ResizeObserver(measurePopoverShift);
		const column = wrapperRef?.closest('.chat-view');
		if (column) popResizeObserver.observe(column);
		if (popoverEl) popResizeObserver.observe(popoverEl);
	}

	function detachPopShiftListener() {
		window.removeEventListener('resize', measurePopoverShift);
		popResizeObserver?.disconnect();
		popResizeObserver = null;
	}

	function setOpen(next: boolean) {
		open = next;
		onOpenChange?.(next);
		if (next) {
			void tick().then(() => {
				if (!open) return;
				measurePopoverShift();
				attachPopShiftListener();
			});
		} else {
			detachPopShiftListener();
			popShift = 0;
		}
	}

	function toggle() {
		setOpen(!open);
	}

	// ── SDK/persisted breakdown = the single display source for the context section ─
	let richBreakdown = $state<ContextBreakdown | null>(null);
	const sessionId = $derived($activeSession.sessionId);
	const isStreaming = $derived($activeSession.isStreaming);
	const displayBreakdown = $derived<ContextBreakdown>(richBreakdown ?? breakdown);

	// The pill ring prefers the rich breakdown when loaded (its totalPercent is
	// the same figure the popover header shows — so they can't disagree),
	// falling back to the poller estimate before the first fetch lands.
	const contextPercent = $derived(displayBreakdown.totalPercent);

	// ── Ring geometry (mirrors TodoList's progress ring) ──────────────────
	const RING_RADIUS = 6;
	const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS;
	const ringDashoffset = $derived(
		RING_CIRCUMFERENCE - (Math.min(Math.max(contextPercent, 0), 100) / 100) * RING_CIRCUMFERENCE
	);

	// Segmented progress bar: one contiguous segment per row whose tokens are
	// actually part of totalUsed (row.inBar — excludes the grayed Free space row,
	// em-dash placeholders, and, on the estimate path, Output tokens, which is
	// session-cumulative and NOT part of context used). Remaining track = free
	// space (no segment).
	interface BarSegment {
		key: string;
		widthPct: number;
		color: string;
	}
	const barSegments = $derived.by<BarSegment[]>(() =>
		displayBreakdown.rows
			.filter((r) => r.inBar && r.tokens > 0)
			.map((r) => ({ key: r.key, widthPct: r.barPercent, color: r.color }))
	);

	let loadedSessionId: string | null = null;
	let currentController: AbortController | null = null;
	function cancelFetch() {
		currentController?.abort();
		currentController = null;
	}
	async function fetchBreakdown(sid: string) {
		cancelFetch();
		const controller = new AbortController();
		currentController = controller;
		try {
			const res = await getContextUsage(sid, { signal: controller.signal });
			if (controller.signal.aborted) return;
			if (res.available) {
				richBreakdown = mapSdkContextUsage(res.usage);
				loadedSessionId = sid;
			}
		} catch {
			// Aborted or network error → keep whatever is on screen.
		} finally {
			if (currentController === controller) currentController = null;
		}
	}

	// Refresh cadence: fetch on session load (even while closed, so the ring is
	// accurate), on popover open (if not already loaded), and at turn end; reset
	// synchronously on session change so no stale cross-session breakdown flashes.
	let prevRefresh: BreakdownRefreshState = { open: false, sessionId: null, isStreaming: false };
	$effect(() => {
		const next: BreakdownRefreshState = { open, sessionId, isStreaming };
		const action = decideBreakdownRefresh(prevRefresh, next);
		prevRefresh = next;

		switch (action) {
			case 'clear':
				cancelFetch();
				richBreakdown = null;
				loadedSessionId = null;
				break;
			case 'session-fetch':
				cancelFetch();
				richBreakdown = null;
				loadedSessionId = null;
				fetchBreakdown(next.sessionId!);
				break;
			case 'open-fetch':
				// Only fetch if nothing is loaded/in-flight for this session already.
				if (loadedSessionId !== next.sessionId && !currentController) {
					fetchBreakdown(next.sessionId!);
				}
				break;
			case 'refresh':
				fetchBreakdown(next.sessionId!);
				break;
			case 'idle':
				break;
		}
	});

	// ── Live per-call breakdown (pushed, not fetched) ─────────────────────
	// fluid reports a context snapshot with each model call's usage; the feed
	// carries it to `liveContextUsage`. Treat it exactly like a landed fetch so
	// the ring, header, and rows track a running turn instead of sitting stale
	// until it ends. The turn-end fetch still runs afterwards and replaces this
	// with the persisted final, which is authoritative.
	let appliedLiveSnapshot: SdkContextUsage | null = null;
	$effect(() => {
		const live = $activeSession.liveContextUsage;
		const sid = sessionId;
		if (!live || !sid) return;
		// Object identity is the change signal: the feed replaces the whole
		// snapshot per call, so re-mapping the same one would be wasted work.
		if (live === appliedLiveSnapshot) return;
		appliedLiveSnapshot = live;
		richBreakdown = mapSdkContextUsage(live);
		loadedSessionId = sid;
	});

	// Expansion choices are shared across mounted instances and account devices.
	const breakdownOpen = $derived($contextBreakdown);
	function toggleBreakdown() { contextBreakdown.set(!breakdownOpen); }
	function isSectionOpen(id: string): boolean { return $usageSections[id] !== false; }
	function toggleSection(id: string) {
		usageSections.set({ ...$usageSections, [id]: !isSectionOpen(id) });
	}

	// ── Plan usage limits (resolved install's rate-limit windows) ─────────
	// Claude subscription bars show the remaining quota reported by its usage feed.
	// Per-second tick keeps countdowns and rolled-over windows fresh.
	// Runs only while the popover is open; refreshed once otherwise.
	let now: number = $state(Date.now());
	$effect(() => {
		now = Date.now();
		if (!open) return;
		const interval = setInterval(() => {
			now = Date.now();
		}, 1000);
		return () => clearInterval(interval);
	});

	interface PercentDisplay {
		pct: number;
		severity: UsageSeverity;
	}

	interface WindowItem extends PercentDisplay {
		type: string;
		label: string;
		countdown: string;
		hasReset: boolean;
	}

	/** Mirror the used-quota thresholds against a remaining-quota value. */
	function remainingSeverity(percentRemaining: number): UsageSeverity {
		return usageSeverity(100 - percentRemaining);
	}

	function providerWindowPercent(
		usageWindow: UsageWindow,
		nowMs: number,
		showsRemaining: boolean
	): PercentDisplay {
		const pct = showsRemaining
			? remainingFromUsedPct(usageWindow, nowMs)
			: effectivePct(usageWindow, nowMs);
		return {
			pct,
			severity: showsRemaining ? remainingSeverity(pct) : usageSeverity(pct),
		};
	}

	function itemsFor(bucket: UsageBucket): WindowItem[] {
		return displayWindows(bucket.windows).map((usageWindow) => {
			const pct = reportedRemainingPct(usageWindow, now);
			return {
				pct,
				severity: remainingSeverity(pct),
				type: usageWindow.type,
				label: windowLabel(usageWindow.type),
				countdown: formatCountdown(usageWindow.resets_at, now),
				hasReset: usageWindow.resets_at != null,
			};
		});
	}

	// Provider credits (non-Claude): render a single muted line from the opaque
	// credits blob when there's no window denominator to draw a bar, or when the
	// provider reports meaningful credit balances. All reads are shape-guarded.
	/** A finite number from the untyped credits blob, else null. */
	function creditsNum(credits: Record<string, unknown> | null, key: string): number | null {
		const v = credits?.[key];
		return typeof v === 'number' && Number.isFinite(v) ? v : null;
	}

	function providerStatusText(pb: ProviderUsageBucket): string | null {
		if (pb.status !== 'stale' && pb.status !== 'unavailable' && pb.status !== 'never_fetched') return null;
		// Keep credential problems actionable without narrating background refreshes.
		const isOpenAI = pb.providerType?.toLowerCase() === 'openai';
		if (pb.lastErrorKind === 'billing_access_denied') {
			return isOpenAI ? 'This credential cannot read OpenAI Platform billing' : 'Billing access required';
		}
		if (pb.lastErrorKind === 'missing_credentials') {
			return isOpenAI ? 'OpenAI API credential required' : 'Sign in to view usage';
		}
		return null;
	}

	function creditsSummary(
		credits: Record<string, unknown> | null,
		providerType: string | null = null
	): string | null {
		if (!credits || typeof credits !== 'object') return null;
		// An account credit BALANCE (OpenRouter's /credits ledger) is the headline
		// figure and outranks the key-scoped usage/limit fields below it.
		const remaining = creditsNum(credits, 'balance_remaining');
		if (remaining != null) {
			const spent = creditsNum(credits, 'total_usage');
			return spent != null
				? `$${remaining.toFixed(2)} left · $${spent.toFixed(2)} used`
				: `$${remaining.toFixed(2)} left`;
		}
		// ChatGPT/Codex extra-credit headers are not prepaid Platform balances.
		// Keep subscription quotas separate from configured OpenAI API rows;
		// show an API balance only when billing actually reports one.
		if ((providerType || '').toLowerCase() === 'chatgpt') return null;
		if (credits.unlimited === true) return 'Credits: unlimited';
		const used = typeof credits.usage === 'number' ? credits.usage : null;
		const left = typeof credits.limit_remaining === 'number' ? credits.limit_remaining : null;
		if (used != null && left != null) return `$${used.toFixed(2)} used · $${left.toFixed(2)} left`;
		if (used != null) return `$${used.toFixed(2)} used`;
		if (left != null) return `$${left.toFixed(2)} left`;
		// Codex credits shape carries a plain balance instead of usage/limit.
		const balance = typeof credits.balance === 'number' ? credits.balance : null;
		if (balance != null) return `Credits: $${balance.toFixed(2)}`;
		return null;
	}

	// Compact one-value credits chip for a collapsed provider header — shown
	// whenever the account balance is known, and otherwise as the fallback when
	// the provider has no rate-limit windows to draw a pct from. Same field
	// semantics as creditsSummary: the account balance first, then usage,
	// key-limit remaining, and a plain balance.
	function creditsShort(
		credits: Record<string, unknown> | null,
		providerType: string | null = null
	): string | null {
		if (!credits || typeof credits !== 'object') return null;
		const remaining = creditsNum(credits, 'balance_remaining');
		if (remaining != null) return `$${remaining.toFixed(2)} left`;
		if ((providerType || '').toLowerCase() === 'chatgpt') return null;
		const used = typeof credits.usage === 'number' ? credits.usage : null;
		if (used != null) return `$${used.toFixed(2)} used`;
		const left = typeof credits.limit_remaining === 'number' ? credits.limit_remaining : null;
		if (left != null) return `$${left.toFixed(2)} left`;
		const balance = typeof credits.balance === 'number' ? credits.balance : null;
		if (balance != null) return `$${balance.toFixed(2)}`;
		return null;
	}

	const target = $derived($usageTargetInstance);
	const resolvedBucket = $derived(resolveBucket($rateLimit, target));
	const resolvedKey = $derived(resolveBucketKey($rateLimit, target));
	const items = $derived.by<WindowItem[]>(() => (resolvedBucket ? itemsFor(resolvedBucket) : []));

	const ringSeverity = $derived<UsageSeverity>(usageSeverity(contextPercent));

	// ── Resolved-install header (brand + auth-kind subtext) ───────────────
	// Every section header in this popover reads BRAND NAME + a subtext saying
	// how that account authenticates, from the shared naming rules in
	// `constants/models` — so the popover, the composer's model dropdown and the
	// agents-UI picker can never drift apart.
	//
	// Claude: "Claude" + its subscription plan ("max"/"pro"), or "API" on
	// API-key auth. Two sources feed that, in this order:
	//
	//   1. the RESOLVED INSTALL's own usage snapshot (`planType`, joined from
	//      that install's `claude_accounts` row) — authoritative for any
	//      install, including a remote worker on a different account;
	//   2. the local registry's `claude` block — it describes THIS install only,
	//      so it is consulted just for the self install (it is also the only
	//      carrier of API-key auth, which snapshots never report).
	//
	// With neither known the subtext falls back to the resolved install's device
	// name / "Other device" hint rather than dropping that disambiguation.
	const isSelf = $derived(target == null || target === $rateLimit.selfInstanceId ||
		(resolvedKey != null && resolvedKey === selfBucketKey($rateLimit)));
	// Empty usage buckets also exist on installs without Claude. Subscription
	// display metadata defaults to "subscription", so it is not proof of an account.
	const hasClaude = $derived(Boolean(
		resolvedBucket?.available || resolvedBucket?.accountId || resolvedBucket?.accountEmail ||
		(isSelf && ($claudeAuthenticated || getClaudeAuth().auth_kind === 'api'))
	));
	const deviceLabel = $derived(resolvedBucket?.deviceName ?? null);
	const claudeDisplay = $derived<ProviderDisplay>(
		getClaudeDisplay({
			planType: resolvedBucket?.planType ?? (isSelf ? getClaudeAuth().plan_type : null),
			authKind: isSelf ? getClaudeAuth().auth_kind : null
		})
	);
	const claudeSubtext = $derived(
		claudeDisplay.subtext ?? deviceLabel ?? (isSelf ? null : 'Other device')
	);

	// Provider sections: prefer the hydrated registry row (its `auth_kind` is
	// derived from the row's auth_mode, so a `custom` row on Codex auth is
	// classified correctly), falling back to the usage bucket's own fields when
	// the registry hasn't hydrated.
	function providerDisplayFor(pb: {
		providerId: string;
		providerName: string | null;
		providerType: string | null;
		planType: string | null;
	}): ProviderDisplay {
		const entry = getProviders().find((p) => p.id === pb.providerId);
		return getProviderDisplay({
			name: entry?.name ?? pb.providerName ?? pb.providerType ?? 'Provider',
			provider_type: entry?.provider_type ?? pb.providerType,
			auth_kind: entry?.auth_kind ?? null,
			plan_type: entry?.plan_type ?? pb.planType
		});
	}

	// Subscriptions (ChatGPT / SuperGrok) first, A–Z, then API accounts
	// (OpenAI API, OpenRouter, fal.ai, …) A–Z. Brand-only alpha parked
	// OpenAI · API next to OpenAI · prolite, which read as one section.
	function isSubscriptionProvider(pb: { providerType: string | null }): boolean {
		const t = (pb.providerType || '').toLowerCase();
		return t === 'chatgpt' || t === 'grok';
	}

	function sortedProviderBuckets() {
		return Object.values($rateLimit.providerBuckets ?? {})
			.map((pb) => ({
				pb,
				name: providerDisplayFor(pb).name,
				group: isSubscriptionProvider(pb) ? 0 : 1
			}))
			.sort((a, b) => a.group - b.group || a.name.localeCompare(b.name))
			.map((entry) => entry.pb);
	}

	/**
	 * Claude, ChatGPT and SuperGrok subscription sections present quota remaining.
	 * SuperGrok billing reports used-%, which `remainingFromUsedPct` converts — so
	 * grok rows and their collapsed summary render "% left" too. The remaining
	 * provider feeds keep their used-quota convention: xAI API-key rows
	 * (`provider_type === 'xai'`), other API-key headers, and OpenRouter credit
	 * limits all expose utilization directly.
	 */
	function providerShowsRemaining(pb: {
		providerId: string;
		providerType: string | null;
	}): boolean {
		const entry = getProviders().find((p) => p.id === pb.providerId);
		const type = entry?.provider_type ?? pb.providerType ?? '';
		if (type === 'grok') return true;
		if (type !== 'chatgpt') return false;
		const kind = entry?.auth_kind ?? 'subscription';
		return kind === 'subscription';
	}

	function providerWindowSummary(
		windows: UsageWindow[],
		nowMs: number,
		showsRemaining: boolean
	): PercentDisplay | null {
		const values = windows.map((usageWindow) =>
			providerWindowPercent(usageWindow, nowMs, showsRemaining)
		);
		if (values.length === 0) return null;
		// The constrained subscription window is the one with the LEAST quota
		// remaining. Used-quota providers retain the previous highest-usage summary.
		return values.reduce((summary, value) => {
			const isMoreConstrained = showsRemaining
				? value.pct < summary.pct
				: value.pct > summary.pct;
			return isMoreConstrained ? value : summary;
		});
	}

	const providerRows = $derived(sortedProviderBuckets());
	const hiddenRows = $derived([
		...(hasClaude ? [{ id: 'plan', name: claudeDisplay.name }] : []),
		...providerRows.map(pb => ({ id: `provider:${pb.providerId}`, name: providerDisplayFor(pb).name }))
	].filter(row => $hiddenUsageProviders[row.id]));

	function setProviderHidden(id: string, hidden: boolean) {
		hiddenUsageProviders.update(current => {
			const next = { ...current };
			if (hidden) next[id] = true;
			else delete next[id];
			return next;
		});
	}

	// Claude plan-usage collapse: the rows follow the 'plan' section toggle.
	const planRowsVisible = $derived(isSectionOpen('plan'));
	// The collapsed Claude summary surfaces the most constrained window: the
	// smallest remaining percentage (null → omit for an empty bucket).
	const planMinRemaining = $derived<number | null>(
		items.length ? Math.min(...items.map((it) => it.pct)) : null
	);

	// ── Document-level click/tap-away + Escape ────────────────────────────
	function handleClickOutside(e: MouseEvent) {
		// Membership via composedPath(), NOT wrapperRef.contains(e.target): Svelte 5
		// flushes DOM updates synchronously after a click handler, so a section
		// toggle can detach the clicked node (e.g. the collapsed summary chip inside
		// `{#if !pbOpen}`) before this document-level listener runs — `contains` then
		// reads the click as outside. `composedPath()` is frozen at dispatch time.
		if (open && wrapperRef && !e.composedPath().includes(wrapperRef)) {
			setOpen(false);
		}
	}
	function handleEscape(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) setOpen(false);
	}

	onMount(() => {
		document.addEventListener('click', handleClickOutside);
		document.addEventListener('keydown', handleEscape);
		return () => {
			document.removeEventListener('click', handleClickOutside);
			document.removeEventListener('keydown', handleEscape);
			detachPopShiftListener();
			cancelFetch();
		};
	});
</script>

{#snippet hideProvider(id: string, name: string)}
	<button
		type="button"
		class="provider-hide"
		title="Hide {name} usage"
		aria-label="Hide {name} usage"
		onclick={() => setProviderHidden(id, true)}
	>
		<EyeOff size={13} aria-hidden="true" />
	</button>
{/snippet}

<div class="usage-context-wrapper" bind:this={wrapperRef}>
	<button
		type="button"
		class="usage-indicator"
		title="{formatTokenCount(displayBreakdown.totalUsed)} / {formatTokenCount(
			displayBreakdown.contextMax
		)} tokens — click for details"
		aria-label="Context and usage — {Math.round(contextPercent)}% of context window used"
		aria-haspopup="dialog"
		aria-expanded={open}
		onclick={toggle}
	>
		<svg class="usage-ring {ringSeverity}" viewBox="0 0 16 16" aria-hidden="true">
			<circle class="ring-track" cx="8" cy="8" r={RING_RADIUS} fill="none" stroke-width="2.4" />
			<circle
				class="ring-progress"
				cx="8"
				cy="8"
				r={RING_RADIUS}
				fill="none"
				stroke-width="2.4"
				stroke-linecap="round"
				stroke-dasharray={RING_CIRCUMFERENCE}
				stroke-dashoffset={ringDashoffset}
			/>
		</svg>
	</button>

	{#if open}
		<div class="usage-popover" bind:this={popoverEl} style:transform="translateX({popShift}px)" role="dialog" aria-label="Context and usage">
			<!-- ═══ Context window ═══ -->
			<div class="ctx-section">
				<button
					type="button"
					class="ctx-header"
					aria-expanded={breakdownOpen}
					aria-label="Context window breakdown — {breakdownOpen ? 'collapse' : 'expand'}"
					onclick={toggleBreakdown}
				>
					<span class="row-label">Context window</span>
					<span class="ctx-value">
						{formatTokenCount(displayBreakdown.totalUsed)} / {formatTokenCount(
							displayBreakdown.contextMax
						)}
						<span class="ctx-pct {displayBreakdown.severity}"
							>({Math.round(displayBreakdown.totalPercent)}%)</span
						>
					</span>
					<svg
						class="ctx-chevron {breakdownOpen ? '' : 'collapsed'}"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						aria-hidden="true"
					>
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
					</svg>
				</button>

				<!-- Segmented bar (always shown). Expanded: one segment per category;
				     the bare track tail is the free space. Collapsed: a single
				     severity-coloured fill, same total width as the header %. -->
				<div class="ctx-segbar" aria-hidden="true">
					{#if breakdownOpen}
						{#each barSegments as seg (seg.key)}
							<span
								class="ctx-seg"
								style="width: {seg.widthPct}%; background: {seg.color}"
							></span>
						{/each}
					{:else}
						<span
							class="ctx-seg solid {displayBreakdown.severity}"
							style="width: {displayBreakdown.totalPercent}%"
						></span>
					{/if}
				</div>

				{#if breakdownOpen}
					{#each displayBreakdown.rows as row (row.key)}
						<div class="breakdown-row {row.muted ? 'muted' : ''}">
							<span
								class="cat-swatch {row.empty ? 'neutral' : ''}"
								style="background: {row.color}"
							></span>
							<span class="breakdown-label">
								{row.label}{#if row.subline}<span class="row-sub"> · {row.subline}</span>{/if}
							</span>
							<span class="breakdown-value">
								{#if row.empty}
									—
								{:else}
									{formatTokenCount(row.tokens)}
									{#if row.showPercent}
										<span class="cat-pct">{row.barPercent.toFixed(1)}%</span>
									{:else}
										<span class="cat-pct">—</span>
									{/if}
								{/if}
							</span>
						</div>
					{/each}
					<div class="breakdown-row muted model-row">
						<span class="breakdown-label">Model</span>
						<span class="breakdown-value">{effectiveModelLabel}</span>
					</div>
				{/if}
			</div>

			{#if hasClaude || Object.keys($rateLimit.providerBuckets ?? {}).length > 0}
				<div class="popover-divider"></div>
			{/if}

			<!-- ═══ Plan usage limits (Claude) ═══ -->
			{#if hasClaude && !$hiddenUsageProviders.plan}
				<!-- The configured account's header shows the "Claude" brand +
				     its auth-kind subtext (plan / "API"), falling back to the resolved
				     device name or "Other device". It is the collapse toggle for the window rows;
				     collapsed shows the least quota remaining and hides the rows +
				     "No usage data" line. No account naming, no "as of" staleness line. -->
				<div class="popover-section">
					<div class="provider-header">
						<button
							type="button"
							class="sec-header"
							aria-expanded={planRowsVisible}
							aria-label="Plan usage limits — {planRowsVisible ? 'collapse' : 'expand'}"
							onclick={() => toggleSection('plan')}
						>
							<span class="sec-head-labels">
								<span class="section-device">{claudeDisplay.name}</span>
								{#if claudeSubtext}
									<span class="section-account">{claudeSubtext}</span>
								{/if}
							</span>
							{#if !planRowsVisible && planMinRemaining != null}
								<span class="row-pct {remainingSeverity(planMinRemaining)} sec-summary"
									use:usageNumber={{ key: `plan:${resolvedKey}:summary`, text: `${planMinRemaining}% left` }}></span
								>
							{/if}
							<svg
								class="ctx-chevron {planRowsVisible ? '' : 'collapsed'}"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								aria-hidden="true"
							>
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
							</svg>
						</button>
						{@render hideProvider('plan', claudeDisplay.name)}
					</div>
				</div>
				{#if planRowsVisible}
					{#if items.length > 0}
						{#each items as item (item.type)}
							<div class="popover-row">
								<div class="row-line">
									<span class="row-label">{item.label}</span>
									<span class="row-value">
										<span class="row-reset-inline">
											{#if item.countdown}
												resets in {item.countdown}
											{:else if item.hasReset}
												resets soon
											{:else}
												no reset window
											{/if}
										</span>
										<span class="row-pct {item.severity}" use:usageNumber={{ key: `plan:${resolvedKey}:${item.type}`, text: `${item.pct}% left` }}></span>
									</span>
								</div>
								<div class="row-bar">
									<div class="row-fill {item.severity}" style="width: {item.pct}%"></div>
								</div>
							</div>
						{/each}
					{:else}
						<div class="row-reset no-data">No usage data yet</div>
					{/if}
				{/if}
			{/if}

			<!-- ═══ Provider usage (non-Claude) ═══ -->
			<!-- Each bucket's head is its collapse toggle (name + planType stay).
			     ChatGPT/Codex and SuperGrok subscription windows show quota remaining;
			     their collapsed summary uses the least remaining window. xAI API-key,
			     other API-key and credit windows keep used-of-100 and summarize the
			     highest-utilization window. -->
			{#each providerRows.filter(pb => !$hiddenUsageProviders[`provider:${pb.providerId}`]) as pb (pb.providerId)}
				{@const sectionId = `provider:${pb.providerId}`}
				{@const pbDisplay = providerDisplayFor(pb)}
				{@const pbOpen = isSectionOpen(sectionId)}
				{@const pbWindows = displayWindows(pb.windows)}
				{@const pbShowsRemaining = providerShowsRemaining(pb)}
				{@const pbSummary = providerWindowSummary(pbWindows, now, pbShowsRemaining)}
				{@const pbStatus = providerStatusText(pb)}
				<div class="popover-section">
					<div class="provider-header">
						<button
							type="button"
							class="sec-header"
							aria-expanded={pbOpen}
							aria-label="{pbDisplay.name} usage — {pbOpen ? 'collapse' : 'expand'}"
							onclick={() => toggleSection(sectionId)}
						>
							<span class="sec-head-labels">
								<span class="section-device">{pbDisplay.name}</span>
								{#if pbStatus}<span class="section-account provider-status">{pbStatus}</span>{/if}
								{#if pbDisplay.subtext}
									<span class="section-account">{pbDisplay.subtext}</span>
								{/if}
							</span>
							{#if !pbOpen}
								{@const pbCreditsShort = creditsShort(pb.credits, pb.providerType)}
								<!-- An account credit balance is the headline the user cares
								     about, so it outranks the window pct (which an OpenRouter
								     key-spend limit would otherwise produce). -->
								{#if creditsNum(pb.credits, 'balance_remaining') != null && pbCreditsShort}
									<span class="sec-summary sec-credits" use:usageNumber={{ key: `${sectionId}:credits-short`, text: pbCreditsShort }}></span>
								{:else if pbSummary}
									<span class="row-pct {pbSummary.severity} sec-summary"
										use:usageNumber={{ key: `${sectionId}:summary`, text: `${pbSummary.pct}%${pbShowsRemaining ? ' left' : ''}` }}></span
									>
								{:else if pbCreditsShort}
									<span class="sec-summary sec-credits" use:usageNumber={{ key: `${sectionId}:credits-short`, text: pbCreditsShort }}></span>
								{/if}
							{/if}
							<svg
								class="ctx-chevron {pbOpen ? '' : 'collapsed'}"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								aria-hidden="true"
							>
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
							</svg>
						</button>
						{@render hideProvider(sectionId, pbDisplay.name)}
					</div>
					{#if pbStatus && pb.providerType?.toLowerCase() === 'openai' && pb.lastErrorKind === 'billing_access_denied'}
						<a
							class="provider-billing-link"
							href="https://platform.openai.com/settings/organization/billing/overview"
							target="_blank"
							rel="noopener noreferrer"
						>View balance in OpenAI Platform billing</a>
					{/if}
				</div>
				{#if pbOpen}
					{#each pbWindows as w (w.type)}
						{@const display = providerWindowPercent(w, now, pbShowsRemaining)}
						{@const countdown = formatCountdown(w.resets_at, now)}
						<div class="popover-row">
							<div class="row-line">
								<span class="row-label">{windowLabel(w.type)}</span>
								<span class="row-value">
									<span class="row-reset-inline">
										{#if countdown}
											resets in {countdown}
										{:else if w.resets_at != null}
											resets soon
										{:else}
											no reset window
										{/if}
									</span>
									<span class="row-pct {display.severity}"
										use:usageNumber={{ key: `${sectionId}:${w.type}`, text: `${display.pct}%${pbShowsRemaining ? ' left' : ''}` }}></span
									>
								</span>
							</div>
							<div class="row-bar">
								<div class="row-fill {display.severity}" style="width: {display.pct}%"></div>
							</div>
						</div>
					{/each}
					{@const credits = creditsSummary(pb.credits, pb.providerType)}
					{#if credits}
						<div class="row-reset usage-credits" use:usageNumber={{ key: `${sectionId}:credits`, text: credits }}></div>
					{/if}
				{/if}
			{/each}
			{#if hiddenRows.length > 0}
				<details class="hidden-providers">
					<summary>Hidden providers ({hiddenRows.length})</summary>
					{#each hiddenRows as row (row.id)}
						<button type="button" class="provider-restore" onclick={() => setProviderHidden(row.id, false)}
							aria-label="Show {row.name} usage" title="Show {row.name} usage">
							<Eye size={13} aria-hidden="true" />
							<span>{row.name}</span>
						</button>
					{/each}
				</details>
			{/if}
		</div>
	{/if}
</div>

<style>
	.usage-context-wrapper {
		position: relative;
		display: inline-flex;
	}

	/* ===== USAGE INDICATOR — the mockup's bare `.ctxring` =====
	   It sits inside the composer island's glass foot row, so it carries no
	   surface of its own: just the ring, on a hit target big enough to click.
	   Rendered as a button (clickable), so reset the button chrome. */
	.usage-indicator {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 26px;
		height: 26px;
		margin-left: 2px;
		padding: 0;
		background: transparent;
		border: none;
		border-radius: 50%;
		font: inherit;
		cursor: pointer;
		transition: background-color 0.13s ease;
	}
	.usage-indicator:hover {
		background: var(--scrim);
	}
	.usage-indicator:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* ===== Circular progress ring (context-window fraction) ===== */
	.usage-ring {
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		transform: rotate(-90deg);
		transform-origin: center;
	}
	.ring-track {
		stroke: var(--border-strong);
	}
	.ring-progress {
		stroke: var(--primary);
		transition: stroke-dashoffset 0.3s ease, stroke 0.2s ease;
	}
	.usage-ring.warning .ring-progress {
		stroke: var(--warning);
	}
	.usage-ring.danger .ring-progress {
		stroke: var(--destructive);
	}

	/* Opens upward and inward from the rightmost control. Clamp both width
	   limits to the visible chat column: min-width otherwise beats max-width. */
	.usage-popover {
		position: absolute;
		bottom: calc(100% + 6px);
		right: 0;
		z-index: 50;
		box-sizing: border-box;
		/* Position corrections must be immediate, not globally animated. */
		transition: none;
		width: max-content;
		min-width: min(340px, var(--usage-popover-room, calc(100vw - 24px)));
		max-width: min(380px, var(--usage-popover-room, calc(100vw - 24px)));
		padding: 12px 14px;
		display: flex;
		flex-direction: column;
		gap: 10px;
		background: var(--popover, var(--card, var(--background)));
		border: 1px solid var(--glass-border, var(--border));
		border-radius: calc(var(--radius, 0.75rem) * 0.66);
		box-shadow: 0 8px 24px rgb(0 0 0 / 0.28);
	}

	/* ===== Context section ===== */
	.ctx-section {
		display: flex;
		flex-direction: column;
		gap: 7px;
	}

	/* Collapsible header — full-width button: label left, value + chevron right. */
	.ctx-header {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 10px;
		width: 100%;
		padding: 0;
		background: none;
		border: none;
		font: inherit;
		text-align: left;
		color: inherit;
		cursor: pointer;
	}
	.ctx-header:focus-visible {
		outline: 2px solid var(--ring, var(--primary));
		outline-offset: 2px;
		border-radius: 3px;
	}
	.ctx-chevron {
		width: 13px;
		height: 13px;
		flex-shrink: 0;
		align-self: center;
		color: var(--muted-foreground);
		transition: transform 200ms ease;
	}
	.ctx-chevron.collapsed {
		transform: rotate(-90deg);
	}

	/* One-line row: label left, value right — shared by the plan-usage data rows. */
	.row-line {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 12px;
	}

	.ctx-value {
		font-family: var(--font-mono);
		font-size: 11px;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.ctx-pct {
		font-weight: 600;
		color: var(--foreground);
	}
	.ctx-pct.warning {
		color: var(--warning);
	}
	.ctx-pct.danger {
		color: var(--destructive);
	}

	/* Single segmented progress bar — contiguous category segments; the
	   remaining track is the free space. Same track colour as the pill ring. */
	.ctx-segbar {
		display: flex;
		width: 100%;
		height: 6px;
		border-radius: 999px;
		overflow: hidden;
		background: color-mix(in oklch, var(--foreground) 12%, var(--background));
	}
	.ctx-seg {
		height: 100%;
		flex-shrink: 0;
		min-width: 0;
		transition: width 0.3s ease;
	}
	/* Collapsed state: one plain severity-coloured fill (same vars as the ring/pct). */
	.ctx-seg.solid {
		background: var(--primary);
	}
	.ctx-seg.solid.warning {
		background: var(--warning);
	}
	.ctx-seg.solid.danger {
		background: var(--destructive);
	}

	/* Per-category breakdown rows — one line: swatch + label (left) + value (right). */
	.breakdown-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}
	.cat-swatch {
		width: 8px;
		height: 8px;
		flex-shrink: 0;
		border-radius: 2px;
	}
	.cat-swatch.neutral {
		border: 1px solid color-mix(in oklch, var(--foreground) 20%, var(--background));
	}
	.breakdown-label {
		flex: 1 1 auto;
		min-width: 0;
		font-size: 11px;
		color: var(--muted-foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.row-sub {
		font-size: 10px;
		opacity: 0.65;
	}
	.breakdown-value {
		flex-shrink: 0;
		font-family: var(--font-mono);
		font-size: 10.5px;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.cat-pct {
		color: var(--foreground);
		font-weight: 600;
	}
	/* Grayed "Free space" row — de-emphasised label, value, and percent. */
	.breakdown-row.muted .breakdown-label,
	.breakdown-row.muted .breakdown-value,
	.breakdown-row.muted .cat-pct {
		color: var(--muted-foreground);
		font-weight: 400;
		opacity: 0.75;
	}
	/* Model row — trailing info line in the expanded breakdown, set off from the
	   category/Free-space rows above it with a hairline separator. */
	.model-row {
		margin-top: 2px;
		padding-top: 6px;
		border-top: 1px dashed var(--glass-border, var(--border));
	}

	.popover-divider {
		height: 1px;
		background: var(--glass-border, var(--border));
		margin: 0 -10px;
	}

	.popover-row {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.row-label {
		font-size: 12px;
		color: var(--foreground);
		white-space: nowrap;
	}

	/* Usage-row value: reset text (muted) + percentage (emphasized), inline right. */
	.row-value {
		display: inline-flex;
		align-items: baseline;
		gap: 8px;
		white-space: nowrap;
	}
	.row-reset-inline {
		font-size: 10.5px;
		color: var(--muted-foreground);
	}

	.row-pct {
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 600;
		color: var(--foreground);
		font-variant-numeric: tabular-nums;
	}
	.row-pct.warning {
		color: var(--warning);
	}
	.row-pct.danger {
		color: var(--destructive);
	}

	/* Thin full-width track — same foreground-mix as the pill ring so unfilled
	   bars stay visible in both themes. */
	.row-bar {
		width: 100%;
		height: 3px;
		background: color-mix(in oklch, var(--foreground) 12%, var(--background));
		border-radius: 999px;
		overflow: hidden;
	}
	.row-fill {
		height: 100%;
		background: var(--primary);
		border-radius: 999px;
		transition: width 0.3s ease;
	}
	.row-fill.warning {
		background: var(--warning);
	}
	.row-fill.danger {
		background: var(--destructive);
	}

	.row-reset {
		font-size: 10.5px;
		color: var(--muted-foreground);
	}
	.row-reset.no-data {
		font-style: italic;
		opacity: 0.8;
	}

	/* Resolved-install header — device/account label (shown when named or remote). */
	.popover-section {
		display: flex;
		flex-direction: column;
		gap: 2px;
	}
	.section-head {
		display: flex;
		align-items: baseline;
		gap: 6px;
		flex-wrap: wrap;
	}

	.provider-header {
		display: flex;
		align-items: center;
		gap: 6px;
		border-radius: 6px;
		transition: background-color 160ms ease, box-shadow 160ms ease;
	}
	.provider-header:focus-within {
		background: var(--accent);
		box-shadow: 0 0 0 4px var(--accent);
	}
	.provider-header .sec-header {
		flex: 1;
		min-width: 0;
	}
	.provider-hide, .provider-restore {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 4px;
		border: none;
		border-radius: 4px;
		background: none;
		color: var(--muted-foreground);
		font: inherit;
		cursor: pointer;
	}
	.provider-hide {
		flex-shrink: 0;
		transition: opacity 160ms ease, background-color 160ms ease, color 160ms ease;
	}
	.provider-header .section-device { transition: transform 160ms ease; }
	/* Opacity keeps the button's hit area, tab stop and layout slot intact. */
	@media (min-width: 769px) and (hover: hover) and (pointer: fine) {
		.provider-hide { opacity: 0; }
		.provider-header:hover .provider-hide,
		.provider-header:focus-within .provider-hide { opacity: 1; }
		.provider-header:hover {
			background: var(--accent);
			box-shadow: 0 0 0 4px var(--accent);
		}
		.provider-header:hover .section-device { transform: translateX(2px); }
	}
	@media (prefers-reduced-motion: reduce) {
		.provider-header, .provider-hide, .provider-header .section-device, .row-fill {
			transition: none;
		}
		.provider-header:hover .section-device { transform: none; }
	}
	.usage-credits { font-variant-numeric: tabular-nums; }
	.provider-hide:hover, .provider-restore:hover {
		background: var(--accent);
		color: var(--foreground);
	}
	.provider-hide:focus-visible, .provider-restore:focus-visible, .hidden-providers summary:focus-visible {
		outline: 2px solid var(--ring, var(--primary));
		outline-offset: 2px;
	}
	.hidden-providers {
		font-size: 10.5px;
		color: var(--muted-foreground);
	}
	.hidden-providers summary { cursor: pointer; }
	.provider-restore { display: flex; margin-top: 4px; }

	/* Collapsible account/provider section header — a full-width button mirroring
	   .ctx-header: label group left, optional max-pct summary + chevron right. */
	.sec-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		width: 100%;
		padding: 0;
		background: none;
		border: none;
		font: inherit;
		text-align: left;
		color: inherit;
		cursor: pointer;
	}
	/* Guidance is prose, not a single-line account label. Allow it to shrink
	   beside collapsed balances and wrap inside the viewport-clamped panel. */
	.provider-status,
	.provider-billing-link {
		min-width: 0;
		max-width: 100%;
		white-space: normal;
		overflow-wrap: anywhere;
	}
	.provider-billing-link {
		align-self: flex-start;
		font-size: 10.5px;
		color: var(--muted-foreground);
		text-decoration: underline;
		text-underline-offset: 2px;
	}
	.provider-billing-link:focus-visible,
	.sec-header:focus-visible {
		outline: 2px solid var(--ring, var(--primary));
		outline-offset: 2px;
		border-radius: 3px;
	}
	/* Device/account (or provider name/planType) labels — wrap as the original
	   .section-head did; the stale line rides along and wraps to a new line. */
	.sec-head-labels {
		display: flex;
		align-items: baseline;
		gap: 6px;
		flex-wrap: wrap;
		flex: 1 1 auto;
		min-width: 0;
	}
	/* Collapsed-header summary — same look as a plan-usage row percent. */
	.sec-summary {
		flex-shrink: 0;
	}
	/* Credits fallback summary — dollars carry no severity, so stay neutral. */
	.sec-credits {
		font-family: var(--font-mono);
		font-size: 11px;
		font-weight: 600;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
		white-space: nowrap;
	}
	.section-device {
		font-size: 11px;
		font-weight: 600;
		color: var(--foreground);
	}
	.section-account {
		font-size: 10.5px;
		color: var(--muted-foreground);
	}
</style>
