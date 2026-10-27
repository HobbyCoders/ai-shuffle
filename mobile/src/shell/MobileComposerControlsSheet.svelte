<script module lang="ts">
	/** Reparent to <body>: no composer ancestor may clip or stack the panel. */
	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return { destroy() { node.remove(); } };
	}
</script>


<script lang="ts">
	/**
	 * The phone's chat controls (issue #1816, design 4): a quick bar under the
	 * composer's text field and the full "Chat controls" panel it pulls up.
	 *
	 * Quick bar: Model · Effort · Access · usage ring · Controls. Each cell shows
	 * its state through its own icon and label. A tap opens a compact picker
	 * (Effort, Access) or a page (Model, usage); a sideways swipe on Effort or
	 * Access steps one level but never lands on Bypass. Dragging the bar up, or
	 * tapping Controls, opens the panel; dragging its grip or header down, the
	 * scrim and Escape close it. Every gesture follows the finger and has a tap.
	 *
	 * The panel is portaled to <body> so no composer ancestor clips it. Its
	 * main page is an inset grouped list: status chips (running work, worker,
	 * project and branch), then Model (model, context and usage, reasoning
	 * effort, Fast mode), Permissions (tool access, Away) and Setup (the
	 * shared ContextPillBar selectors drawn as rows), over a pinned action bar
	 * (Call / Browser / Terminal / Export). Rows with a chevron open a page
	 * that slides in from the right inside the same sheet, under a centred
	 * title and a Back arrow:
	 *   Model           the app's own ModelPickerPanel;
	 *   Context/usage   the shared ContextUsageBar details, inline;
	 *   Agent, Runs on  the shared selectors' own menus, hosted on the page
	 *                   (menuHost), so the busy rule for moving a chat stays
	 *                   the desktop's; picking closes the menu and slides back;
	 *   Running tasks   RunningTasksList as a page;
	 *   Export chat     ChatExportActions as a page with a pinned button.
	 * There is no visible close button; a visually hidden one stays for
	 * keyboard and screen-reader users. Prompts never render here: they dock
	 * above the composer (MobilePromptDock).
	 *
	 * ControlsDock (shared $lib) computes every value and owns the open state;
	 * this component only renders and reports intent.
	 */
	import { tick, untrack } from 'svelte';
	import { pushState } from '$app/navigation';
	import { page } from '$app/state';
	import {
		ChevronLeft, ChevronRight, Cpu, Download, FilePen, Globe, Hand, Lightbulb, LoaderCircle, Lock,
		Moon, ShieldCheck, ShieldOff, SlidersHorizontal, Sparkles, SquareTerminal, Zap
	} from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { watches } from '$lib/stores/watches';
	import { watchCount } from '$lib/components/chat/running-tasks';
	import { notify } from '$lib/stores/notifications';
	import { clusterProjects, projects, sessions } from '$lib/stores/app-data.svelte';
	import { gitStatus } from '$lib/stores/git.svelte';
	import { workerInstalls } from '$lib/stores/worker-presence';
	import { closeDropdown } from '$lib/stores/header-dropdowns';
	import ContextPillBar from '$lib/components/chat/ContextPillBar.svelte';
	import ContextUsageBar from '$lib/components/chat/ContextUsageBar.svelte';
	import RunningTasksList from '$lib/components/chat/RunningTasksList.svelte';
	import ChatExportActions from '$lib/components/layout/ChatExportActions.svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
	import ProviderGlyph from '$lib/components/settings/ProviderGlyph.svelte';
	import type { PickerGroup } from '$lib/constants/model-picker';
	import type { ComposerSheetProps as Props } from '$lib/components/chat/composer-shell';
	import EffortMeter from './EffortMeter.svelte';
	import {
		ACCESS_HELP, ACCESS_LABEL, ACCESS_SHORT, BYPASS, VelocityTracker, dragAxis, settlesClosed,
		settlesOpen, stepValue, taskSummary
	} from './quick-controls';

	let {
		open, onClose, onOpenChange, panelId = 'mobile-composer-panel', inertToolbar = false,
		modelOptions, modelGroups, modelValue, modelLabel, modelNotice, onModelSelect, modelLocked = false,
		contextChoice, onContextChoiceSelect, contextStandardTokens, onContextStandardChange, effortOptions, effortValue, onEffortSelect,
		effortExplanation, effortKnown = true, effortDefaultValue = null,
		fastSupported = false, fastEnabled = false, fastPending = false, onToggleFast, showCodeControls = true,
		showSetup = true, permissionOptions, permissionValue, permissionPending = false, permissionUpdating = false, onPermissionSelect,
		awayEnabled = false, awayAvailable = false, awayBusy = false, onToggleAway,
		onUsageOpenChange, callAction, browserOpen = false, onBrowserToggle, onBrowserPreload, terminal
	}: Props = $props();

	type Picker = 'effort' | 'access';
	type Page = 'main' | 'model' | 'usage' | 'agent' | 'runner' | 'tasks' | 'export';
	const PAGE_TITLE: Record<Page, string> = {
		main: 'Chat controls',
		model: 'Model',
		usage: 'Context and usage',
		agent: 'Agent',
		runner: 'Runs on',
		tasks: 'Running tasks',
		export: 'Export chat'
	};
	/** How long a page takes to slide; content stays mounted until it is off screen. */
	const PAGE_SLIDE_MS = 320;

	// ── Derived state ─────────────────────────────────────────────────────
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
	/** The current model's provider mark; ProviderGlyph draws its own default for anything unknown. */
	const modelGlyph = $derived.by(() => {
		const key = modelOptions.find((o) => o.value === modelValue)?.groupKey;
		return railGroups.find((g) => g.key === key)?.glyph ?? 'custom';
	});

	const effortLocked = $derived(modelLocked || !effortKnown || effortOptions.length === 0);
	const effortValues = $derived(effortOptions.map((o) => o.value));
	/** What the meter draws: the pinned level, else the model's default. */
	const effortShown = $derived(effortValue ?? effortDefaultValue ?? null);
	const effortIndex = $derived(effortShown ? effortValues.indexOf(effortShown) : -1);
	const effortLabel = $derived(
		!effortKnown ? (effortValue ?? 'Unknown')
			: !effortOptions.length ? 'Fixed'
			: effortValue ? (effortOptions.find((o) => o.value === effortValue)?.label ?? 'Auto')
			: 'Auto'
	);
	/** The effort row's value: the level, or why there is no choice. */
	const panelEffortValue = $derived(effortLocked && effortKnown ? 'Set by the model' : effortLabel);
	const effortA11y = $derived(
		!effortKnown ? `Reasoning effort: ${effortValue ?? 'unknown'}`
			: !effortOptions.length ? 'Reasoning effort: set by the model'
			: `Reasoning effort: ${effortLabel}`
	);
	// Tool access exists in code chats; chat and voice run a fixed tool surface.
	const accessShown = $derived(showCodeControls);
	const accessValues = $derived(permissionOptions.map((o) => o.value));
	// While the chat's own access is still loading (#418) no mode is selected:
	// showing Ask would be a guess, and stepping from a guess picks the wrong one.
	const accessValue = $derived(permissionPending ? '' : (permissionValue ?? 'default'));
	const accessIcons: Record<string, typeof Hand> = { default: Hand, auto: Sparkles, acceptEdits: FilePen, bypassPermissions: ShieldOff };
	const awayShown = $derived(accessShown && !!onToggleAway);
	/** Away as the quick bar shows it: the desktop composer's "· Away" badge. */
	const awayOn = $derived(awayShown && awayEnabled && awayAvailable);
	const fastShown = $derived(fastSupported && !modelLocked);

	const runningTasks = $derived(($activeSession.activeTasks ?? []).filter((t) => t.status === 'running'));
	// The owner's watches (#371) open the same list. They are long-lived, so
	// they get the chip but not the Controls badge, which means work running now.
	const liveWatches = $derived($watches.list.filter((w) => w.status !== 'stopped').length);
	const pausedWatches = $derived($watches.list.filter((w) => w.status === 'paused').length);
	// A worker chosen for new watches stays changeable with no watch live (#565).
	const preferenceOnly = $derived(!runningTasks.length && !liveWatches && !!$watches.preferredWorkerId);
	const runSummary = $derived(
		runningTasks.length
			? [taskSummary(runningTasks), liveWatches ? watchCount(liveWatches) : ''].filter(Boolean).join(' · ')
			: preferenceOnly
				? 'Where new watches run'
				: pausedWatches
				? `${pausedWatches} paused`
				: 'Watching for changes'
	);
	const runHead = $derived(
		runningTasks.length ? `${runningTasks.length} running` : preferenceOnly ? 'Watches' : watchCount(liveWatches)
	);
	const runChipLabel = $derived(
		runningTasks.length ? `${runningTasks.length} ${runningTasks.length === 1 ? 'task' : 'tasks'} running` : runHead
	);
	const showRunChip = $derived(runningTasks.length > 0 || liveWatches > 0 || preferenceOnly);
	const canExport = $derived(!!$activeSession.sessionId && $activeSession.messages.length > 0);

	// Status chips: where this chat runs and what it works on, when known.
	const sessionRow = $derived($sessions.find((s) => s.id === $activeSession.sessionId) ?? null);
	const selfName = $derived($workerInstalls?.find((i) => i.is_self)?.device_name ?? null);
	const workerName = $derived.by((): string | null => {
		if (!$activeSession.sessionId) return $activeSession.deviceIntent?.name ?? selfName;
		if (sessionRow?.is_remote ?? $activeSession.isRemote) return sessionRow?.device_name ?? $activeSession.ownerDeviceName ?? null;
		return selfName ?? sessionRow?.device_name ?? null;
	});
	const projectName = $derived(
		$activeSession.mode === 'chat' ? null
			: $projects.find((p) => p.id === $activeSession.project)?.name
				?? $clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name
				?? null
	);
	const branchName = $derived(
		!projectName ? null
			: $activeSession.worktreeBranch || $activeSession.selectedWorktreeBranch
				|| ($activeSession.project ? ($gitStatus?.current_branch ?? null) : null)
	);

	// ── Quick bar: pickers and sideways steps ─────────────────────────────
	let picker = $state<Picker | null>(null);
	/** A swipe's preview, shown on the bar until the finger lifts. */
	let preview = $state<{ cell: Picker; value: string } | null>(null);
	let barRef = $state<HTMLElement | null>(null);
	let pickerRef = $state<HTMLElement | null>(null);
	let controlsCellRef = $state<HTMLButtonElement | null>(null);
	let usageBar = $state<ContextUsageBar | null>(null);
	/** Context used, read back from the shared ring for the panel's usage row. */
	let usagePercent = $state(0);
	let usageOverLimit = $state(false);
	/** Tokens used when the model's context window is unknown (no percentage then). */
	let usageUnknownUsed = $state<string | null>(null);
	let usageUsedTokens = $state('');
	let usageWindowTokens = $state<string | null>(null);
	const RING_C = 2 * Math.PI * 11;
	const usageSeverity = $derived(usagePercent > 90 ? 'danger' : usagePercent >= 70 ? 'warning' : 'ok');
	const usageTokensLine = $derived(
		usageUnknownUsed != null ? `${usageUnknownUsed} tokens used`
			: usageUsedTokens && usageWindowTokens ? `${usageUsedTokens} of ${usageWindowTokens} tokens`
			: null
	);

	const shownEffortIndex = $derived(preview?.cell === 'effort' ? effortValues.indexOf(preview.value) : effortIndex);
	const shownEffortLabel = $derived(preview?.cell === 'effort' ? (effortOptions.find((o) => o.value === preview!.value)?.label ?? effortLabel) : effortLabel);
	const shownAccess = $derived(preview?.cell === 'access' ? preview.value : accessValue);
	const AccessIcon = $derived(accessIcons[shownAccess] ?? Hand);

	function togglePicker(kind: Picker) {
		if (kind === 'effort' && effortLocked) {
			notify(effortExplanation || 'Set by the model', { severity: 'info' });
			return;
		}
		picker = picker === kind ? null : kind;
		if (picker) void tick().then(() => pickerRef?.querySelector<HTMLElement>('[aria-checked="true"], button')?.focus({ preventScroll: true }));
	}

	function closePicker(restoreFocus = false) {
		if (!picker) return;
		const kind = picker;
		picker = null;
		if (restoreFocus) barRef?.querySelector<HTMLElement>(`[data-cell="${kind}"]`)?.focus({ preventScroll: true });
	}

	function pickEffort(value: string) {
		picker = null;
		if (value !== effortValue) onEffortSelect(value);
	}

	function pickAccess(value: string) {
		picker = null;
		if (value !== accessValue && !permissionUpdating) onPermissionSelect(value);
	}

	function handleDocumentPointerDown(event: PointerEvent) {
		if (!picker) return;
		const target = event.target as Node | null;
		if (target && (pickerRef?.contains(target) || barRef?.querySelector(`[data-cell="${picker}"]`)?.contains(target))) return;
		picker = null;
	}

	$effect(() => {
		document.addEventListener('pointerdown', handleDocumentPointerDown, true);
		return () => document.removeEventListener('pointerdown', handleDocumentPointerDown, true);
	});

	// ── Panel open state, pages and Android Back ──────────────────────────
	let pageName = $state<Page>('main');
	/** The page sliding out after Back: kept mounted until it is off screen. */
	let leaving = $state<Page | null>(null);
	let leaveTimer: ReturnType<typeof setTimeout> | undefined;
	/** Opened straight to a page from the bar: Escape and Android Back close the panel. */
	let direct = $state(false);
	/** The row that opened the current page, focused again on the way back. */
	let opener: HTMLElement | null = null;
	let panelRef = $state<HTMLElement | null>(null);
	let bodyRef = $state<HTMLElement | null>(null);
	let backRef = $state<HTMLButtonElement | null>(null);
	/** The Agent and "Runs on" pages host the shared selectors' own menus. */
	let agentHost = $state<HTMLElement | null>(null);
	let runnerHost = $state<HTMLElement | null>(null);
	const menuHosts = $derived({ agent: agentHost, runner: runnerHost });
	/** Stays true through the closing slide so content does not vanish mid-move. */
	let rendered = $state(false);
	let renderTimer: ReturnType<typeof setTimeout> | undefined;
	const onSubPage = $derived(pageName !== 'main');
	const mounted = (p: Page) => pageName === p || leaving === p;

	function requestOpen(next: boolean) {
		if (onOpenChange) onOpenChange(next);
		else if (!next) onClose();
	}

	function openPanel(target: Page = 'main') {
		closePicker();
		setPage(target);
		direct = target !== 'main';
		opener = null;
		if (!open) requestOpen(true);
		else void focusPanel();
	}

	/** Set from a close request until the panel opens again: the close pops every history entry itself. */
	let closingPanel = false;

	function closePanel() {
		closingPanel = true;
		dismissHostedMenus();
		requestOpen(false);
	}

	async function focusPanel() {
		await tick();
		if (pageName === 'main') panelRef?.focus({ preventScroll: true });
		else backRef?.focus({ preventScroll: true });
	}

	/** Switch pages, telling the dock while the usage details are on screen (it polls limits then). */
	function setPage(next: Page) {
		const was = pageName;
		if (was === next) return;
		pageName = next;
		if (was === 'usage') onUsageOpenChange?.(false);
		if (next === 'usage') onUsageOpenChange?.(true);
	}

	function goTo(target: Page, from: HTMLElement | null = null) {
		clearTimeout(leaveTimer);
		leaving = null;
		if (from) opener = from;
		setPage(target);
		direct = false;
		pushPageEntry(target);
		void tick().then(() => {
			panelRef?.querySelector<HTMLElement>(`[data-page="${target}"] .mcp-scroll`)?.scrollTo?.({ top: 0 });
			backRef?.focus({ preventScroll: true });
		});
	}

	/** Back from a page to the main list; `fromHistory` when Android Back already popped its entry. */
	function backToMain(fromHistory = false) {
		if (pageName === 'main') return;
		if (pageEntry && !closingPanel) {
			pageEntry = false;
			if (!fromHistory && page.state?.mobileControlsPage) history.back();
		}
		const from = pageName;
		dismissHostedMenus();
		leaving = from;
		setPage('main');
		direct = false;
		clearTimeout(leaveTimer);
		leaveTimer = setTimeout(() => { if (leaving === from) leaving = null; }, PAGE_SLIDE_MS);
		const row = opener;
		opener = null;
		void tick().then(() => (row?.isConnected ? row : panelRef)?.focus({ preventScroll: true }));
	}

	/** Escape and Android Back: a page opened straight from the bar closes the panel. */
	function back() {
		if (direct || pageName === 'main') { closePanel(); return; }
		backToMain();
	}

	/**
	 * Close a selector menu hosted on the Agent or "Runs on" page. The shared
	 * selectors close their menus on an outside click; the store-driven ones
	 * close directly, and a click on the page body reaches the rest.
	 */
	function dismissHostedMenus() {
		if (!agentHost?.childElementCount && !runnerHost?.childElementCount) return;
		closeDropdown('sheet-profile');
		closeDropdown('runner');
		document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
	}

	/** A hosted menu opened (its row was tapped) or closed (a pick, Escape). */
	function syncHostedPage() {
		const hosted: Page | null = runnerHost?.childElementCount ? 'runner' : agentHost?.childElementCount ? 'agent' : null;
		if (hosted && pageName !== hosted) {
			const row = panelRef?.querySelector<HTMLElement>(
				hosted === 'agent' ? '.mcp-setup .context-selector-btn' : '.mcp-setup .ctx-chip.runner, .mcp-setup .mode-trigger'
			);
			goTo(hosted, row ?? null);
		} else if (!hosted && (pageName === 'agent' || pageName === 'runner')) {
			backToMain();
		}
	}

	$effect(() => {
		const hosts = [agentHost, runnerHost].filter((h): h is HTMLElement => !!h);
		if (!hosts.length || typeof MutationObserver === 'undefined') return;
		const observer = new MutationObserver(() => syncHostedPage());
		for (const host of hosts) observer.observe(host, { childList: true });
		return () => observer.disconnect();
	});

	let historyEntry = false;
	/** A page has its own history entry over the panel's, so Android Back returns to the list. */
	let pageEntry = false;

	function pushPageEntry(target: Page) {
		if (!historyEntry || pageEntry) return;
		try {
			pushState('', { ...(page.state ?? {}), mobileControls: true, mobileControlsPage: target });
			pageEntry = true;
		} catch {
			// Router not ready (tests): Back closes the panel instead.
		}
	}

	$effect(() => {
		const isOpen = open;
		untrack(() => {
			clearTimeout(renderTimer);
			if (isOpen) {
				closingPanel = false;
				rendered = true;
				picker = null;
				void focusPanel();
				if (!historyEntry) {
					try {
						pushState('', { ...(page.state ?? {}), mobileControls: true });
						historyEntry = true;
					} catch {
						// Router not ready (tests): Back just won't close the panel.
					}
				}
				void tick().then(() => {
					if (bodyRef && pageName === 'main') bodyRef.scrollTop = 0;
				});
			} else {
				dismissHostedMenus();
				renderTimer = setTimeout(() => {
					rendered = false;
					setPage('main');
					leaving = null;
					direct = false;
					opener = null;
				}, 400);
				if (panelRef?.contains(document.activeElement)) controlsCellRef?.focus({ preventScroll: true });
				if (historyEntry) {
					historyEntry = false;
					// Pop the page's entry too, so no dead Back press is left behind.
					if (page.state?.mobileControls) history.go(pageEntry && page.state?.mobileControlsPage ? -2 : -1);
				}
				pageEntry = false;
			}
		});
	});

	$effect(() => () => {
		clearTimeout(leaveTimer);
		clearTimeout(renderTimer);
	});

	// Android Back on a page pops that page's entry: return to the list.
	$effect(() => {
		const pageInHistory = !!page.state?.mobileControlsPage;
		untrack(() => {
			if (!pageInHistory && pageEntry && open) backToMain(true);
		});
	});

	// Android Back pops the entry the panel pushed: close without popping again.
	$effect(() => {
		const inHistory = !!page.state?.mobileControls;
		untrack(() => {
			if (!inHistory && historyEntry && open) {
				historyEntry = false;
				closePanel();
			}
		});
	});

	function handleKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape' || event.defaultPrevented) return;
		if (picker) {
			event.preventDefault();
			closePicker(true);
			return;
		}
		if (!open) return;
		event.preventDefault();
		event.stopPropagation();
		back();
	}

	// ── Drag: pull the bar up to open, the grip or header down to close ──
	interface Drag {
		id: number;
		x: number;
		y: number;
		kind: 'pending' | 'open' | 'close' | 'usage' | 'step' | 'none';
		cell: string | null;
		height: number;
		offset: number;
		tracker: VelocityTracker;
		/** The element that started the drag; it captures the pointer once the drag is real. */
		el: Element | null;
	}
	let drag: Drag | null = null;
	/** Panel offset (px from fully open) while a finger holds it; null otherwise. */
	let dragOffset = $state<number | null>(null);
	let dragHeight = $state(0);
	let suppressClick = false;

	function panelHeight(): number {
		return panelRef?.offsetHeight || Math.round(window.innerHeight * 0.86);
	}

	function startBarDrag(event: PointerEvent) {
		if (event.button !== 0 || open || drag) return;
		// The usage sheet lives inside the bar's DOM: pulling its content up must
		// not open Chat controls underneath it.
		if ((event.target as Element | null)?.closest?.('.usage-popover')) return;
		const cell = (event.target as Element | null)?.closest<HTMLElement>('[data-cell]')?.dataset.cell ?? null;
		drag = { id: event.pointerId, x: event.clientX, y: event.clientY, kind: 'pending', cell, height: 0, offset: 0, tracker: new VelocityTracker(), el: event.currentTarget as Element };
		suppressClick = false;
	}

	function startHeadDrag(event: PointerEvent) {
		if (event.button !== 0 || !open || drag) return;
		if ((event.target as Element | null)?.closest('button')) return;
		drag = { id: event.pointerId, x: event.clientX, y: event.clientY, kind: 'pending', cell: 'head', height: panelHeight(), offset: 0, tracker: new VelocityTracker(), el: event.currentTarget as Element };
	}

	// The usage sheet (the shared ContextUsageBar details, restyled below as a
	// bottom sheet) closes the same way: drag its grip down. Only the grip
	// starts the drag, as only the panel's header does, so its content scrolls.
	/** Sheet offset (px) while a finger holds its grip or it slides away; null at rest. */
	let usageDragOffset = $state<number | null>(null);
	let usageDragHeight = $state(0);
	/** A finger is on the grip: follow it with no easing. */
	let usageHeld = $state(false);
	/** Released past the threshold: sliding out before the details close. */
	let usageClosing = false;
	let usageCloseTimer: ReturnType<typeof setTimeout> | undefined;

	function usageSheetHeight(from: Element | null): number {
		return from?.closest<HTMLElement>('.usage-popover')?.offsetHeight || Math.round(window.innerHeight * 0.6);
	}

	function startUsageDrag(event: PointerEvent) {
		// Setting `drag` first also keeps the bar's own pull-up from starting.
		if (event.button !== 0 || drag || usageClosing) return;
		const el = event.currentTarget as Element;
		drag = { id: event.pointerId, x: event.clientX, y: event.clientY, kind: 'pending', cell: 'usage-grip', height: usageSheetHeight(el), offset: 0, tracker: new VelocityTracker(), el };
	}

	function dismissUsage(height: number) {
		const reduce = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		if (reduce) {
			usageDragOffset = null;
			usageBar?.closeDetails();
			return;
		}
		// Finish the slide the finger started, then close through the bar's own path.
		usageClosing = true;
		usageDragOffset = height + 40;
		clearTimeout(usageCloseTimer);
		usageCloseTimer = setTimeout(() => {
			usageBar?.closeDetails();
			usageDragOffset = null;
			usageClosing = false;
		}, 240);
	}

	$effect(() => () => clearTimeout(usageCloseTimer));

	function handleUsageOpenChange(next: boolean) {
		// A fresh open always starts at rest, even inside a closing slide.
		if (next) {
			clearTimeout(usageCloseTimer);
			usageClosing = false;
			usageDragOffset = null;
		}
		onUsageOpenChange?.(next);
	}

	/** The scrim fades as the sheet is pulled away, as the panel's does. */
	const usageFade = $derived(
		usageDragOffset !== null && usageDragHeight > 0 ? Math.max(0, Math.min(1, 1 - usageDragOffset / usageDragHeight)) : null
	);

	function handlePointerMove(event: PointerEvent) {
		const d = drag;
		if (!d || event.pointerId !== d.id) return;
		const dx = event.clientX - d.x;
		const dy = event.clientY - d.y;
		if (d.kind === 'pending') {
			const axis = dragAxis(dx, dy);
			if (!axis) return;
			if (d.cell === 'head') {
				d.kind = axis === 'vertical' ? 'close' : 'none';
			} else if (d.cell === 'usage-grip') {
				// Not 'usage': that is the quick bar's ring cell, which pulls the panel up.
				d.kind = axis === 'vertical' ? 'usage' : 'none';
			} else if (axis === 'vertical') {
				d.kind = dy < 0 ? 'open' : 'none';
			} else if (d.cell === 'effort' && !effortLocked) {
				d.kind = 'step';
			} else if (d.cell === 'access' && accessShown && !permissionUpdating) {
				d.kind = 'step';
			} else {
				d.kind = 'none';
			}
			if (d.kind === 'none') { drag = null; return; }
			suppressClick = true;
			closePicker();
			if (d.kind === 'open') {
				rendered = true;
				setPage('main');
				leaving = null;
				direct = false;
				d.height = panelHeight();
				dragHeight = d.height;
				void tick().then(() => { if (bodyRef) bodyRef.scrollTop = 0; });
			}
			try { d.el?.setPointerCapture?.(event.pointerId); } catch { /* synthetic pointer */ }
		}
		if (event.cancelable) event.preventDefault();
		if (d.kind === 'open') {
			if (!d.height) d.height = panelHeight();
			dragHeight = d.height;
			d.offset = Math.min(d.height, Math.max(0, d.height + dy));
			dragOffset = d.offset;
			d.tracker.add(event.clientY, event.timeStamp || performance.now());
		} else if (d.kind === 'close') {
			d.offset = dy < 0 ? dy * 0.15 : dy;
			dragOffset = d.offset;
			dragHeight = d.height;
			d.tracker.add(event.clientY, event.timeStamp || performance.now());
		} else if (d.kind === 'usage') {
			d.offset = dy < 0 ? dy * 0.15 : dy;
			usageHeld = true;
			usageDragHeight = d.height;
			usageDragOffset = d.offset;
			d.tracker.add(event.clientY, event.timeStamp || performance.now());
		} else if (d.kind === 'step') {
			const next = d.cell === 'effort'
				? stepValue(effortValues, effortShown, dx)
				: permissionPending ? null : stepValue(accessValues, accessValue, dx, BYPASS);
			const current = d.cell === 'effort' ? effortShown : accessValue;
			preview = next && next !== current ? { cell: d.cell as Picker, value: next } : null;
		}
	}

	function handlePointerEnd(event: PointerEvent) {
		const d = drag;
		if (!d || event.pointerId !== d.id) return;
		drag = null;
		const cancelled = event.type === 'pointercancel';
		if (d.kind === 'open') {
			const progress = d.height ? 1 - d.offset / d.height : 0;
			const keep = !cancelled && settlesOpen(progress, d.tracker.velocity());
			dragOffset = null;
			if (keep) openPanel('main');
			else if (!open) scheduleUnrender();
		} else if (d.kind === 'close') {
			const shut = !cancelled && settlesClosed(d.offset, d.height, d.tracker.velocity());
			dragOffset = null;
			if (shut) closePanel();
		} else if (d.kind === 'usage') {
			usageHeld = false;
			if (!cancelled && settlesClosed(d.offset, d.height, d.tracker.velocity())) dismissUsage(d.height);
			else usageDragOffset = null; // springs back through the sheet's transition
		} else if (d.kind === 'step') {
			const chosen = preview;
			preview = null;
			if (!cancelled && chosen) {
				if (chosen.cell === 'effort') onEffortSelect(chosen.value);
				else onPermissionSelect(chosen.value);
			}
		}
		if (suppressClick) setTimeout(() => { suppressClick = false; }, 0);
	}

	function scheduleUnrender() {
		clearTimeout(renderTimer);
		renderTimer = setTimeout(() => { if (!open && dragOffset === null) rendered = false; }, 400);
	}

	function guardClick(event: MouseEvent) {
		if (!suppressClick) return;
		event.preventDefault();
		event.stopPropagation();
		suppressClick = false;
	}

	$effect(() => {
		window.addEventListener('pointermove', handlePointerMove, { passive: false });
		window.addEventListener('pointerup', handlePointerEnd);
		window.addEventListener('pointercancel', handlePointerEnd);
		return () => {
			window.removeEventListener('pointermove', handlePointerMove);
			window.removeEventListener('pointerup', handlePointerEnd);
			window.removeEventListener('pointercancel', handlePointerEnd);
		};
	});

	/** translateY for the panel: the finger while dragging, else open or closed. */
	const panelShift = $derived(dragOffset !== null ? `${dragOffset}px` : open ? '0px' : 'calc(100% + 40px)');
	const scrimOpacity = $derived(dragOffset !== null && dragHeight > 0 ? Math.max(0, Math.min(1, 1 - dragOffset / dragHeight)) : open ? 1 : 0);
	const panelVisible = $derived(open || dragOffset !== null);

	// ── Panel actions ─────────────────────────────────────────────────────
	/** Fresh watch counts while the panel is up (the chat's list also tracks). */
	$effect(() => {
		if (open) return watches.track();
	});

	function toggleBrowser() {
		closePanel();
		onBrowserToggle?.();
	}

	function toggleTerminal() {
		closePanel();
		terminal?.toggle();
	}

	function toggleAway() {
		if (awayAvailable && !awayBusy) onToggleAway?.();
	}

	function pickPanelEffort(value: string) {
		if (value !== effortValue) onEffortSelect(value);
	}

	function pickPanelAccess(value: string) {
		if (value !== accessValue && !permissionUpdating) onPermissionSelect(value);
	}
</script>

<svelte:window onkeydown={handleKeydown} />

<!-- The quick bar: the composer's bottom edge on the phone. -->
<div class="mobile-composer-controls" inert={inertToolbar}>
	<!-- The cells are the tab stops; the bar itself takes the drag. -->
	<!-- svelte-ignore a11y_interactive_supports_focus -->
	<div
		bind:this={barRef}
		class="qbar"
		class:dragging={dragOffset !== null}
		role="toolbar"
		aria-label="Quick controls. Pull up for all controls."
		data-no-drawer-swipe
		onpointerdown={startBarDrag}
		onclickcapture={guardClick}
	>
		<span class="qbar-grip" aria-hidden="true"></span>

		<button
			type="button"
			class="qcell"
			class:locked={modelLocked}
			data-cell="model"
			aria-haspopup="dialog"
			aria-label={`Model: ${modelLabel}${fastShown && fastEnabled && !fastPending ? ', fast mode' : ''}${modelLocked ? ', locked' : ''}`}
			aria-disabled={modelLocked || undefined}
			onclick={() => { if (!modelLocked) openPanel('model'); }}
		>
			<span class="qi">
				{#if modelLocked}<Lock size={18} aria-hidden="true" />{:else}<Cpu size={20} aria-hidden="true" />{/if}
				{#if fastShown && fastEnabled && !fastPending}<span class="qmini" aria-hidden="true"><Zap size={10} fill="currentColor" /></span>{/if}
			</span>
			<span class="ql">{modelLabel}</span>
		</button>

		<button
			type="button"
			class="qcell"
			class:lit={!effortLocked}
			class:locked={effortLocked}
			class:stepping={preview?.cell === 'effort'}
			data-cell="effort"
			aria-haspopup="dialog"
			aria-expanded={picker === 'effort'}
			aria-label={effortA11y}
			onclick={() => togglePicker('effort')}
		>
			<span class="qi"><EffortMeter index={effortLocked && !effortKnown ? -1 : shownEffortIndex} count={Math.max(effortOptions.length, effortLocked ? 5 : 1)} /></span>
			<span class="ql">{shownEffortLabel}</span>
		</button>

		{#if accessShown}
			<button
				type="button"
				class="qcell"
				class:stepping={preview?.cell === 'access'}
				data-cell="access"
				aria-haspopup="dialog"
				aria-expanded={picker === 'access'}
				aria-busy={permissionUpdating || permissionPending || undefined}
				aria-label={`Tool access: ${shownAccess ? (ACCESS_LABEL[shownAccess] ?? shownAccess) : 'loading'}${permissionUpdating ? ', saving' : ''}${awayOn ? ', Away mode on' : ''}`}
				onclick={() => togglePicker('access')}
			>
				<span class="qi">
					{#if permissionUpdating || (permissionPending && !shownAccess)}<LoaderCircle size={20} class="mcc-spin" aria-hidden="true" />{:else}<AccessIcon size={20} aria-hidden="true" />{/if}
					{#if awayOn}<span class="qmini" data-away-badge aria-hidden="true"><Moon size={10} fill="currentColor" /></span>{/if}
				</span>
				<span class="ql">{shownAccess ? (ACCESS_SHORT[shownAccess] ?? shownAccess) : 'Access'}</span>
			</button>
		{/if}

		<div
			class="qcell usage"
			class:held={usageHeld}
			class:moved={usageDragOffset !== null}
			data-cell="usage"
			style:--usage-drag={usageDragOffset !== null ? `${usageDragOffset}px` : undefined}
			style:--usage-fade={usageFade ?? undefined}
		>
			<ContextUsageBar bind:this={usageBar} bind:percent={usagePercent} bind:overLimit={usageOverLimit} bind:unknownWindowUsed={usageUnknownUsed} bind:usedTokens={usageUsedTokens} bind:windowTokens={usageWindowTokens} onOpenChange={handleUsageOpenChange} showPercent>
				{#snippet grip()}
					<!-- Pointer-only: Escape and a tap outside close the sheet for everyone. -->
					<!-- svelte-ignore a11y_no_static_element_interactions -->
					<div class="usage-grip" aria-hidden="true" onpointerdown={startUsageDrag}><span></span></div>
				{/snippet}
			</ContextUsageBar>
		</div>

		<button
			bind:this={controlsCellRef}
			type="button"
			class="qcell"
			data-cell="controls"
			aria-haspopup="dialog"
			aria-controls={panelId}
			aria-expanded={open}
			aria-label={`All chat controls${runningTasks.length ? `, ${runningTasks.length} running` : ''}`}
			onclick={() => (open ? closePanel() : openPanel('main'))}
		>
			<span class="qi">
				<SlidersHorizontal size={20} aria-hidden="true" />
				{#if runningTasks.length}<span class="qcount" aria-hidden="true">{runningTasks.length}</span>{/if}
			</span>
			<span class="ql">Controls</span>
		</button>
	</div>

	{#if picker}
		<div
			bind:this={pickerRef}
			class="qpick"
			role="dialog"
			aria-label={picker === 'effort' ? 'Reasoning effort' : 'Tool access'}
			data-composer-owned-overlay="true"
			data-no-drawer-swipe
		>
			{#if picker === 'effort'}
				<div class="qp-h"><span>Reasoning effort</span></div>
				<div class="segs" role="radiogroup" aria-label="Reasoning effort" style:--n={effortOptions.length}>
					{#each effortOptions as option, i (option.value)}
						<button type="button" class="seg" role="radio" aria-checked={option.value === effortValue}
							title={option.description} onclick={() => pickEffort(option.value)}>
							<EffortMeter index={i} count={effortOptions.length} size={18} />
							<span>{option.label}</span>
						</button>
					{/each}
				</div>
			{:else}
				<div class="qp-h">
					<span>Tool access</span>
					<span class="qp-help" aria-live="polite">
						{#if permissionUpdating}<LoaderCircle size={13} class="mcc-spin" aria-hidden="true" />Saving{:else}{ACCESS_HELP[accessValue] ?? ''}{/if}
					</span>
				</div>
				<div class="segs" role="radiogroup" aria-label="Tool access" aria-busy={permissionUpdating} style:--n={permissionOptions.length}>
					{#each permissionOptions as option (option.value)}
						{@const Icon = accessIcons[option.value] ?? Hand}
						<button type="button" class="seg" role="radio"
							aria-checked={option.value === accessValue} disabled={permissionUpdating}
							onclick={() => pickAccess(option.value)}>
							<Icon size={18} aria-hidden="true" />
							<span>{ACCESS_LABEL[option.value] ?? option.label}</span>
						</button>
					{/each}
				</div>
			{/if}
		</div>
	{/if}
</div>

<!-- The full panel, portaled to <body>. -->
<div class="mcp-portal" use:portal data-composer-owned-overlay="true" data-no-drawer-swipe>
	{#if panelVisible || rendered}
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div class="mcp-scrim" class:live={panelVisible} class:dragging={dragOffset !== null} style:opacity={scrimOpacity} onclick={closePanel}></div>
	{/if}
	<div
		id={panelId}
		bind:this={panelRef}
		class="mcp"
		class:dragging={dragOffset !== null}
		class:shown={panelVisible}
		class:sub={onSubPage}
		role="dialog"
		aria-modal="true"
		aria-label={PAGE_TITLE[pageName]}
		aria-hidden={!panelVisible}
		inert={!open}
		tabindex="-1"
		style:transform={`translateY(${panelShift})`}
	>
		<!-- Grip and title: dragging either down closes the sheet from any page. -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<header class="mcp-head" onpointerdown={startHeadDrag}>
			<span class="mcp-grab" aria-hidden="true"></span>
			<div class="mcp-head-row">
				<button bind:this={backRef} type="button" class="mcp-back" aria-label="Back to chat controls" tabindex={onSubPage ? 0 : -1}
					aria-hidden={!onSubPage} onclick={() => backToMain()}><ChevronLeft size={24} strokeWidth={2.4} aria-hidden="true" /></button>
				<h2 class="mcp-title" aria-live="polite">{PAGE_TITLE[pageName]}</h2>
				<button type="button" class="mcp-close" onclick={closePanel}>Close chat controls</button>
			</div>
		</header>

		<div class="mcp-pages">
			<!-- ═══ Main: the inset grouped list ═══ -->
			<section class="mcp-page" data-page="main" class:active={!onSubPage} class:under={onSubPage} inert={onSubPage} aria-label="Chat controls">
				<div class="mcp-scroll" bind:this={bodyRef}>
					{#if rendered || panelVisible}
						{#if showRunChip || workerName || projectName}
							<div class="mcp-status">
								{#if showRunChip}
									<button type="button" class="mcp-chip run" class:quiet={!runningTasks.length}
										aria-label={`${runHead}: ${runSummary}. Open the list`}
										onclick={(e) => goTo('tasks', e.currentTarget)}>
										<i aria-hidden="true"></i>{runChipLabel}
									</button>
								{/if}
								{#if workerName}<span class="mcp-chip" title="Runs on">{workerName}</span>{/if}
								{#if projectName}<span class="mcp-chip" title="Project and branch">{projectName}{#if branchName}<span class="mcp-chip-sep" aria-hidden="true">·</span>{branchName}{/if}</span>{/if}
							</div>
						{/if}

						<h3 class="mcp-glabel">Model</h3>
						<div class="mcp-group">
							<button type="button" class="mcp-row" disabled={modelLocked} aria-label={`Model: ${modelLabel}${modelLocked ? ', locked' : ''}`}
								onclick={(e) => goTo('model', e.currentTarget)}>
								<span class="mcp-ic glyph" aria-hidden="true"><ProviderGlyph kind={modelGlyph} size={17} /></span>
								<span class="mcp-lbl"><span>Model</span></span>
								<span class="mcp-val">{modelLabel}</span>
								{#if modelLocked}<Lock size={16} class="mcp-chev" aria-hidden="true" />{:else}<ChevronRight size={18} class="mcp-chev" aria-hidden="true" />{/if}
							</button>
							<button type="button" class="mcp-row" onclick={(e) => goTo('usage', e.currentTarget)}
								aria-label={`Context and usage: ${usageUnknownUsed != null ? `${usageUnknownUsed} tokens used` : usageOverLimit ? 'over the limit' : `${usagePercent}% used`}`}>
								<svg class="mcp-ring {usageSeverity}" class:unknown={usageUnknownUsed != null} width="30" height="30" viewBox="0 0 30 30" aria-hidden="true">
									<circle class="ring-track" cx="15" cy="15" r="11" fill="none" stroke-width="3.5" />
									{#if usageUnknownUsed == null}
										<circle class="ring-progress" cx="15" cy="15" r="11" fill="none" stroke-width="3.5" stroke-linecap="round"
											stroke-dasharray={RING_C} stroke-dashoffset={RING_C * (1 - Math.min(100, Math.max(0, usagePercent)) / 100)} />
									{/if}
								</svg>
								<span class="mcp-lbl"><span>Context and usage</span>{#if usageTokensLine}<small>{usageTokensLine}</small>{/if}</span>
								<span class="mcp-val" class:warn={usageSeverity === 'warning'} class:danger={usageSeverity === 'danger' || usageOverLimit}>
									{usageUnknownUsed != null ? '—' : usageOverLimit ? 'Over' : `${usagePercent}%`}
								</span>
								<ChevronRight size={18} class="mcp-chev" aria-hidden="true" />
							</button>
							<div class="mcp-stack" data-row="effort">
								<div class="mcp-rowhead">
									<span class="mcp-ic" aria-hidden="true"><Lightbulb size={17} /></span>
									<span class="mcp-lbl"><span>Reasoning effort</span>{#if effortLocked && effortExplanation && effortExplanation !== panelEffortValue}<small>{effortExplanation}</small>{/if}</span>
									<span class="mcp-val">{panelEffortValue}</span>
								</div>
								{#if !effortLocked}
									<div class="mcp-seg accent" role="radiogroup" aria-label="Reasoning effort" style:--n={effortOptions.length}>
										{#each effortOptions as option (option.value)}
											<button type="button" role="radio" aria-checked={option.value === effortValue}
												class:auto={!effortValue && option.value === effortDefaultValue}
												title={option.description} onclick={() => pickPanelEffort(option.value)}>{option.label}</button>
										{/each}
									</div>
								{/if}
							</div>
							{#if fastShown}
								<button type="button" class="mcp-row" role="switch" aria-label={fastPending ? 'Fast mode: loading' : 'Fast mode'}
									aria-checked={fastPending ? false : fastEnabled} aria-busy={fastPending || undefined} disabled={fastPending}
									onclick={() => { if (!fastPending) onToggleFast?.(); }}>
									<span class="mcp-ic" aria-hidden="true"><Zap size={17} /></span>
									<span class="mcp-lbl"><span>Fast mode</span><small>Higher price</small></span>
									{#if fastPending}<LoaderCircle size={18} class="mcc-spin mcp-chev" aria-hidden="true" />{:else}<span class="mcp-sw" aria-hidden="true"></span>{/if}
								</button>
							{/if}
						</div>
						{#if modelNotice}<p class="mcp-note warn">{modelNotice}</p>{/if}

						{#if accessShown}
							<h3 class="mcp-glabel">Permissions</h3>
							<div class="mcp-group">
								<div class="mcp-stack" data-row="access">
									<div class="mcp-rowhead">
										<span class="mcp-ic" aria-hidden="true">{#if accessValue === BYPASS}<ShieldOff size={17} />{:else}<ShieldCheck size={17} />{/if}</span>
										<span class="mcp-lbl"><span>Tool access</span></span>
										<span class="mcp-val" class:danger={accessValue === BYPASS}>{accessValue ? (ACCESS_LABEL[accessValue] ?? accessValue) : 'Loading…'}</span>
									</div>
									<div class="mcp-seg" role="radiogroup" aria-label="Tool access" aria-busy={permissionUpdating} style:--n={permissionOptions.length}>
										{#each permissionOptions as option (option.value)}
											<button type="button" role="radio" class:danger={option.value === BYPASS}
												aria-checked={option.value === accessValue} aria-label={ACCESS_LABEL[option.value] ?? option.label}
												disabled={permissionUpdating} onclick={() => pickPanelAccess(option.value)}>{ACCESS_SHORT[option.value] ?? option.label}</button>
										{/each}
									</div>
									{#if permissionUpdating || ACCESS_HELP[accessValue]}
										<p class="mcp-help access-help" class:danger={!permissionUpdating && accessValue === BYPASS} aria-live="polite">
											{#if permissionUpdating}<LoaderCircle size={13} class="mcc-spin" aria-hidden="true" />Saving{:else}{ACCESS_HELP[accessValue]}{/if}
										</p>
									{/if}
								</div>
								{#if awayShown}
									<button type="button" class="mcp-row" role="switch" aria-label="Away mode" aria-checked={awayEnabled && awayAvailable}
										aria-disabled={!awayAvailable || awayBusy || undefined} onclick={toggleAway}>
										<span class="mcp-ic" aria-hidden="true"><Moon size={17} /></span>
										<span class="mcp-lbl"><span>Away mode</span><small>{awayAvailable ? 'Takes recommended defaults' : 'Available after the first message'}</small></span>
										{#if awayBusy}<LoaderCircle size={18} class="mcc-spin mcp-chev" aria-hidden="true" />{:else}<span class="mcp-sw" aria-hidden="true"></span>{/if}
									</button>
								{/if}
							</div>
						{/if}

						{#if showSetup && rendered}
							<h3 class="mcp-glabel">Setup</h3>
							<div class="mcp-group mcp-setup"><ContextPillBar sheetMode {menuHosts} /></div>
						{/if}
					{/if}
				</div>

				<footer class="mcp-foot">
					<div class="actions" role="group" aria-label="More actions">
						{#if callAction}
							<div class="act call-act">{@render callAction()}<span class="act-label" aria-hidden="true">Call</span></div>
						{/if}
						{#if onBrowserToggle}
							<button type="button" class="act" aria-pressed={browserOpen} onpointerenter={onBrowserPreload} onfocus={onBrowserPreload} onclick={toggleBrowser} aria-label="Live browser view">
								<Globe size={18} aria-hidden="true" /><span class="act-label">Browser</span>
							</button>
						{/if}
						{#if terminal}
							<button type="button" class="act" aria-pressed={terminal.open} onpointerenter={terminal.preload} onfocus={terminal.preload}
								onclick={toggleTerminal} aria-label="Terminal" title={terminal.disabledReason ?? undefined}>
								<SquareTerminal size={18} aria-hidden="true" /><span class="act-label">Terminal</span>
							</button>
						{/if}
						<button type="button" class="act" disabled={!canExport} onclick={(e) => goTo('export', e.currentTarget)} aria-label="Export chat">
							<Download size={18} aria-hidden="true" /><span class="act-label">Export</span>
						</button>
					</div>
				</footer>
			</section>

			<!-- ═══ Sub-pages: slide in from the right ═══ -->
			<section class="mcp-page" data-page="model" class:active={pageName === 'model'} inert={pageName !== 'model'} aria-label="Model">
				{#if mounted('model')}
					<div class="mcp-model">
						<ModelPickerPanel
							groups={railGroups}
							options={modelOptions}
							value={modelValue}
							notice={modelNotice}
							{contextChoice}
							{onContextChoiceSelect}
							{contextStandardTokens}
							{onContextStandardChange}
							onSelect={(value) => { onModelSelect(value); back(); }}
							onRequestClose={back}
						/>
					</div>
				{/if}
			</section>

			<section class="mcp-page" data-page="usage" class:active={pageName === 'usage'} inert={pageName !== 'usage'} aria-label="Context and usage">
				<div class="mcp-scroll">
					{#if mounted('usage')}
						<div class="mcp-usage"><ContextUsageBar inline /></div>
					{/if}
				</div>
			</section>

			<section class="mcp-page" data-page="agent" class:active={pageName === 'agent'} inert={pageName !== 'agent'} aria-label="Agent">
				<div class="mcp-scroll">
					<div class="mcp-host agent-host" bind:this={agentHost}></div>
				</div>
			</section>

			<section class="mcp-page" data-page="runner" class:active={pageName === 'runner'} inert={pageName !== 'runner'} aria-label="Runs on">
				<div class="mcp-scroll">
					<div class="mcp-host runner-host" bind:this={runnerHost}></div>
				</div>
			</section>

			<section class="mcp-page" data-page="tasks" class:active={pageName === 'tasks'} inert={pageName !== 'tasks'} aria-label="Running tasks">
				{#if mounted('tasks')}
					<RunningTasksList sessionId={$activeSession.sessionId} variant="page" onClose={closePanel} />
				{/if}
			</section>

			<section class="mcp-page" data-page="export" class:active={pageName === 'export'} inert={pageName !== 'export'} aria-label="Export chat">
				{#if mounted('export') && $activeSession.sessionId}
					<ChatExportActions sessionId={$activeSession.sessionId} page />
				{/if}
			</section>
		</div>
	</div>
</div>

<style>
	/* ── Quick bar ───────────────────────────────────────────────────── */
	.mobile-composer-controls { position: relative; display: none; min-width: 0; }
	:global(html[data-platform='mobile']) .mobile-composer-controls { display: block; }
	.qbar {
		position: relative;
		display: flex;
		gap: 1px;
		padding: 12px 2px 4px;
		border-top: 1px solid var(--border-subtle);
		touch-action: none;
		cursor: grab;
		user-select: none;
		-webkit-user-select: none;
	}
	.qbar-grip {
		position: absolute;
		top: 4px;
		left: 50%;
		width: 32px;
		height: 4px;
		margin-left: -16px;
		border-radius: 2px;
		background: var(--border-strong);
		transition: background 0.2s, width 0.2s, margin 0.2s;
	}
	.qbar:hover .qbar-grip, .qbar.dragging .qbar-grip { width: 44px; margin-left: -22px; background: var(--muted-foreground); }
	.qcell {
		position: relative;
		flex: 1 1 0;
		min-width: 0;
		min-height: 52px;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 3px;
		padding: 5px 1px 4px;
		border: 0;
		border-radius: var(--radius);
		background: transparent;
		color: var(--foreground);
		font: inherit;
		cursor: pointer;
		touch-action: none;
		-webkit-tap-highlight-color: transparent;
		transition: background-color 0.15s, transform 0.15s;
	}
	.qcell:hover { background: var(--hover-overlay); }
	.qcell:active { background: var(--active-overlay); }
	.qcell[aria-expanded='true'] { background: var(--muted); }
	.qi { position: relative; display: grid; place-items: center; height: 20px; color: var(--muted-foreground); }
	.ql {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 11.5px;
		font-weight: 550;
		line-height: 1.2;
		font-variant-numeric: tabular-nums;
	}
	.qcell.lit .qi { color: var(--primary); }
	.qcell.locked .qi, .qcell.locked .ql { color: var(--subtle-foreground); }
	.qcell.stepping { background: var(--muted); transform: scale(1.04); }
	.qmini { position: absolute; right: -9px; bottom: -3px; display: grid; place-items: center; width: 14px; height: 14px; border-radius: 50%; background: var(--card); color: var(--primary); }
	.qcount {
		position: absolute;
		top: -6px;
		right: -12px;
		display: grid;
		place-items: center;
		min-width: 18px;
		height: 18px;
		padding: 0 5px;
		border-radius: 9px;
		background: var(--primary);
		color: var(--primary-foreground);
		font-size: 11px;
		font-weight: 700;
	}
	.qcell:focus-visible, .seg:focus-visible, .bar:focus-visible, .tile:focus-visible, .act:focus-visible, .mcp-icon-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }

	/* The usage cell hosts the shared ring button, stretched over the cell. */
	.qcell.usage { cursor: default; padding: 0; }
	.qcell.usage :global(.usage-context-wrapper) { position: static; display: contents; }
	.qcell.usage :global(.usage-indicator) {
		width: 100%;
		height: 100%;
		min-height: 52px;
		margin: 0;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 3px;
		border-radius: var(--radius);
		color: var(--foreground);
	}
	.qcell.usage :global(.usage-ring) { width: 20px; height: 20px; }
	.qcell.usage :global(.usage-percent) { font-size: 11.5px; font-weight: 550; line-height: 1.2; font-variant-numeric: tabular-nums; }
	/* The shared usage details, as a bottom sheet over a scrim. */
	.qcell.usage :global(.usage-popover) {
		position: fixed;
		left: 8px;
		right: 8px;
		top: auto;
		bottom: calc(12px + env(safe-area-inset-bottom, 0px));
		z-index: 130;
		width: auto;
		min-width: 0;
		max-width: none;
		max-height: calc(var(--app-viewport-height, 100dvh) - 120px);
		/* No top padding: the grip strip is the sheet's top edge. */
		padding: 0 16px 16px;
		border-radius: calc(var(--radius) * 2);
		transform: none !important;
		/* The drag offset, separate from the desktop popover's own transform. */
		translate: 0 var(--usage-drag, 0px);
		box-shadow: var(--shadow-l), 0 0 0 100vmax var(--modal-overlay, color-mix(in oklch, var(--background) 60%, transparent));
		transition: translate 0.3s cubic-bezier(0.22, 1, 0.36, 1), box-shadow 0.3s ease;
		animation: mcc-sheet 0.3s cubic-bezier(0.22, 1, 0.36, 1);
	}
	@keyframes mcc-sheet { from { translate: 0 calc(100% + 40px); } }
	.qcell.usage.moved :global(.usage-popover) {
		box-shadow: var(--shadow-l), 0 0 0 100vmax color-mix(in oklch, var(--modal-overlay, color-mix(in oklch, var(--background) 60%, transparent)) calc(var(--usage-fade, 1) * 100%), transparent);
	}
	/* Transition only: changing `animation` here would replay the open slide on release. */
	.qcell.usage.held :global(.usage-popover) { transition: none; }
	/* The grip: the quick bar's own handle, on a strip that stays put while the
	   details scroll beneath it. Only this strip starts the drag-down. */
	.usage-grip {
		position: sticky;
		top: 0;
		z-index: 1;
		flex-shrink: 0;
		/* A 32px touch strip; the negative margin cancels the sheet's row gap. */
		height: 32px;
		margin: 0 -16px -10px;
		background: inherit;
		border-radius: calc(var(--radius) * 2) calc(var(--radius) * 2) 0 0;
		touch-action: none;
		cursor: grab;
		-webkit-tap-highlight-color: transparent;
	}
	.usage-grip span {
		position: absolute;
		top: 10px;
		left: 50%;
		width: 32px;
		height: 4px;
		margin-left: -16px;
		border-radius: 2px;
		background: var(--border-strong);
		transition: background 0.2s, width 0.2s, margin 0.2s;
	}
	.usage-grip:hover span, .qcell.usage.held .usage-grip span { width: 44px; margin-left: -22px; background: var(--muted-foreground); }
	.qcell.usage.held .usage-grip { cursor: grabbing; }

	/* ── Compact picker over the bar ─────────────────────────────────── */
	.qpick {
		position: absolute;
		left: 2px;
		right: 2px;
		bottom: calc(100% + 8px);
		z-index: 70;
		padding: 10px;
		border: 1px solid var(--border-strong);
		border-radius: calc(var(--radius) * 1.6);
		background: var(--popover);
		box-shadow: var(--shadow-l);
		transform-origin: bottom center;
		animation: mcc-pop 0.22s cubic-bezier(0.22, 1, 0.36, 1);
	}
	@keyframes mcc-pop { from { opacity: 0; transform: translateY(10px) scale(0.98); } }
	.qp-h { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin: 0 2px 8px; font-size: 12.5px; font-weight: 600; color: var(--muted-foreground); }
	.qp-help { display: flex; align-items: center; gap: 6px; font-weight: 450; }

	.segs {
		display: grid;
		grid-template-columns: repeat(var(--n, 4), minmax(0, 1fr));
		gap: 2px;
		padding: 3px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius);
		background: var(--background);
	}
	.seg {
		min-width: 0;
		min-height: 56px;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 4px;
		padding: 4px 2px;
		border: 0;
		border-radius: calc(var(--radius) - 2px);
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12px;
		line-height: 1.15;
		text-align: center;
		cursor: pointer;
	}
	.seg:hover { background: var(--hover-overlay); }
	.seg[aria-checked='true'] {
		background: color-mix(in oklab, var(--primary) 13%, var(--card));
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--primary) 40%, transparent);
		color: var(--primary);
		font-weight: 600;
	}
	.segs[aria-busy='true'] .seg { cursor: wait; opacity: 0.6; }

	/* ── Panel ───────────────────────────────────────────────────────── */
	.mcp-scrim {
		position: fixed;
		inset: 0;
		z-index: 120;
		background: var(--modal-overlay, color-mix(in oklch, var(--background) 60%, transparent));
		opacity: 0;
		pointer-events: none;
		transition: opacity 0.3s ease;
	}
	.mcp-scrim.live { pointer-events: auto; }
	.mcp-scrim.dragging { transition: none; }
	.mcp {
		--mcp-ease: cubic-bezier(0.22, 1, 0.36, 1);
		--mcp-row-radius: calc(var(--radius) * 1.15);
		position: fixed;
		left: 0;
		right: 0;
		bottom: 0;
		top: max(48px, calc(env(safe-area-inset-top, 0px) + 48px));
		z-index: 121;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		background: var(--popover);
		color: var(--foreground);
		border-top: 1px solid var(--border-strong);
		border-radius: calc(var(--radius) * 2) calc(var(--radius) * 2) 0 0;
		box-shadow: var(--shadow-l);
		outline: none;
		visibility: hidden;
		transition: transform 0.36s var(--mcp-ease), visibility 0s linear 0.36s;
		will-change: transform;
	}
	.mcp.shown { visibility: visible; transition: transform 0.36s var(--mcp-ease), visibility 0s; }
	.mcp.dragging { transition: none; }

	/* Header: the grip, then one row with the title centred. */
	.mcp-head { flex-shrink: 0; padding: 8px 12px 6px; touch-action: none; cursor: grab; user-select: none; -webkit-user-select: none; }
	.mcp-grab { display: block; width: 38px; height: 5px; margin: 0 auto 6px; border-radius: 3px; background: var(--border-strong); transition: width 0.15s, background-color 0.15s; }
	.mcp.dragging .mcp-grab, .mcp-head:hover .mcp-grab { width: 48px; background: var(--muted-foreground); }
	.mcp.dragging .mcp-head { cursor: grabbing; }
	.mcp-head-row { position: relative; display: grid; grid-template-columns: 44px minmax(0, 1fr) 44px; align-items: center; min-height: 44px; }
	.mcp-title {
		grid-column: 2;
		margin: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		text-align: center;
		font-size: 17px;
		font-weight: 650;
		letter-spacing: -0.01em;
	}
	.mcp-back {
		grid-column: 1;
		grid-row: 1;
		display: grid;
		place-items: center;
		width: 44px;
		height: 44px;
		margin-left: -6px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: transparent;
		color: var(--foreground);
		cursor: pointer;
		opacity: 0;
		pointer-events: none;
		transform: translateX(6px);
		transition: opacity 0.2s ease, transform 0.3s var(--mcp-ease), background-color 0.15s;
	}
	.mcp.sub .mcp-back { opacity: 1; pointer-events: auto; transform: none; }
	.mcp-back:hover { background: var(--hover-overlay); }
	.mcp-back:active { background: var(--active-overlay); }
	/* Close: visually hidden until keyboard focus reaches it. */
	.mcp-close {
		position: absolute;
		top: 50%;
		right: 0;
		width: 1px;
		height: 1px;
		padding: 0;
		overflow: hidden;
		clip-path: inset(50%);
		white-space: nowrap;
		border: 0;
		background: var(--muted);
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		font-weight: 550;
		cursor: pointer;
	}
	.mcp-close:focus-visible {
		width: auto;
		height: 32px;
		padding: 0 12px;
		margin-top: -16px;
		clip-path: none;
		border-radius: 999px;
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* Pages: the active one in place, the main list shifted left and dimmed
	   under a sub-page, the rest waiting off to the right. */
	.mcp-pages { position: relative; flex: 1; min-height: 0; overflow: hidden; }
	.mcp-page {
		position: absolute;
		inset: 0;
		display: flex;
		flex-direction: column;
		background: var(--popover);
		transform: translateX(100%);
		transition: transform 0.32s var(--mcp-ease), opacity 0.32s var(--mcp-ease);
	}
	.mcp-page.active { transform: none; }
	.mcp-page.under { transform: translateX(-28%); opacity: 0.4; }
	.mcp-page:not(.active):not(.under) { visibility: hidden; transition: transform 0.32s var(--mcp-ease), opacity 0.32s var(--mcp-ease), visibility 0s linear 0.32s; }
	.mcp-scroll {
		flex: 1;
		min-height: 0;
		padding: 4px 14px 18px;
		overflow-x: hidden;
		overflow-y: auto;
		overscroll-behavior: contain;
		scrollbar-width: none;
		-webkit-overflow-scrolling: touch;
	}
	.mcp-scroll::-webkit-scrollbar { display: none; }

	/* Status chips */
	.mcp-status { display: flex; flex-wrap: wrap; gap: 6px; padding: 2px 4px 14px; }
	.mcp-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		max-width: 100%;
		min-height: 28px;
		padding: 4px 11px;
		overflow: hidden;
		border: 0;
		border-radius: 999px;
		background: var(--muted);
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 500;
		line-height: 1.2;
		white-space: nowrap;
		text-overflow: ellipsis;
	}
	.mcp-chip-sep { opacity: 0.6; }
	button.mcp-chip { cursor: pointer; }
	.mcp-chip.run { color: var(--primary); background: color-mix(in oklab, var(--primary) 14%, transparent); font-weight: 600; }
	.mcp-chip.run:active { background: color-mix(in oklab, var(--primary) 22%, transparent); }
	.mcp-chip.run i { width: 6px; height: 6px; flex: none; border-radius: 50%; background: currentColor; animation: mcp-pulse 1.6s ease-in-out infinite; }
	.mcp-chip.run.quiet i { animation: none; opacity: 0.6; }
	@keyframes mcp-pulse { 50% { opacity: 0.3; } }

	/* Grouped list */
	.mcp-glabel { margin: 0; padding: 4px 8px 7px; font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted-foreground); }
	.mcp-group { margin-bottom: 18px; overflow: hidden; border: 1px solid var(--border); border-radius: var(--mcp-row-radius); background: var(--card); }
	.mcp-group + .mcp-note { margin-top: -10px; }
	.mcp-row {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		min-height: 52px;
		padding: 10px 14px;
		border: 0;
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 15px;
		text-align: left;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
		transition: background-color 0.15s;
	}
	.mcp-row:hover:not(:disabled):not([aria-disabled='true']) { background: var(--hover-overlay); }
	.mcp-row:active:not(:disabled):not([aria-disabled='true']) { background: var(--muted); }
	.mcp-row:disabled { cursor: default; }
	.mcp-group > * + * { border-top: 1px solid var(--border); }
	.mcp-ic {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		flex: none;
		border-radius: calc(var(--radius) * 0.65);
		background: var(--muted);
		color: var(--foreground);
	}
	.mcp-lbl { flex: 1; min-width: 0; }
	.mcp-lbl > span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.mcp-lbl small { display: block; margin-top: 1px; font-size: 12.5px; color: var(--muted-foreground); line-height: 1.3; }
	.mcp-val { max-width: 48%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 14.5px; color: var(--muted-foreground); font-variant-numeric: tabular-nums; }
	.mcp-val.warn { color: var(--warning); }
	.mcp-val.danger { color: var(--destructive); }
	.mcp-scroll :global(.mcp-chev) { flex: none; color: var(--muted-foreground); opacity: 0.75; }
	.mcp-row:disabled .mcp-val { color: var(--subtle-foreground, var(--muted-foreground)); }

	/* Usage ring */
	.mcp-ring { display: block; flex: none; transform: rotate(-90deg); }
	.mcp-ring .ring-track { stroke: var(--border-strong); }
	.mcp-ring .ring-progress { stroke: var(--primary); transition: stroke-dashoffset 0.3s ease; }
	.mcp-ring.warning .ring-progress { stroke: var(--warning); }
	.mcp-ring.danger .ring-progress { stroke: var(--destructive); }
	.mcp-ring.unknown .ring-track { stroke-dasharray: 3 2.7; }

	/* Switch rows */
	.mcp-sw { position: relative; width: 46px; height: 28px; flex: none; border-radius: 999px; background: var(--border-strong); transition: background-color 0.2s; }
	.mcp-sw::after {
		content: '';
		position: absolute;
		top: 3px;
		left: 3px;
		width: 22px;
		height: 22px;
		border-radius: 50%;
		background: var(--background);
		box-shadow: 0 1px 3px color-mix(in oklch, var(--foreground) 35%, transparent);
		transition: transform 0.22s var(--mcp-ease);
	}
	.mcp-row[aria-checked='true'] .mcp-sw { background: var(--primary); }
	.mcp-row[aria-checked='true'] .mcp-sw::after { transform: translateX(18px); background: var(--primary-foreground); }
	.mcp-row[aria-disabled='true'] { cursor: default; }
	.mcp-row[aria-disabled='true'] .mcp-lbl > span, .mcp-row[aria-disabled='true'] .mcp-ic { opacity: 0.5; }
	.mcp-row[aria-disabled='true'] .mcp-sw { opacity: 0.4; }

	/* Stacked rows: label over a segmented control */
	.mcp-stack { padding: 12px 14px 14px; }
	.mcp-rowhead { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; font-size: 15px; }
	.mcp-seg {
		display: grid;
		grid-template-columns: repeat(var(--n, 4), minmax(0, 1fr));
		gap: 3px;
		padding: 3px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) * 0.9);
		background: var(--background);
	}
	.mcp-seg button {
		min-width: 0;
		min-height: 38px;
		padding: 4px 2px;
		overflow: hidden;
		border: 0;
		border-radius: calc(var(--radius) * 0.65);
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 13.5px;
		font-weight: 500;
		line-height: 1.15;
		text-overflow: ellipsis;
		white-space: nowrap;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
		transition: background-color 0.15s, color 0.15s, box-shadow 0.15s;
	}
	.mcp-seg button:hover:not(:disabled) { color: var(--foreground); }
	.mcp-seg button[aria-checked='true'] {
		background: var(--muted);
		color: var(--foreground);
		font-weight: 600;
		box-shadow: inset 0 0 0 1px var(--border-strong);
	}
	.mcp-seg.accent button[aria-checked='true'] {
		background: color-mix(in oklab, var(--primary) 20%, var(--muted));
		box-shadow: inset 0 0 0 1px color-mix(in oklab, var(--primary) 55%, transparent);
	}
	/* No pinned level: the model's default is marked, not selected. */
	.mcp-seg button.auto { box-shadow: inset 0 0 0 1px color-mix(in oklab, var(--primary) 40%, transparent); color: var(--foreground); }
	.mcp-seg button.danger[aria-checked='true'] {
		background: color-mix(in oklab, var(--destructive) 18%, var(--muted));
		color: var(--destructive);
		box-shadow: inset 0 0 0 1px color-mix(in oklab, var(--destructive) 50%, transparent);
	}
	.mcp-seg[aria-busy='true'] button { cursor: wait; opacity: 0.6; }
	.mcp-help { display: flex; align-items: center; gap: 6px; margin: 9px 2px 0; font-size: 12.5px; color: var(--muted-foreground); }
	.mcp-help.danger { color: var(--destructive); }
	.mcp-note { margin: -8px 8px 18px; font-size: 12.5px; color: var(--muted-foreground); }
	.mcp-note.warn { color: var(--warning); }

	/* Focus */
	.mcp-row:focus-visible, .mcp-seg button:focus-visible, .mcp-chip:focus-visible, .mcp-back:focus-visible, .act:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.mcp-back:focus-visible, .mcp-chip:focus-visible { outline-offset: 2px; }

	/* Setup rows: the shared ContextPillBar selectors (sheet mode), drawn as
	   list rows. The labels are generated so the selectors stay the shared
	   components; their own aria-labels already name each one. Agent and
	   "Runs on" open their menus on their own pages (menuHost). */
	.mcp-setup { padding: 0; }
	.mcp-setup :global(.context-pill-bar.sheet-mode) { display: flex; flex-direction: column; gap: 0; margin: -1px 0 0; }
	.mcp-setup :global(.sheet-mode .ctx-sep) { display: none; }
	.mcp-setup :global(.sheet-mode :is(.selector-wrapper, .runner-badge-wrap, .execution-mode-bar, .mode-bar-row, .branch-wt-row, .mode-selector, .branch-selector, .workspace-folder-selector)) {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		width: 100%;
		gap: 0;
		margin: 0;
		padding: 0;
	}
	.mcp-setup :global(.sheet-mode .runner-badge-wrap) { flex-direction: row; flex-wrap: wrap; align-items: center; }
	.mcp-setup :global(.sheet-mode .ctx-chip) {
		position: relative;
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		height: auto;
		min-height: 52px;
		margin: 0;
		padding: 10px 14px;
		border: 0;
		border-top: 1px solid var(--border);
		border-radius: 0;
		background: transparent;
		color: var(--foreground);
		font-size: 15px;
		font-weight: 400;
		text-align: left;
		box-shadow: none;
	}
	.mcp-setup :global(.sheet-mode button.ctx-chip:not(:disabled)) { cursor: pointer; }
	.mcp-setup :global(.sheet-mode button.ctx-chip:hover:not(:disabled)) { background: var(--hover-overlay); }
	.mcp-setup :global(.sheet-mode button.ctx-chip:active:not(:disabled)) { background: var(--muted); }
	.mcp-setup :global(.sheet-mode .ctx-chip:focus-visible) { outline: 2px solid var(--ring); outline-offset: -2px; }
	/* Icon tile · name · value · chevron */
	.mcp-setup :global(.sheet-mode .ctx-chip > svg:first-of-type) {
		order: 0;
		box-sizing: border-box;
		width: 30px;
		height: 30px;
		flex: none;
		padding: 7px;
		border-radius: calc(var(--radius) * 0.65);
		background: var(--muted);
		color: var(--foreground);
	}
	.mcp-setup :global(.sheet-mode .ctx-chip::before) {
		order: 1;
		flex: 1;
		min-width: 0;
		position: static;
		font-size: 15px;
		font-weight: 400;
		color: var(--foreground);
	}
	.mcp-setup :global(.sheet-mode .ctx-chip > :not(svg:first-of-type)) { order: 2; }
	.mcp-setup :global(.sheet-mode .ctx-chip .ctx-chip-label) {
		flex: 0 1 auto;
		max-width: 52%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 14.5px;
		font-weight: 400;
		color: var(--muted-foreground);
		text-align: right;
	}
	.mcp-setup :global(.sheet-mode .ctx-chip .ctx-chip-chev),
	.mcp-setup :global(.sheet-mode .ctx-chip > svg.lucide-chevron-down) { order: 3; transform: rotate(-90deg); width: 16px; height: 16px; color: var(--muted-foreground); }
	/* Agent and "Runs on" open a page: draw the list chevron. */
	.mcp-setup :global(.sheet-mode :is(.context-selector-btn, .ctx-chip.runner, .mode-trigger)::after) {
		content: '';
		order: 4;
		width: 8px;
		height: 8px;
		flex: none;
		margin: 0 3px 0 -2px;
		border-top: 2px solid var(--muted-foreground);
		border-right: 2px solid var(--muted-foreground);
		transform: rotate(45deg);
		opacity: 0.75;
	}
	.mcp-setup :global(.sheet-mode :is(.context-selector-btn .ctx-chip-chev, .mode-trigger > svg.lucide-chevron-down)) { display: none; }
	.mcp-setup :global(.sheet-mode .context-selector-btn::before) { content: 'Agent'; }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner::before),
	.mcp-setup :global(.sheet-mode .mode-trigger::before),
	.mcp-setup :global(.sheet-mode .ctx-chip.readonly:not(.wt-indicator):has(.lucide-monitor)::before),
	.mcp-setup :global(.sheet-mode .ctx-chip.readonly:not(.wt-indicator):has(.lucide-monitor-off)::before) { content: 'Runs on'; }
	.mcp-setup :global(.sheet-mode .repo-chip::before) { content: 'Project'; }
	.mcp-setup :global(.sheet-mode .branch-btn::before),
	.mcp-setup :global(.sheet-mode .branch-trigger::before),
	.mcp-setup :global(.sheet-mode .ctx-chip.readonly:has(.branch-name)::before) { content: 'Branch'; }
	.mcp-setup :global(.sheet-mode .wt-indicator::before),
	.mcp-setup :global(.sheet-mode .wt-chip::before) { content: 'Worktree'; padding-left: 42px; }
	.mcp-setup :global(.sheet-mode .degraded::before) { content: 'Runs on'; }
	/* The Project row names the project; the Branch row shows the branch alone. */
	.mcp-setup :global(.sheet-mode .branch-btn .project-name),
	.mcp-setup :global(.sheet-mode .branch-btn .separator) { display: none; }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner) { color: var(--foreground); }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner svg) { color: inherit; }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner.busy) { background: color-mix(in oklab, var(--warning) 12%, transparent); }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner.busy .ctx-chip-label) { color: var(--warning); white-space: normal; text-align: right; }
	.mcp-setup :global(.sheet-mode .ctx-chip.degraded) { color: var(--warning); }
	.mcp-setup :global(.sheet-mode .ctx-chip.degraded .ctx-chip-label) { color: var(--warning); }
	.mcp-setup :global(.sheet-mode .runner-badge-wrap > .ctx-chip.runner) { flex: 1 1 0; width: auto; }
	.mcp-setup :global(.sheet-mode .queued-cancel) { width: 44px; height: 44px; margin: 0 8px 0 0; border-radius: 50%; }
	/* A selector that still opens in place (project, branch) drops below its row. */
	.mcp-setup :global(.sheet-mode :is(.branch-dropdown, .selector-dropdown, .wf-menu)) { margin: 0 10px 10px; }

	/* Pages hosting a selector's own menu (Agent, "Runs on") as a full list. */
	.mcp-page .mcp-host:empty { display: none; }
	.mcp-page .mcp-host :global(:is(.selector-dropdown, .runner-dropdown, .mode-dropdown)) {
		position: static;
		inset: auto;
		translate: none;
		width: 100%;
		min-width: 0;
		max-width: none;
		max-height: none;
		margin: 0;
		padding: 0;
		overflow: hidden;
		border: 1px solid var(--border);
		border-radius: var(--mcp-row-radius);
		background: var(--card);
		box-shadow: none;
		animation: none;
		--selector-scroll-height: none;
	}
	.mcp-page .mcp-host :global(:is(.menu-item, .runner-item)) {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		min-height: 52px;
		margin: 0;
		padding: 10px 14px;
		border-radius: 0;
		font-size: 15px;
	}
	.mcp-page .mcp-host :global(:is(.menu-item, .runner-item) + :is(.menu-item, .runner-item, span[hidden] + .runner-item)),
	.mcp-page .mcp-host :global(span[hidden] + .runner-item) { border-top: 1px solid var(--border); }
	.mcp-page .mcp-host :global(.menu-item.selected), .mcp-page .mcp-host :global(.runner-item.checked) { background: transparent; color: var(--foreground); font-weight: 600; }
	.mcp-page .mcp-host :global(.menu-item .check-icon), .mcp-page .mcp-host :global(.worker-row-check) { width: 20px; height: 20px; color: var(--primary); }
	.mcp-page .mcp-host :global(.menu-divider) { height: 0; margin: 0; border-top: 1px solid var(--border); }
	.mcp-page .mcp-host :global(.menu-item .item-desc) { font-size: 12.5px; font-weight: 400; margin-top: 1px; }
	.mcp-page .mcp-host :global(.panel-label) { padding: 10px 14px 4px; }
	/* A worker that cannot take the chat keeps its greyed name and says why in its tag. */
	.mcp-page .mcp-host :global(.worker-row-note) { font-size: 11.5px; padding: 3px 8px; border-radius: 999px; }
	.mcp-page .mcp-host :global(:is(.runner-busy, .runner-error, .runner-loading)) { padding: 12px 14px; font-size: 13.5px; }
	.mcp-page .mcp-host :global(.runner-item.primary) { color: var(--primary); }
	.mcp-page .mcp-host :global(.runner-item.danger) { color: var(--destructive); }

	/* Model page: the shared picker, sized to the page. */
	.mcp-model { display: flex; flex: 1; min-height: 0; --mpp-width: 100%; --mpp-height: 100%; }
	.mcp-model :global(.mpp-list), .mcp-model :global(.mpp-rail) { touch-action: pan-y; -webkit-overflow-scrolling: touch; }

	/* Context and usage page: the shared details on a card. */
	.mcp-usage { padding: 14px; border: 1px solid var(--border); border-radius: var(--mcp-row-radius); background: var(--card); }

	/* Pinned action bar */
	.mcp-foot { flex-shrink: 0; padding: 10px 14px calc(12px + env(safe-area-inset-bottom, 0px)); border-top: 1px solid var(--border); background: var(--popover); }
	/* One equal column per action that is actually shown (Call hides itself when the composer has none). */
	.actions { display: grid; grid-auto-flow: column; grid-auto-columns: minmax(0, 1fr); gap: 8px; }
	.act {
		position: relative;
		min-width: 0;
		height: 46px;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 7px;
		padding: 0 6px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) * 0.9);
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 14px;
		font-weight: 500;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
		transition: background-color 0.15s, transform 0.15s;
	}
	.act :global(svg) { flex: none; color: var(--muted-foreground); }
	.act:hover:not(:disabled) { background: var(--hover-overlay); }
	.act:active:not(:disabled) { transform: scale(0.97); }
	.act[aria-pressed='true'] { background: color-mix(in oklab, var(--primary) 14%, var(--card)); border-color: color-mix(in oklab, var(--primary) 45%, transparent); }
	.act[aria-pressed='true'] :global(svg), .act[aria-pressed='true'] .act-label { color: var(--primary); }
	.act:disabled { opacity: 0.45; cursor: not-allowed; }
	.act-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; pointer-events: none; }
	/* Four actions: icon over label so every name fits at 360px. */
	.actions:has(:global(.voice-call-btn)):has(> button:nth-of-type(3)) .act { flex-direction: column; gap: 3px; height: 54px; font-size: 12px; }
	/* Call is the composer's own VoiceCallButton, stretched over the cell. */
	.call-act :global(.voice-call-btn) {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		display: flex;
		align-items: center;
		justify-content: flex-start;
		padding: 0 0 0 calc(50% - 28px);
		border: 0;
		border-radius: inherit;
		background: transparent;
		color: var(--muted-foreground);
	}
	.call-act :global(.voice-call-btn svg) { width: 18px; height: 18px; }
	.call-act .act-label { padding-left: 25px; }
	.actions:has(:global(.voice-call-btn)):has(> button:nth-of-type(3)) .call-act :global(.voice-call-btn) { justify-content: center; align-items: flex-start; padding: 9px 0 0; }
	.actions:has(:global(.voice-call-btn)):has(> button:nth-of-type(3)) .call-act .act-label { padding: 21px 0 0; }
	.call-act:not(:has(:global(.voice-call-btn))) { display: none; }

	:global(.mcc-spin) { animation: mcc-spin 0.9s linear infinite; }
	@keyframes mcc-spin { to { transform: rotate(360deg); } }

	@media (prefers-reduced-motion: reduce) {
		.qpick, .qcell.usage :global(.usage-popover) { animation: none; }
		.mcp, .mcp.shown, .mcp-scrim, .qcell, .qbar-grip, .usage-grip span, .bar-fill, .tile, .qcell.usage :global(.usage-popover),
		.mcp-grab, .mcp-back, .mcp-page, .mcp-page:not(.active):not(.under),
		.mcp-row, .mcp-sw, .mcp-sw::after, .mcp-seg button, .act, .mcp-ring .ring-progress { transition: none; }
		.mcp-chip.run i { animation: none; }
		:global(.mcc-spin) { animation: none; }
	}
</style>
