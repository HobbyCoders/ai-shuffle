<script lang="ts">
	/**
	 * ContextUsageBar — the single chat-toolbar usage pill. Shows the
	 * context-window % and the plan usage limits in ONE glass pill + ONE
	 * popover.
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
	 *   2. Usage sections — Claude's plan limits (the resolved install's windows
	 *      from the `rateLimit` store, via `usage-target.ts`) and each provider's
	 *      usage, in one order (subscriptions first, then the Settings order).
	 *      Every section has a FIXED skeleton whatever its state (fresh, stale,
	 *      refreshing, unavailable, error, nothing reported yet): its header, its
	 *      baseline window rows (placeholders until reported), a credits line for
	 *      credit-billed providers, and one meta line ("Checked 4m ago" or the
	 *      action to take, plus the dashboard link). A state change swaps text in
	 *      place; it never adds or removes a line. Figures stay those of the last
	 *      report.
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
	import { onMount, tick, untrack, type Snippet } from 'svelte';
	import { contextBreakdown, usageSections, hiddenUsageProviders } from '$lib/stores/expansion-preferences';
	import { EyeOff, Eye, ExternalLink } from 'lucide-svelte';
	import { AGENT_PROVIDER_ORDER } from '$lib/components/settings/providers/providers-model';
	import { createUsageNumberAnimation } from './usage-number-animation';
	import { creditNumber as creditsNum, creditText } from './provider-credit';
	import { activeSession } from '$lib/stores/active-session';
	import { getContextMaxForModel } from '$lib/constants/chat';
	import { contextWindowForChoice, isContextWindowChoice } from '$lib/constants/model-context';
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
		resolveContextWindow,
		fitBreakdownToWindow,
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
		formatAsOf,
		bucketObservedAt,
		bucketHasUndatedWindow,
		bucketReporters,
		resolveBucket,
		resolveBucketKey,
		selfBucketKey,
		isSelfTarget,
		windowRolledOver,
		claudeUsageSource,
		type UsageBucket,
		type ProviderUsageBucket,
		type UsageWindow,
	} from '$lib/stores/rate-limit';
	import { usageTargetInstance, usageTargetDeviceName } from '$lib/stores/usage-target';
	import { claudeAuthenticated } from '$lib/stores/auth.svelte';

	const usageNumber = createUsageNumberAnimation();

	/** The user's own usage page; AI Shuffle never fetches it. */
	const CLAUDE_USAGE_URL = 'https://claude.ai/settings/usage';

	interface Props {
		onOpenChange?: (open: boolean) => void;
		/** Print the percentage under the ring (the phone's quick bar). */
		showPercent?: boolean;
		/** Context used, rounded, for a caller that shows it elsewhere. */
		percent?: number;
		/** More is in context than the window holds; `percent` stops at 100. */
		overLimit?: boolean;
	}

	let { onOpenChange, showPercent = false, percent = $bindable(0), overLimit = $bindable(false) }: Props = $props();

	// ── Effective model → context window (200k / 1M) ──────────────────────
	// The session's model pick may be a `prov:<providerId>:<modelId>` composite
	// (PA13's cross-provider switch) — decode it once and let BOTH the model id
	// and the provider id come from the pick, mirroring the backend's resolution.
	const overrideComposite = $derived(decodeProviderModel($activeSession.modelOverride));
	const defaultComposite = $derived(decodeProviderModel(getDefaultModel()));
	const effectiveModel = $derived.by(() => {
		if (overrideComposite) return overrideComposite.modelId;
		if ($activeSession.modelOverride) return $activeSession.modelOverride;
		// The default can itself be a composite (no Claude credential on the worker).
		return defaultComposite?.modelId ?? getDefaultModel();
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
		if ($activeSession.liveProvider) return $activeSession.liveProvider;
		return !$activeSession.modelOverride && defaultComposite ? defaultComposite.providerId : null;
	});
	const modelCapacity = $derived(getContextMaxForModel(effectiveModel, effectiveProviderId));
	// The chat's picked window (200k / 1M), resolved exactly as the next send
	// resolves it, so the meter shows what that turn will run with. Null when
	// this model offers no choice: its own window applies.
	const selectedWindow = $derived.by(() => {
		const choice = $activeSession.contextWindowChoice;
		if (!isContextWindowChoice(choice)) return null;
		return contextWindowForChoice($activeSession.modelOverride || getDefaultModel(), choice);
	});

	// Context usage from activeSession — set by onPollApplied after each poll.
	const rawContextUsed = $derived($activeSession.contextUsed ?? 0);

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
		// This menu opens upward. Bound it to the actual space above the
		// trigger (including a reduced chat column), not the whole viewport.
		const triggerTop = wrapperRef?.getBoundingClientRect().top ?? window.innerHeight;
		const minTop = Math.max(0, column?.top ?? 0) + 12;
		el.style.setProperty("--usage-popover-height", `${Math.max(100, triggerTop - minTop - 6)}px`);
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

	/** Open the details from another control (the phone's controls panel). */
	export function openDetails() {
		if (!open) setOpen(true);
	}

	// ── SDK/persisted breakdown = the single display source for the context section ─
	let richBreakdown = $state<ContextBreakdown | null>(null);
	// True when `richBreakdown` was measured by the turn that is running now
	// (a pushed per-call snapshot, or a live fetch mid-turn) rather than left
	// over from the previous turn.
	let richFromRunningTurn = $state(false);
	const sessionId = $derived($activeSession.sessionId);
	const isStreaming = $derived($activeSession.isStreaming);

	// The denominator for the ring, header, bar, rows and severity alike.
	const contextWindow = $derived(resolveContextWindow({
		selected: selectedWindow,
		reported: richBreakdown?.contextMax ?? null,
		capacity: modelCapacity,
		reportedIsCurrentTurn: isStreaming && richFromRunningTurn,
	}));

	const breakdown = $derived(
		computeContextBreakdown({
			rawContextUsed,
			inputTokens: $activeSession.totalTokensIn ?? 0,
			outputTokens: $activeSession.totalTokensOut ?? 0,
			cacheCreationTokens: $activeSession.totalCacheCreationTokens ?? 0,
			cacheReadTokens: $activeSession.totalCacheReadTokens ?? 0,
			contextMax: contextWindow.window,
		})
	);
	const displayBreakdown = $derived<ContextBreakdown>(
		fitBreakdownToWindow(richBreakdown ?? breakdown, contextWindow.window)
	);
	const usedOfMax = $derived(
		`${formatTokenCount(displayBreakdown.totalUsed)} / ${formatTokenCount(displayBreakdown.contextMax)}`
	);
	const nextWindowText = $derived(
		contextWindow.nextWindow != null ? `${formatTokenCount(contextWindow.nextWindow)} from the next turn` : null
	);

	// The pill ring prefers the rich breakdown when loaded (its totalPercent is
	// the same figure the popover header shows — so they can't disagree),
	// falling back to the poller estimate before the first fetch lands.
	const contextPercent = $derived(displayBreakdown.totalPercent);
	$effect(() => { percent = Math.round(contextPercent); });
	$effect(() => { overLimit = displayBreakdown.overLimit; });

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
				richFromRunningTurn = res.source === 'live' && untrack(() => isStreaming);
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
				richFromRunningTurn = false;
				loadedSessionId = null;
				break;
			case 'session-fetch':
				cancelFetch();
				richBreakdown = null;
				richFromRunningTurn = false;
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
		richFromRunningTurn = true;
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
		rolledOver: boolean;
	}

	/**
	 * A window row as drawn: a reported window, or a placeholder for a baseline
	 * window that has no reading yet. Placeholders keep the section's rows in
	 * place, so a first report fills a row in rather than adding one.
	 */
	interface RowSlot {
		type: string;
		label: string;
		/** Null for a placeholder: the row shows "—" and an empty bar. */
		pct: number | null;
		severity: UsageSeverity;
		resetText: string;
	}

	/**
	 * Windows a section always shows, reported or not. Claude and ChatGPT/Codex
	 * subscriptions report a 5-hour and a weekly window; SuperGrok a weekly one.
	 */
	const CLAUDE_BASELINE_WINDOWS = ['five_hour', 'seven_day'] as const;
	function providerBaselineWindows(pb: { providerType: string | null }, showsRemaining: boolean): readonly string[] {
		if (!showsRemaining) return [];
		if (pb.providerType === 'chatgpt') return CLAUDE_BASELINE_WINDOWS;
		if (pb.providerType === 'grok') return ['seven_day'];
		return [];
	}

	/** Reported windows plus a placeholder for each missing baseline window, baseline first. */
	function withBaseline<T extends { type: string }>(
		reported: T[],
		baseline: readonly string[],
		toSlot: (w: T) => RowSlot
	): RowSlot[] {
		const placeholders = baseline
			.filter((type) => !reported.some((w) => w.type === type))
			.map<RowSlot>((type) => ({
				type,
				label: windowLabel(type),
				pct: null,
				severity: 'neutral' as UsageSeverity,
				resetText: 'not reported yet',
			}));
		const slots = [...reported.map(toSlot), ...placeholders];
		const rank = (type: string) => {
			const i = baseline.indexOf(type);
			return i < 0 ? baseline.length : i;
		};
		// Stable sort: baseline windows in their fixed order, then the rest as reported.
		return slots
			.map((slot, i) => ({ slot, i }))
			.sort((a, b) => rank(a.slot.type) - rank(b.slot.type) || a.i - b.i)
			.map(({ slot }) => slot);
	}

	function providerSlots(
		pb: { providerType: string | null },
		windows: UsageWindow[],
		showsRemaining: boolean,
		nowMs: number
	): RowSlot[] {
		return withBaseline(windows, providerBaselineWindows(pb, showsRemaining), (w) => {
			const display = providerWindowPercent(w, nowMs, showsRemaining);
			return { type: w.type, label: windowLabel(w.type), pct: display.pct, severity: display.severity, resetText: resetText(w, nowMs) };
		});
	}

	function resetText(usageWindow: UsageWindow, nowMs: number): string {
		const countdown = formatCountdown(usageWindow.resets_at, nowMs);
		if (countdown) return `resets in ${countdown}`;
		if (isRolledOver(usageWindow, nowMs)) return 'reset · awaiting next report';
		if (usageWindow.resets_at != null) return 'resets soon';
		return 'no reset window';
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

	function isRolledOver(usageWindow: UsageWindow, nowMs: number): boolean {
		return windowRolledOver(usageWindow, nowMs);
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
				rolledOver: isRolledOver(usageWindow, now),
			};
		});
	}

	/**
	 * Each expanded section ends in ONE fixed meta line: a quiet "Checked <age>"
	 * (or the one thing the user must do), plus the dashboard link. Staleness is
	 * not announced elsewhere: the figures stay those of the last report, and
	 * the line beside them says when that was. The provider dashboards are the
	 * place to confirm a figure.
	 */
	interface MetaLine {
		text: string;
		/** Guidance the user can act on, rather than the check time. */
		guidance: boolean;
		/** The full text plus the check time, for the tooltip. */
		title: string;
	}

	function checkedText(at: number | null, nowMs: number): string | null {
		return at != null ? `Checked ${formatAsOf(at, nowMs)}` : null;
	}

	function claudeMeta(bucket: UsageBucket | null, nowMs: number): MetaLine {
		if (remoteUnreported) {
			const text = `Not reported from ${deviceLabel ?? 'that device'} yet`;
			return { text, guidance: false, title: `${text}. It appears after that device's next Claude turn.` };
		}
		if (!bucket || Object.keys(bucket.windows ?? {}).length === 0) {
			return { text: 'Not reported yet', guidance: false, title: 'Usage appears after the next Claude turn.' };
		}
		const observedAt = bucketObservedAt(bucket, nowMs);
		if (bucketHasUndatedWindow(bucket, nowMs) || observedAt == null) {
			return { text: 'Check time unknown', guidance: false, title: 'Check time unknown' };
		}
		const checked = checkedText(observedAt, nowMs) as string;
		// Installs on one Claude account share the freshest reading: name the
		// install(s) it came from, since it may not be the target itself.
		const reporters = bucketReporters(bucket);
		if (reporters.length === 0) return { text: checked, guidance: false, title: checked };
		const text = `${checked} · from ${reporters.join(' and ')}`;
		const detail = Object.values(bucket.windows)
			.filter((w) => w.reportedBy)
			.map((w) => {
				const who = w.reportedBy?.deviceName || w.reportedBy?.deviceId || 'another device';
				const when = w.received_at != null ? formatAsOf(w.received_at, nowMs) : 'at an unknown time';
				return `${windowLabel(w.type)}: reported from ${who} ${when}`;
			});
		return { text, guidance: false, title: [text, ...detail].join('\n') };
	}

	/** Something the user can act on for this provider row, or null. */
	function providerGuidance(pb: ProviderUsageBucket): string | null {
		// An OpenAI API key never has a readable balance (see `creditsLine`):
		// no sign-in or billing access would change that, so ask for nothing.
		if (pb.providerType === 'openai' && creditsNum(pb.credits, 'balance_remaining') == null) return null;
		// OpenRouter and fal.ai read the balance with the key already saved; no
		// extra billing access exists to add, so a refusal is about that key
		// and the always-present credits line already says so.
		if (pb.lastErrorKind === 'billing_access_denied') {
			return pb.providerType?.startsWith('openrouter') || pb.providerType === 'fal' ? null : 'Billing access required';
		}
		if (pb.lastErrorKind === 'missing_credentials') return 'Sign in to view usage';
		if (pb.lastErrorKind === 'unsupported') return 'Not supported for this credential; polling paused';
		return null;
	}

	function providerMeta(pb: ProviderUsageBucket, nowMs: number): MetaLine {
		const checked = checkedText(pb.fetchedAt, nowMs) ?? 'Not checked yet';
		const guidance = providerGuidance(pb);
		if (guidance) return { text: guidance, guidance: true, title: `${guidance} · ${checked}` };
		return { text: checked, guidance: false, title: checked };
	}

	/**
	 * Providers that bill against a credit balance always show a credits line,
	 * so a balance that cannot be read swaps text in place instead of removing
	 * the line.
	 */
	function creditsLine(pb: ProviderUsageBucket): string | null {
		const text = creditsSummary(pb.credits, pb.providerType);
		if (text) return text;
		// No source can read an OpenAI API prepaid balance: the Admin API reports
		// spend only, and the billing endpoints refuse API and Admin keys.
		if (pb.providerType === 'openai') return 'Credit balance not available for this key';
		if (pb.providerType?.startsWith('openrouter') || pb.providerType === 'fal') return 'Balance unavailable for this key';
		return null;
	}

	function creditsSummary(credits: Record<string, unknown> | null, type: string | null): string | null {
		return creditText(credits, type)?.detail ?? null;
	}
	function creditsShort(credits: Record<string, unknown> | null, type: string | null): string | null {
		return creditText(credits, type)?.short ?? null;
	}

	const target = $derived($usageTargetInstance);
	const resolvedBucket = $derived(resolveBucket($rateLimit, target));
	const resolvedKey = $derived(resolveBucketKey($rateLimit, target));
	const items = $derived.by<WindowItem[]>(() => (resolvedBucket ? itemsFor(resolvedBucket) : []));
	// Claude plan rows: the 5-hour and weekly windows always hold a row (an
	// API-key login reports none, so it gets no placeholders).
	const claudeSlots = $derived.by<RowSlot[]>(() =>
		withBaseline(items, claudeDisplay.subtext === 'API' ? [] : CLAUDE_BASELINE_WINDOWS, (item) => ({
			type: item.type,
			label: item.label,
			pct: item.pct,
			severity: item.severity,
			resetText: item.countdown
				? `resets in ${item.countdown}`
				: item.rolledOver
					? 'reset · awaiting next report'
					: item.hasReset
						? 'resets soon'
						: 'no reset window',
		}))
	);

	const ringSeverity = $derived<UsageSeverity>(usageSeverity(contextPercent));

	// ── Resolved-install header (brand + auth-kind subtext) ───────────────
	// Every section header in this popover reads BRAND NAME + a subtext saying
	// how that account authenticates, from the shared naming rules in
	// `constants/models` — so the popover, the composer's model dropdown and the
	// agents-UI picker can never drift apart.
	//
	// Claude: "Claude" + its subscription plan ("max"/"pro"), or "API" on
	// API-key auth, from the local registry's `claude` block (which reads what
	// the Claude Code CLI reports about its own sign-in). It describes THIS
	// install only, so it is consulted just for the self install; usage
	// snapshots carry no account or plan.
	//
	// Without it the subtext falls back to the resolved install's device
	// name / "Other device" hint rather than dropping that disambiguation.
	const isSelf = $derived(isSelfTarget($rateLimit, target) ||
		(resolvedKey != null && resolvedKey === selfBucketKey($rateLimit)));
	const selfClaude = $derived($claudeAuthenticated || getClaudeAuth().auth_kind === 'api');
	// Empty usage buckets also exist on installs without Claude. Subscription
	// display metadata defaults to "subscription", so it is not proof of a login.
	// Once Claude is in use on any install, a session on a device whose usage
	// has not reached this one keeps the row with a "nothing reported" state:
	// hiding it read as "Claude usage is gone".
	const claudeInUse = $derived(selfClaude || Object.values($rateLimit.buckets).some((b) => b.available));
	const hasClaude = $derived(Boolean(
		resolvedBucket?.available ||
		(isSelf ? selfClaude : claudeInUse)
	));
	const deviceLabel = $derived(resolvedBucket?.deviceName ?? (isSelf ? null : $usageTargetDeviceName));
	/** Another device owns the session and none of its usage has reached this one. */
	const remoteUnreported = $derived(!isSelf && !resolvedBucket?.available && items.length === 0);
	const claudeDisplay = $derived<ProviderDisplay>(
		getClaudeDisplay({
			planType: isSelf ? getClaudeAuth().plan_type : null,
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

	// One order for every usage section, Claude included (#1845):
	// subscriptions (ChatGPT, SuperGrok, a Claude plan) above API accounts, so
	// OpenAI · API never sits next to OpenAI · prolite and reads as one section
	// (#1087); within each group, the Settings > Providers order (OpenAI, xAI
	// Grok, Claude, OpenRouter), then fal.ai and anything else, by name.
	function isSubscriptionProvider(pb: { providerType: string | null }): boolean {
		const t = (pb.providerType || '').toLowerCase();
		return t === 'chatgpt' || t === 'grok';
	}

	function providerFamily(type: string | null): string {
		const t = (type || '').toLowerCase();
		if (t === 'chatgpt' || t === 'openai') return 'openai';
		if (t === 'grok' || t === 'xai') return 'grok';
		if (t.startsWith('openrouter')) return 'openrouter';
		return t;
	}

	function familyRank(family: string): number {
		const i = (AGENT_PROVIDER_ORDER as readonly string[]).indexOf(family);
		if (i >= 0) return i;
		return family === 'fal' ? AGENT_PROVIDER_ORDER.length : AGENT_PROVIDER_ORDER.length + 1;
	}

	interface SectionEntry {
		id: string;
		kind: 'claude' | 'provider';
		pb: ProviderUsageBucket | null;
		name: string;
		subscription: boolean;
		rank: number;
	}

	function compareSections(a: SectionEntry, b: SectionEntry): number {
		return (
			Number(!a.subscription) - Number(!b.subscription) ||
			a.rank - b.rank ||
			a.name.localeCompare(b.name) ||
			a.id.localeCompare(b.id)
		);
	}

	function sortedProviderBuckets() {
		const buckets = Object.values($rateLimit.providerBuckets ?? {});
		return buckets.filter(pb => {
			if (pb.providerType === 'openrouter') {
				return !buckets.some(account => account.providerId === `openrouter-account:${pb.providerId}` && creditsNum(account.credits, 'balance_remaining') != null);
			}
			if (pb.providerType === 'openrouter_account' && creditsNum(pb.credits, 'balance_remaining') == null) {
				return !buckets.some(key => key.providerId === pb.providerId.replace(/^openrouter-account:/, ''));
			}
			return true;
		});
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
		if (type === 'grok' || type === 'openrouter') return true;
		if (type !== 'chatgpt') return false;
		const kind = entry?.auth_kind ?? 'subscription';
		return kind === 'subscription';
	}

	function providerWindowSummary(
		windows: UsageWindow[],
		nowMs: number,
		showsRemaining: boolean
	): PercentDisplay | null {
		// A subscription's 5-hour window is the one a session works against
		// right now (ChatGPT/Codex reports one, like Claude): summarize by it.
		const fiveHour = showsRemaining ? windows.find((w) => w.type === 'five_hour') : undefined;
		if (fiveHour) return providerWindowPercent(fiveHour, nowMs, showsRemaining);
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

	const sections = $derived.by<SectionEntry[]>(() => {
		const entries: SectionEntry[] = sortedProviderBuckets().map((pb) => ({
			id: `provider:${pb.providerId}`,
			kind: 'provider',
			pb,
			name: providerDisplayFor(pb).name,
			subscription: isSubscriptionProvider(pb),
			rank: familyRank(providerFamily(pb.providerType)),
		}));
		if (hasClaude) {
			entries.push({
				id: 'plan',
				kind: 'claude',
				pb: null,
				name: claudeDisplay.name,
				// A plan login is a subscription; an API-key login is an API account.
				subscription: claudeDisplay.subtext !== 'API',
				rank: familyRank('claude'),
			});
		}
		return entries.sort(compareSections);
	});
	const hiddenRows = $derived(sections.filter((s) => $hiddenUsageProviders[s.id]));
	const hasUsageSections = $derived(sections.length > 0);

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
	// The collapsed Claude summary is the 5-hour window, the one a session is
	// working against right now; the expanded rows keep every limit. Without a
	// 5-hour reading it falls back to the smallest remaining percentage (null →
	// omit for an empty bucket).
	const planSummaryRemaining = $derived<number | null>(
		items.find((it) => it.type === 'five_hour')?.pct ??
			(items.length ? Math.min(...items.map((it) => it.pct)) : null)
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

<!-- A window row: label, reset text and percent on one line over a thin bar.
     A placeholder (pct null) keeps the same shape with "—" and an empty bar. -->
{#snippet windowRow(slot: RowSlot, numberKey: string, suffix: string)}
	<div class="popover-row" class:placeholder={slot.pct == null}>
		<div class="row-line">
			<span class="row-label">{slot.label}</span>
			<span class="row-value">
				<span class="row-reset-inline">{slot.resetText}</span>
				{#if slot.pct == null}
					<span class="row-pct row-pct-empty">—</span>
				{:else}
					<span class="row-pct {slot.severity}" use:usageNumber={{ key: numberKey, text: `${slot.pct}%${suffix}` }}></span>
				{/if}
			</span>
		</div>
		<div class="row-bar">
			<div class="row-fill {slot.severity}" style="width: {slot.pct ?? 0}%"></div>
		</div>
	</div>
{/snippet}

<!-- The one fixed meta line closing an expanded section: when the figures were
     last checked (or what the user must do) on the left, the dashboard link on
     the right. One line whatever the state; the tooltip holds the full text. -->
{#snippet metaLine(meta: MetaLine, href: string | null | undefined, linkText: string, linkLabel: string)}
	<div class="usage-meta">
		<span class="usage-meta-text" class:usage-checked={!meta.guidance} class:provider-status={meta.guidance} title={meta.title}>{meta.text}</span>
		{#if href}
			<a class="provider-dashboard-link" {href} target="_blank" rel="noopener noreferrer" aria-label={linkLabel} title={linkLabel}
				>{linkText}<ExternalLink size={10} aria-hidden="true" /></a
			>
		{/if}
	</div>
{/snippet}

{#snippet sectionHeader(id: string, name: string, subtext: string | null, isOpen: boolean, ariaName: string, summary: Snippet)}
	<div class="popover-section">
		<div class="provider-header">
			<button
				type="button"
				class="sec-header"
				aria-expanded={isOpen}
				aria-label="{ariaName} — {isOpen ? 'collapse' : 'expand'}"
				onclick={() => toggleSection(id)}
			>
				<span class="sec-head-labels">
					<span class="section-device">{name}</span>
					{#if subtext}
						<span class="section-account">{subtext}</span>
					{/if}
				</span>
				{#if !isOpen}{@render summary()}{/if}
				<svg
					class="ctx-chevron {isOpen ? '' : 'collapsed'}"
					viewBox="0 0 24 24"
					fill="none"
					stroke="currentColor"
					aria-hidden="true"
				>
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
				</svg>
			</button>
			{@render hideProvider(id, name)}
		</div>
	</div>
{/snippet}

<!-- ═══ Plan usage limits (Claude) ═══
     Brand + auth-kind subtext (plan / "API"), else the resolved device name or
     "Other device". Collapsed: the 5-hour window's remaining quota ("—" before
     a report). Expanded: the 5-hour and weekly rows always (placeholders until
     reported), any other reported window, then the meta line. -->
{#snippet claudeSummary()}
	{#if planSummaryRemaining != null}
		<span class="row-pct {remainingSeverity(planSummaryRemaining)} sec-summary"
			use:usageNumber={{ key: `plan:${resolvedKey}:summary`, text: `${planSummaryRemaining}% left` }}></span>
	{:else}
		<span class="sec-summary sec-credits sec-empty">—</span>
	{/if}
{/snippet}

{#snippet claudeSection()}
	{@render sectionHeader('plan', claudeDisplay.name, claudeSubtext, planRowsVisible, 'Plan usage limits', claudeSummary)}
	{#if planRowsVisible}
		{#each claudeSlots as slot (slot.type)}
			{@render windowRow(slot, `plan:${resolvedKey}:${slot.type}`, ' left')}
		{/each}
		<!-- AI Shuffle only sees the windows the CLI reports during a run;
		     the full picture is on the user's own claude.ai usage page. -->
		{@render metaLine(claudeMeta(resolvedBucket, now), CLAUDE_USAGE_URL, 'claude.ai', 'See all usage on claude.ai')}
	{/if}
{/snippet}

<!-- ═══ Provider usage (non-Claude) ═══
     ChatGPT/Codex and SuperGrok subscription windows show quota remaining;
     their collapsed summary uses the 5-hour window when there is one, else the
     least remaining window. xAI API-key, other API-key and credit windows keep
     used-of-100 and summarize the highest-utilization window. -->
{#snippet providerSection(pb: ProviderUsageBucket)}
	{@const sectionId = `provider:${pb.providerId}`}
	{@const pbDisplay = providerDisplayFor(pb)}
	{@const pbOpen = isSectionOpen(sectionId)}
	{@const pbWindows = displayWindows(pb.windows)}
	{@const pbShowsRemaining = providerShowsRemaining(pb)}
	{@const suffix = pbShowsRemaining ? ' left' : ''}
	{#snippet providerSummary()}
		{@const pbSummary = providerWindowSummary(pbWindows, now, pbShowsRemaining)}
		{@const pbCreditsShort = creditsShort(pb.credits, pb.providerType)}
		<!-- An account credit balance is the headline the user cares about, so
		     it outranks the window pct (which an OpenRouter key-spend limit
		     would otherwise produce). -->
		{#if creditsNum(pb.credits, 'balance_remaining') != null && pbCreditsShort}
			<span class="sec-summary sec-credits" use:usageNumber={{ key: `${resolvedKey}:${sectionId}:credits-short`, text: pbCreditsShort }}></span>
		{:else if pbSummary}
			<span class="row-pct {pbSummary.severity} sec-summary"
				use:usageNumber={{ key: `${resolvedKey}:${sectionId}:summary`, text: `${pbSummary.pct}%${suffix}` }}></span>
		{:else if pbCreditsShort && pb.providerType !== 'chatgpt'}
			<!-- ChatGPT extra credits live in the expanded rows only. -->
			<span class="sec-summary sec-credits" use:usageNumber={{ key: `${resolvedKey}:${sectionId}:credits-short`, text: pbCreditsShort }}></span>
		{:else if pb.providerType?.startsWith('openrouter') || pb.providerType === 'fal' || pb.providerType === 'openai'}
			<span class="sec-summary sec-credits">Balance unavailable</span>
		{:else}
			<span class="sec-summary sec-credits sec-empty">—</span>
		{/if}
	{/snippet}
	{@render sectionHeader(sectionId, pbDisplay.name, pbDisplay.subtext, pbOpen, `${pbDisplay.name} usage`, providerSummary)}
	{#if pbOpen}
		{#each providerSlots(pb, pbWindows, pbShowsRemaining, now) as slot (slot.type)}
			{@render windowRow(slot, `${resolvedKey}:${sectionId}:${slot.type}`, suffix)}
		{/each}
		{@const credits = creditsLine(pb)}
		{#if credits}
			<div class="row-reset usage-credits" use:usageNumber={{ key: `${resolvedKey}:${sectionId}:credits`, text: credits }}></div>
		{/if}
		{@render metaLine(providerMeta(pb, now), pb.sourceInfo?.dashboard_url, 'Dashboard', 'Compare on the provider dashboard')}
	{/if}
{/snippet}

<div class="usage-context-wrapper" bind:this={wrapperRef}>
	<button
		type="button"
		class="usage-indicator"
		title="{usedOfMax} tokens{displayBreakdown.overLimit ? ', over the limit' : ''}{nextWindowText ? ` · ${nextWindowText}` : ''} — click for details"
		aria-label="Context and usage — {displayBreakdown.overLimit
			? `over the limit, ${usedOfMax} tokens`
			: `${Math.round(contextPercent)}% of context window used`}"
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
		{#if showPercent}<span class="usage-percent" aria-hidden="true">{displayBreakdown.overLimit ? 'Over' : `${Math.round(contextPercent)}%`}</span>{/if}
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
						{usedOfMax}
						<span class="ctx-pct {displayBreakdown.severity}"
							use:usageNumber={{ key: `context:${resolvedKey}:${sessionId ?? 'new'}:${effectiveProviderId}:${effectiveModel}`, text: displayBreakdown.overLimit ? '(over limit)' : `(${Math.round(displayBreakdown.totalPercent)}%)` }}></span>
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

				{#if nextWindowText}
					<!-- A pick applies from the next turn; the running one keeps its window. -->
					<p class="ctx-next">This turn uses {formatTokenCount(displayBreakdown.contextMax)} · {nextWindowText}</p>
				{/if}

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

			{#if hasUsageSections}
				<div class="popover-divider"></div>
			{/if}

			<!-- ═══ Usage sections, one order for all (#1845) ═══ -->
			{#each sections.filter((s) => !$hiddenUsageProviders[s.id]) as section (section.id)}
				{#if section.kind === 'claude'}
					{@render claudeSection()}
				{:else if section.pb}
					{@render providerSection(section.pb)}
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
		max-height: min(var(--usage-popover-height, calc(100dvh - 24px)), calc(100dvh - 24px));
		overflow-y: auto;
		overscroll-behavior: contain;
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

	.ctx-next {
		margin: 0;
		font-size: 10.5px;
		color: var(--muted-foreground);
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
	/* A baseline window with no reading yet: same row, muted dash. */
	.row-pct-empty {
		font-weight: 400;
		color: var(--muted-foreground);
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
	/* The section's one meta line: check time (or guidance) left, dashboard
	   link right. Never wraps, so no state adds a line; the full text is in
	   the tooltip. A fixed height keeps it identical with or without a link. */
	.usage-meta {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		min-width: 0;
		height: 16px;
		font-size: 10.5px;
		line-height: 16px;
		color: var(--muted-foreground);
	}
	.usage-meta-text {
		flex: 1 1 auto;
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		font-variant-numeric: tabular-nums;
	}
	/* Guidance the user can act on reads a step brighter than the check time. */
	.usage-meta-text.provider-status {
		color: var(--foreground);
		opacity: 0.85;
	}
	.provider-dashboard-link {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		flex-shrink: 0;
		font-size: 10.5px;
		color: var(--muted-foreground);
		text-decoration: underline;
		text-underline-offset: 2px;
		border-radius: 3px;
		transition: color 160ms ease;
	}
	.provider-dashboard-link:hover {
		color: var(--foreground);
	}
	.provider-dashboard-link:focus-visible {
		outline: 2px solid var(--ring, var(--primary));
		outline-offset: 2px;
	}
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
	.sec-credits.sec-empty {
		font-weight: 400;
	}
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
