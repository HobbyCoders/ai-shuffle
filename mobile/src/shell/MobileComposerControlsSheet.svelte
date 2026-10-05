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
	 * tapping Controls, opens the panel; dragging its header down or tapping
	 * the scrim closes it. Every gesture follows the finger and has a tap.
	 *
	 * The panel is portaled to <body> so no composer ancestor clips it. It
	 * holds the model (the app's own ModelPickerPanel), context and usage, the
	 * chat's setup (the shared ContextPillBar selectors, including "Runs on"
	 * with its busy rule), Away, Fast mode, effort, tool access, a running-tasks
	 * summary and the Call / Browser / Export actions. Prompts never render in
	 * it: they dock above the composer (MobilePromptDock).
	 *
	 * ControlsDock (shared $lib) computes every value and owns the open state;
	 * this component only renders and reports intent.
	 */
	import { tick, untrack } from 'svelte';
	import { pushState } from '$app/navigation';
	import { page } from '$app/state';
	import {
		Activity, ArrowLeft, Check, ChevronDown, ChevronRight, Cpu, Download, FilePen, Globe,
		Hand, LoaderCircle, Lock, Moon, ShieldOff, SlidersHorizontal, Sparkles, Zap
	} from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { notify } from '$lib/stores/notifications';
	import ContextPillBar from '$lib/components/chat/ContextPillBar.svelte';
	import ContextUsageBar from '$lib/components/chat/ContextUsageBar.svelte';
	import RunningTasksList from '$lib/components/chat/RunningTasksList.svelte';
	import ChatExportActions from '$lib/components/layout/ChatExportActions.svelte';
	import ModelPickerPanel from '$lib/components/settings/ModelPickerPanel.svelte';
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
		contextChoice, onContextChoiceSelect, effortOptions, effortValue, onEffortSelect,
		effortExplanation, effortKnown = true, effortDefaultValue = null,
		fastSupported = false, fastEnabled = false, onToggleFast, showCodeControls = true,
		showSetup = true, permissionOptions, permissionValue, permissionUpdating = false, onPermissionSelect,
		awayEnabled = false, awayAvailable = false, awayBusy = false, onToggleAway,
		assistantMode = false, onUsageOpenChange, callAction, browserOpen = false, onBrowserToggle, onBrowserPreload
	}: Props = $props();

	type Picker = 'effort' | 'access';
	type Page = 'main' | 'model' | 'export';

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

	/** The assistant's home runs its model's own effort; the backend ignores a pick. */
	const effortLocked = $derived(assistantMode || modelLocked || !effortKnown || effortOptions.length === 0);
	const effortValues = $derived(effortOptions.map((o) => o.value));
	/** What the meter draws: the pinned level, else the model's default. */
	const effortShown = $derived(effortValue ?? effortDefaultValue ?? null);
	const effortIndex = $derived(effortShown ? effortValues.indexOf(effortShown) : -1);
	const effortLabel = $derived(
		assistantMode ? 'Default'
			: !effortKnown ? (effortValue ?? 'Unknown')
			: !effortOptions.length ? 'Fixed'
			: effortValue ? (effortOptions.find((o) => o.value === effortValue)?.label ?? 'Auto')
			: 'Auto'
	);
	const effortA11y = $derived(
		assistantMode ? "Reasoning effort: the assistant's model default"
			: !effortKnown ? `Reasoning effort: ${effortValue ?? 'unknown'}`
			: !effortOptions.length ? 'Reasoning effort: set by the model'
			: `Reasoning effort: ${effortLabel}`
	);
	// Tool access exists in code chats and in the assistant's home (a code
	// session by construction); chat and voice run a fixed tool surface.
	const accessShown = $derived(showCodeControls || assistantMode);
	const accessValues = $derived(permissionOptions.map((o) => o.value));
	const accessValue = $derived(permissionValue ?? 'default');
	const accessIcons: Record<string, typeof Hand> = { default: Hand, auto: Sparkles, acceptEdits: FilePen, bypassPermissions: ShieldOff };
	const showSetupTiles = $derived(showSetup && !assistantMode);
	const awayShown = $derived(accessShown && !!onToggleAway);
	/** Away as the quick bar shows it: the desktop composer's "· Away" badge. */
	const awayOn = $derived(awayShown && awayEnabled && awayAvailable);
	const fastShown = $derived(fastSupported && !modelLocked);

	const runningTasks = $derived(($activeSession.activeTasks ?? []).filter((t) => t.status === 'running'));
	const chatTitle = $derived($activeSession.title?.trim() || 'New chat');
	const canExport = $derived(!assistantMode && !!$activeSession.sessionId && $activeSession.messages.length > 0);

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
	const RING_C = 2 * Math.PI * 6;
	const usageSeverity = $derived(usagePercent > 90 ? 'danger' : usagePercent >= 70 ? 'warning' : 'ok');

	const shownEffortIndex = $derived(preview?.cell === 'effort' ? effortValues.indexOf(preview.value) : effortIndex);
	const shownEffortLabel = $derived(preview?.cell === 'effort' ? (effortOptions.find((o) => o.value === preview!.value)?.label ?? effortLabel) : effortLabel);
	const shownAccess = $derived(preview?.cell === 'access' ? preview.value : accessValue);
	const AccessIcon = $derived(accessIcons[shownAccess] ?? Hand);

	function togglePicker(kind: Picker) {
		if (kind === 'effort' && effortLocked) {
			notify(assistantMode ? "The assistant uses its model's effort" : effortExplanation || 'Set by the model', { severity: 'info' });
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
	/** Opened straight to a page from the bar: Back closes the panel. */
	let direct = $state(false);
	let panelRef = $state<HTMLElement | null>(null);
	let bodyRef = $state<HTMLElement | null>(null);
	/** Stays true through the closing slide so content does not vanish mid-move. */
	let rendered = $state(false);
	let renderTimer: ReturnType<typeof setTimeout> | undefined;

	function requestOpen(next: boolean) {
		if (onOpenChange) onOpenChange(next);
		else if (!next) onClose();
	}

	function openPanel(target: Page = 'main') {
		closePicker();
		pageName = target;
		direct = target !== 'main';
		if (!open) requestOpen(true);
		else void focusPanel();
	}

	function closePanel() {
		requestOpen(false);
	}

	async function focusPanel() {
		await tick();
		panelRef?.querySelector<HTMLElement>('.mcp-head .mcp-icon-btn')?.focus({ preventScroll: true });
	}

	function goTo(target: Page) {
		pageName = target;
		direct = false;
		void tick().then(() => { if (bodyRef) bodyRef.scrollTop = 0; });
	}

	function back() {
		if (direct || pageName === 'main') { closePanel(); return; }
		pageName = 'main';
		void tick().then(() => { if (bodyRef) bodyRef.scrollTop = bodyRef.scrollHeight; });
	}

	let historyEntry = false;
	$effect(() => {
		const isOpen = open;
		untrack(() => {
			clearTimeout(renderTimer);
			if (isOpen) {
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
					if (bodyRef && pageName === 'main') bodyRef.scrollTop = bodyRef.scrollHeight;
				});
			} else {
				renderTimer = setTimeout(() => {
					rendered = false;
					pageName = 'main';
					direct = false;
				}, 400);
				if (panelRef?.contains(document.activeElement)) controlsCellRef?.focus({ preventScroll: true });
				if (historyEntry) {
					historyEntry = false;
					if (page.state?.mobileControls) history.back();
				}
			}
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

	// ── Drag: pull the bar up to open, the header down to close ──────────
	interface Drag {
		id: number;
		x: number;
		y: number;
		kind: 'pending' | 'open' | 'close' | 'step' | 'none';
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
		const cell = (event.target as Element | null)?.closest<HTMLElement>('[data-cell]')?.dataset.cell ?? null;
		drag = { id: event.pointerId, x: event.clientX, y: event.clientY, kind: 'pending', cell, height: 0, offset: 0, tracker: new VelocityTracker(), el: event.currentTarget as Element };
		suppressClick = false;
	}

	function startHeadDrag(event: PointerEvent) {
		if (event.button !== 0 || !open || drag) return;
		if ((event.target as Element | null)?.closest('button')) return;
		drag = { id: event.pointerId, x: event.clientX, y: event.clientY, kind: 'pending', cell: 'head', height: panelHeight(), offset: 0, tracker: new VelocityTracker(), el: event.currentTarget as Element };
	}

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
				pageName = 'main';
				direct = false;
				d.height = panelHeight();
				dragHeight = d.height;
				// The controls changed most often sit at the bottom, under the thumb.
				void tick().then(() => { if (bodyRef) bodyRef.scrollTop = bodyRef.scrollHeight; });
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
		} else if (d.kind === 'step') {
			const next = d.cell === 'effort'
				? stepValue(effortValues, effortShown, dx)
				: stepValue(accessValues, accessValue, dx, BYPASS);
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

	// ── Effort scrub across the panel's bars ──────────────────────────────
	let scrub = $state<string | null>(null);
	let scrubId = -1;
	let barsRef = $state<HTMLElement | null>(null);

	function scrubAt(clientX: number) {
		if (!barsRef || !effortValues.length) return;
		const r = barsRef.getBoundingClientRect();
		if (!r.width) return;
		const i = Math.min(effortValues.length - 1, Math.max(0, Math.floor(((clientX - r.left) / r.width) * effortValues.length)));
		scrub = effortValues[i];
	}

	function scrubStart(event: PointerEvent) {
		if (effortLocked || event.button !== 0) return;
		scrubId = event.pointerId;
		try { (event.currentTarget as Element).setPointerCapture?.(event.pointerId); } catch { /* synthetic pointer */ }
		scrubAt(event.clientX);
	}

	function scrubMove(event: PointerEvent) {
		if (event.pointerId !== scrubId) return;
		scrubAt(event.clientX);
	}

	function scrubEnd(event: PointerEvent) {
		if (event.pointerId !== scrubId) return;
		scrubId = -1;
		const value = scrub;
		scrub = null;
		if (value && value !== effortValue && event.type !== 'pointercancel') onEffortSelect(value);
	}

	const panelEffortIndex = $derived(scrub ? effortValues.indexOf(scrub) : effortIndex);
	const panelEffortLabel = $derived(scrub ? (effortOptions.find((o) => o.value === scrub)?.label ?? effortLabel) : effortLabel);

	// ── Panel actions ─────────────────────────────────────────────────────
	function openUsage() {
		closePicker();
		const wasOpen = open;
		if (wasOpen) closePanel();
		// After this tap's click reaches the document: the popover closes on
		// any click outside itself, including the one that asked for it.
		setTimeout(() => usageBar?.openDetails(), wasOpen ? 320 : 0);
	}

	/**
	 * The running-tasks list. RunningTasksList (shared, being built in
	 * $lib/components/chat) is not on this branch yet; until it lands this
	 * opens the transcript's own list (ActiveTasks) at the end of the chat.
	 * Hook-up point: render <RunningTasksList variant="sheet"> here instead.
	 */
	let tasksOpen = $state(false);

	function openRunningTasks() {
		closePanel();
		tasksOpen = true;
	}

	function toggleBrowser() {
		closePanel();
		onBrowserToggle?.();
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
			aria-label={`Model: ${modelLabel}${fastShown && fastEnabled ? ', fast mode' : ''}${modelLocked ? ', locked' : ''}`}
			aria-disabled={modelLocked || undefined}
			onclick={() => { if (!modelLocked) openPanel('model'); }}
		>
			<span class="qi">
				{#if modelLocked}<Lock size={18} aria-hidden="true" />{:else}<Cpu size={20} aria-hidden="true" />{/if}
				{#if fastShown && fastEnabled}<span class="qmini" aria-hidden="true"><Zap size={10} fill="currentColor" /></span>{/if}
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
				aria-busy={permissionUpdating || undefined}
				aria-label={`Tool access: ${ACCESS_LABEL[shownAccess] ?? shownAccess}${permissionUpdating ? ', saving' : ''}${awayOn ? ', Away mode on' : ''}`}
				onclick={() => togglePicker('access')}
			>
				<span class="qi">
					{#if permissionUpdating}<LoaderCircle size={20} class="mcc-spin" aria-hidden="true" />{:else}<AccessIcon size={20} aria-hidden="true" />{/if}
					{#if awayOn}<span class="qmini" data-away-badge aria-hidden="true"><Moon size={10} fill="currentColor" /></span>{/if}
				</span>
				<span class="ql">{ACCESS_SHORT[shownAccess] ?? shownAccess}</span>
			</button>
		{/if}

		<div class="qcell usage" data-cell="usage">
			<ContextUsageBar bind:this={usageBar} bind:percent={usagePercent} bind:overLimit={usageOverLimit} onOpenChange={onUsageOpenChange} showPercent />
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
		role="dialog"
		aria-modal="true"
		aria-label={pageName === 'model' ? 'Choose model' : pageName === 'export' ? 'Export chat' : 'Chat controls'}
		aria-hidden={!panelVisible}
		inert={!open}
		style:transform={`translateY(${panelShift})`}
	>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<header class="mcp-head" onpointerdown={startHeadDrag}>
			<span class="mcp-grab" aria-hidden="true"></span>
			<div class="mcp-head-row">
				{#if pageName !== 'main' && !direct}
					<button type="button" class="mcp-icon-btn" aria-label="Back to chat controls" onclick={back}><ArrowLeft size={20} aria-hidden="true" /></button>
				{:else}
					<button type="button" class="mcp-icon-btn" aria-label="Close chat controls" onclick={closePanel}><ChevronDown size={22} aria-hidden="true" /></button>
				{/if}
				<div class="mcp-title">
					<strong>{pageName === 'model' ? 'Model' : pageName === 'export' ? 'Export chat' : 'Chat controls'}</strong>
					<span>{chatTitle}</span>
				</div>
				<span></span>
			</div>
		</header>

		<div class="mcp-body" class:fill={pageName === 'model'} bind:this={bodyRef}>
			{#if rendered || panelVisible}
				{#if pageName === 'model'}
					<div class="mcp-model">
						<ModelPickerPanel
							groups={railGroups}
							options={modelOptions}
							value={modelValue}
							notice={modelNotice}
							{contextChoice}
							{onContextChoiceSelect}
							onSelect={(value) => { onModelSelect(value); back(); }}
							onRequestClose={back}
						/>
					</div>
				{:else if pageName === 'export'}
					<div class="mcp-stack">
						{#if $activeSession.sessionId}
							<div class="mcp-export"><ChatExportActions sessionId={$activeSession.sessionId} menu /></div>
						{/if}
						<p class="mcp-note">Review before sharing.</p>
					</div>
				{:else}
					<div class="mcp-stack">
						{#if runningTasks.length}
							<button type="button" class="tile run-row" onclick={openRunningTasks} aria-label={`${runningTasks.length} running tasks. Open the list`}>
								<span class="run-ic"><Activity size={18} aria-hidden="true" /></span>
								<span class="run-copy"><b>{runningTasks.length} running</b><small>{taskSummary(runningTasks)}</small></span>
								<ChevronRight size={20} class="mcp-chev" aria-hidden="true" />
							</button>
						{/if}

						<section class="sec" aria-label={assistantMode ? 'Assistant' : 'Model and setup'}>
							<h3 class="sec-h">{assistantMode ? 'Assistant' : 'Model and setup'}</h3>
							<button type="button" class="tile model-tile" disabled={modelLocked} onclick={() => goTo('model')} aria-label={`Model: ${modelLabel}${modelLocked ? ', locked' : ''}`}>
								<span><span class="tile-label">Model</span><span class="nm">{modelLabel}</span></span>
								{#if modelLocked}<Lock size={18} class="mcp-chev" aria-hidden="true" />{:else}<ChevronRight size={20} class="mcp-chev" aria-hidden="true" />{/if}
							</button>
							{#if modelNotice}<p class="mcp-warn">{modelNotice}</p>{/if}
							<button type="button" class="tile usage-row" onclick={openUsage}>
								<svg class="usage-ring {usageSeverity}" width="22" height="22" viewBox="0 0 16 16" aria-hidden="true">
									<circle class="ring-track" cx="8" cy="8" r="6" fill="none" stroke-width="2.4" />
									<circle class="ring-progress" cx="8" cy="8" r="6" fill="none" stroke-width="2.4" stroke-linecap="round"
										stroke-dasharray={RING_C} stroke-dashoffset={RING_C * (1 - Math.min(100, Math.max(0, usagePercent)) / 100)} />
								</svg>
								<b>Context and usage</b>
								<span class="pct">{usageOverLimit ? 'Over limit' : `${usagePercent}%`}</span>
								<ChevronRight size={20} class="mcp-chev" aria-hidden="true" />
							</button>
							{#if showSetupTiles && rendered}
								<div class="mcp-setup"><ContextPillBar sheetMode /></div>
							{/if}
						</section>

						<section class="sec" aria-label="This chat">
							<h3 class="sec-h">This chat</h3>
							{#if awayShown || fastShown}
								<div class="toggles" style:--n={(awayShown ? 1 : 0) + (fastShown ? 1 : 0)}>
									{#if awayShown}
										<button type="button" class="tile toggle" role="switch" aria-label="Away mode" aria-checked={awayEnabled && awayAvailable}
											aria-disabled={!awayAvailable || awayBusy || undefined}
											onclick={() => { if (awayAvailable && !awayBusy) onToggleAway?.(); else if (!awayAvailable) notify('Away works after the first message', { severity: 'info' }); }}>
											<span class="tg-top"><Moon size={20} aria-hidden="true" fill={awayEnabled && awayAvailable ? 'currentColor' : 'none'} />
												<span class="tg-state">{#if !awayAvailable}<Lock size={14} aria-hidden="true" />{:else if awayBusy}<LoaderCircle size={14} class="mcc-spin" aria-hidden="true" />{:else}{awayEnabled ? 'On' : 'Off'}{/if}</span></span>
											<span><span class="tg-name">Away</span>{#if !awayAvailable}<small class="tg-help">After first message</small>{/if}</span>
										</button>
									{/if}
									{#if fastShown}
										<button type="button" class="tile toggle" role="switch" aria-label="Fast mode" aria-checked={fastEnabled} onclick={() => onToggleFast?.()}>
											<span class="tg-top"><Zap size={20} aria-hidden="true" /><span class="tg-state">{fastEnabled ? 'On' : 'Off'}</span></span>
											<span><span class="tg-name">Fast mode</span><small class="tg-help">Higher price</small></span>
										</button>
									{/if}
								</div>
							{/if}

							<div class="tile effort-tile">
								<div class="tile-row"><span class="tile-label">Reasoning effort</span><span class="tile-value">{effortLocked ? (assistantMode ? 'Model default' : effortKnown ? 'Set by the model' : effortLabel) : panelEffortLabel}</span></div>
								{#if !effortLocked}
									<!-- The bars are the tab stops; the group takes the scrub. -->
									<!-- svelte-ignore a11y_interactive_supports_focus -->
									<div
										bind:this={barsRef}
										class="bars"
										role="radiogroup"
										aria-label="Reasoning effort"
										style:--n={effortOptions.length}
										onpointerdown={scrubStart}
										onpointermove={scrubMove}
										onpointerup={scrubEnd}
										onpointercancel={scrubEnd}
									>
										{#each effortOptions as option, i (option.value)}
											<button type="button" class="bar" class:lit={i <= panelEffortIndex} role="radio" aria-checked={option.value === (scrub ?? effortValue)}
												style:--h={`${Math.round(10 + ((i + 1) / effortOptions.length) * 30)}px`}
												onclick={() => { if (option.value !== effortValue) onEffortSelect(option.value); }}>
												<span class="bar-fill"></span>
												<span class="bar-label">{option.label}</span>
											</button>
										{/each}
									</div>
								{/if}
							</div>

							{#if accessShown}
								<div class="tile">
									<div class="tile-row"><span class="tile-label">Tool access</span></div>
									<div class="segs" role="radiogroup" aria-label="Tool access" aria-busy={permissionUpdating} style:--n={permissionOptions.length}>
										{#each permissionOptions as option (option.value)}
											{@const Icon = accessIcons[option.value] ?? Hand}
											<button type="button" class="seg" role="radio"
												aria-checked={option.value === accessValue} disabled={permissionUpdating}
												onclick={() => { if (option.value !== accessValue) onPermissionSelect(option.value); }}>
												<Icon size={18} aria-hidden="true" />
												<span>{ACCESS_LABEL[option.value] ?? option.label}</span>
											</button>
										{/each}
									</div>
									{#if permissionUpdating || ACCESS_HELP[accessValue]}
										<p class="access-help" aria-live="polite">{#if permissionUpdating}<LoaderCircle size={14} class="mcc-spin" aria-hidden="true" />Saving{:else}{ACCESS_HELP[accessValue]}{/if}</p>
									{/if}
								</div>
							{/if}
						</section>
					</div>
				{/if}
			{/if}
		</div>

		{#if pageName === 'main'}
			<footer class="mcp-foot">
				<div class="actions" role="group" aria-label="More actions">
					{#if callAction}
						<div class="act call-act">{@render callAction()}<span class="act-label" aria-hidden="true">Call</span></div>
					{/if}
					{#if onBrowserToggle}
						<button type="button" class="act" aria-pressed={browserOpen} onpointerenter={onBrowserPreload} onfocus={onBrowserPreload} onclick={toggleBrowser} aria-label="Live browser view">
							<Globe size={20} aria-hidden="true" /><span class="act-label">Browser</span>
						</button>
					{/if}
					{#if !assistantMode}
						<!-- The assistant's home keeps no transcript download. -->
						<button type="button" class="act" disabled={!canExport} onclick={() => goTo('export')} aria-label="Export chat">
							<Download size={20} aria-hidden="true" /><span class="act-label">Export</span>
						</button>
					{/if}
				</div>
			</footer>
		{/if}
	</div>
</div>

{#if tasksOpen && $activeSession.sessionId}
	<RunningTasksList sessionId={$activeSession.sessionId} variant="sheet" onClose={() => (tasksOpen = false)} />
{/if}

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
		padding: 16px;
		border-radius: calc(var(--radius) * 2);
		transform: none !important;
		box-shadow: var(--shadow-l), 0 0 0 100vmax var(--modal-overlay, color-mix(in oklch, var(--background) 60%, transparent));
		animation: mcc-sheet 0.3s cubic-bezier(0.22, 1, 0.36, 1);
	}
	@keyframes mcc-sheet { from { translate: 0 calc(100% + 40px); } }

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
		background: color-mix(in oklch, var(--primary) 13%, var(--card));
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
		position: fixed;
		left: 0;
		right: 0;
		bottom: 0;
		top: max(48px, calc(env(safe-area-inset-top, 0px) + 48px));
		z-index: 121;
		display: flex;
		flex-direction: column;
		background: var(--background);
		color: var(--foreground);
		border-top: 1px solid var(--border-strong);
		border-radius: calc(var(--radius) * 2) calc(var(--radius) * 2) 0 0;
		box-shadow: var(--shadow-l);
		visibility: hidden;
		transition: transform 0.36s cubic-bezier(0.22, 1, 0.36, 1), visibility 0s linear 0.36s;
		will-change: transform;
	}
	.mcp.shown { visibility: visible; transition: transform 0.36s cubic-bezier(0.22, 1, 0.36, 1), visibility 0s; }
	.mcp.dragging { transition: none; }
	.mcp-head { flex-shrink: 0; padding: 8px 8px 6px; touch-action: none; cursor: grab; }
	.mcp-grab { display: block; width: 40px; height: 4px; margin: 0 auto 4px; border-radius: 2px; background: var(--border-strong); }
	.mcp-head-row { display: grid; grid-template-columns: 44px minmax(0, 1fr) 44px; align-items: center; gap: 6px; }
	.mcp-title { min-width: 0; text-align: center; }
	.mcp-title strong { display: block; font-size: 15px; font-weight: 600; }
	.mcp-title span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; color: var(--muted-foreground); }
	.mcp-icon-btn { width: 44px; height: 44px; display: grid; place-items: center; border: 0; border-radius: var(--radius); background: transparent; color: var(--foreground); cursor: pointer; }
	.mcp-icon-btn:hover { background: var(--hover-overlay); }
	.mcp-body { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 2px 14px 12px; overflow-x: hidden; overflow-y: auto; overscroll-behavior: contain; scrollbar-width: none; -webkit-overflow-scrolling: touch; }
	.mcp-body::-webkit-scrollbar { display: none; }
	.mcp-body.fill { padding: 0; overflow: hidden; }
	.mcp-stack { margin-top: auto; display: flex; flex-direction: column; gap: 18px; }
	.sec { display: flex; flex-direction: column; gap: 8px; }
	.sec-h { margin: 0 2px; font-size: 13px; font-weight: 600; color: var(--foreground); }
	.tile {
		width: 100%;
		padding: 12px;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius);
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		text-align: left;
		transition: background-color 0.18s, border-color 0.18s;
	}
	button.tile { cursor: pointer; }
	button.tile:hover:not(:disabled) { background: color-mix(in oklch, var(--card) 85%, var(--foreground) 6%); }
	button.tile:disabled { cursor: default; }
	.tile-row { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
	.tile-label { display: block; font-size: 12.5px; font-weight: 500; color: var(--muted-foreground); }
	.tile-value { font-size: 13.5px; font-weight: 600; }
	.mcp-stack :global(.mcp-chev) { color: var(--muted-foreground); flex-shrink: 0; }
	.mcp-warn { margin: -2px 2px 0; font-size: 12px; color: var(--warning); }
	.mcp-note { margin: 0 2px; font-size: 12.5px; color: var(--muted-foreground); }

	.run-row { min-height: 60px; display: grid; grid-template-columns: 34px minmax(0, 1fr) 20px; align-items: center; gap: 10px; }
	.run-ic { display: grid; place-items: center; width: 34px; height: 34px; border-radius: calc(var(--radius) - 2px); background: color-mix(in oklch, var(--primary) 13%, var(--card)); color: var(--primary); }
	.run-copy b { display: block; font-size: 14.5px; font-weight: 600; }
	.run-copy small { display: block; font-size: 12px; color: var(--muted-foreground); }

	.model-tile { min-height: 60px; display: grid; grid-template-columns: minmax(0, 1fr) 20px; align-items: center; gap: 10px; }
	.model-tile .nm { display: block; margin-top: 1px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 15.5px; font-weight: 600; }
	.usage-row { min-height: 52px; display: grid; grid-template-columns: 22px minmax(0, 1fr) auto 20px; align-items: center; gap: 10px; }
	.usage-row b { font-size: 13.5px; font-weight: 550; }
	.usage-row .pct { font-size: 13px; color: var(--muted-foreground); font-variant-numeric: tabular-nums; }
	.usage-ring { display: block; transform: rotate(-90deg); }
	.usage-ring .ring-track { stroke: var(--border-strong); }
	.usage-ring .ring-progress { stroke: var(--primary); transition: stroke-dashoffset 0.3s ease; }
	.usage-ring.warning .ring-progress { stroke: var(--warning); }
	.usage-ring.danger .ring-progress { stroke: var(--destructive); }

	.toggles { display: grid; grid-template-columns: repeat(var(--n, 2), minmax(0, 1fr)); gap: 8px; }
	.toggle { min-height: 80px; display: flex; flex-direction: column; justify-content: space-between; gap: 6px; }
	.tg-top { display: flex; align-items: center; justify-content: space-between; color: var(--muted-foreground); }
	.tg-state { display: flex; align-items: center; gap: 4px; font-size: 12px; }
	.tg-name { display: block; font-size: 14px; font-weight: 600; }
	.tg-help { display: block; margin-top: 1px; font-size: 11.5px; color: var(--muted-foreground); }
	.toggle[aria-checked='true'] { background: color-mix(in oklch, var(--primary) 13%, var(--card)); border-color: color-mix(in oklch, var(--primary) 40%, transparent); }
	.toggle[aria-checked='true'] .tg-top { color: var(--primary); }
	.toggle[aria-disabled='true'] .tg-name { color: var(--muted-foreground); }
	.toggle[aria-disabled='true'] .tg-top { color: var(--subtle-foreground); }

	.effort-tile { padding-bottom: 8px; }
	.bars { display: grid; grid-template-columns: repeat(var(--n, 5), minmax(0, 1fr)); gap: 6px; margin-top: 8px; touch-action: pan-y; cursor: ew-resize; }
	.bar { height: 64px; display: flex; flex-direction: column; align-items: stretch; justify-content: flex-end; gap: 5px; padding: 0; border: 0; border-radius: calc(var(--radius) - 2px); background: transparent; font: inherit; cursor: pointer; }
	.bar-fill { height: var(--h); border-radius: 4px; background: color-mix(in oklch, var(--muted-foreground) 22%, var(--card)); transition: background-color 0.15s, height 0.2s; }
	.bar.lit .bar-fill { background: color-mix(in oklch, var(--primary) 70%, var(--card)); }
	.bar[aria-checked='true'] .bar-fill { background: var(--primary); }
	.bar-label { font-size: 11.5px; color: var(--muted-foreground); text-align: center; }
	.bar[aria-checked='true'] .bar-label { color: var(--foreground); font-weight: 600; }
	.tile .segs { margin-top: 8px; }
	.access-help { display: flex; align-items: center; gap: 6px; margin: 7px 2px 0; font-size: 12.5px; color: var(--muted-foreground); }

	/* Setup tiles: the shared ContextPillBar selectors (sheet mode) drawn as
	   label-over-value cells. The labels are generated so the selectors stay
	   the shared components; their own aria-labels already name each one. */
	.mcp-setup :global(.context-pill-bar.sheet-mode) { gap: 8px; }
	.mcp-setup :global(.sheet-mode .ctx-chip) {
		position: relative;
		height: auto;
		min-height: 58px;
		padding: 22px 12px 9px;
		align-items: center;
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius);
		background: var(--card);
		font-size: 13px;
		font-weight: 550;
	}
	.mcp-setup :global(.sheet-mode .ctx-chip::before) {
		position: absolute;
		top: 8px;
		left: 12px;
		font-size: 11.5px;
		font-weight: 450;
		color: var(--muted-foreground);
	}
	.mcp-setup :global(.sheet-mode .context-selector-btn::before) { content: 'Agent'; }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner::before),
	.mcp-setup :global(.sheet-mode .mode-trigger::before),
	.mcp-setup :global(.sheet-mode .ctx-chip.readonly:not(.wt-indicator):has(.lucide-monitor)::before) { content: 'Runs on'; }
	.mcp-setup :global(.sheet-mode .repo-chip::before) { content: 'Project'; }
	.mcp-setup :global(.sheet-mode .branch-btn::before),
	.mcp-setup :global(.sheet-mode .branch-trigger::before),
	.mcp-setup :global(.sheet-mode .ctx-chip.readonly:has(.branch-name)::before) { content: 'Branch'; }
	.mcp-setup :global(.sheet-mode .wt-indicator::before),
	.mcp-setup :global(.sheet-mode .wt-chip::before) { content: 'Worktree'; }
	/* The Project tile names the project; the Branch tile shows the branch alone. */
	.mcp-setup :global(.sheet-mode .branch-btn .project-name),
	.mcp-setup :global(.sheet-mode .branch-btn .separator),
	.mcp-setup :global(.sheet-mode .branch-btn > svg:first-child) { display: none; }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner) { color: var(--foreground); background: var(--card); }
	.mcp-setup :global(.sheet-mode .ctx-chip.runner.busy) {
		border-color: color-mix(in oklch, var(--warning) 40%, transparent);
		background: color-mix(in oklch, var(--warning) 14%, var(--card));
		color: var(--warning);
	}
	.mcp-setup :global(.sheet-mode .queued-cancel) { width: 44px; height: 44px; margin-left: 6px; border-radius: var(--radius); }
	/* An open move menu takes the whole row so it never runs off the side. */
	.mcp-setup :global(.sheet-mode .runner-badge-wrap:has(.runner-dropdown)) { grid-column: 1 / -1; }
	.mcp-setup :global(.sheet-mode .runner-dropdown) { width: 100%; max-width: none; max-height: none; }
	.mcp-setup :global(.runner-dropdown), .mcp-setup :global(.runner-item) { font-size: 13.5px; }
	.mcp-setup :global(.runner-item) { min-height: 44px; }

	.mcp-model { display: flex; flex: 1; min-height: 0; --mpp-width: 100%; --mpp-height: 100%; }
	.mcp-model :global(.mpp-list), .mcp-model :global(.mpp-rail) { touch-action: pan-y; -webkit-overflow-scrolling: touch; }
	.mcp-export { padding: 4px; border: 1px solid var(--border-subtle); border-radius: var(--radius); background: var(--card); }

	.mcp-foot { flex-shrink: 0; padding: 6px 12px calc(10px + env(safe-area-inset-bottom, 0px)); border-top: 1px solid var(--border-subtle); background: var(--popover); }
	.actions { display: flex; gap: 4px; }
	.act {
		position: relative;
		flex: 1 1 0;
		min-height: 56px;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 4px;
		border: 0;
		border-radius: var(--radius);
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 11.5px;
		cursor: pointer;
	}
	.act:hover:not(:disabled) { background: var(--hover-overlay); }
	.act[aria-pressed='true'] { background: color-mix(in oklch, var(--primary) 13%, var(--card)); color: var(--primary); }
	.act:disabled { opacity: 0.45; cursor: not-allowed; }
	.act-label { color: var(--muted-foreground); pointer-events: none; }
	.act[aria-pressed='true'] .act-label { color: var(--primary); }
	/* Call is the composer's own VoiceCallButton, stretched over the cell. */
	.call-act :global(.voice-call-btn) {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		padding-bottom: 18px;
		display: grid;
		place-items: center;
		border: 0;
		border-radius: var(--radius);
		background: transparent;
		color: var(--foreground);
	}
	.call-act :global(.voice-call-btn svg) { width: 20px; height: 20px; }
	.call-act .act-label { position: absolute; bottom: 9px; }
	.call-act:not(:has(.voice-call-btn)) { display: none; }

	:global(.mcc-spin) { animation: mcc-spin 0.9s linear infinite; }
	@keyframes mcc-spin { to { transform: rotate(360deg); } }

	@media (prefers-reduced-motion: reduce) {
		.qpick, .qcell.usage :global(.usage-popover) { animation: none; }
		.mcp, .mcp.shown, .mcp-scrim, .qcell, .qbar-grip, .bar-fill, .tile { transition: none; }
		:global(.mcc-spin) { animation: none; }
	}
</style>
