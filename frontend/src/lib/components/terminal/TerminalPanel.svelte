<script lang="ts">
	/**
	 * The Terminal tool in the session side panel (#1945, phase 2).
	 *
	 * Tabs are per chat and come back from the worker after a reload. Each tab
	 * keeps its emulator mounted while the panel is open so output never stops;
	 * closing the panel only detaches, the shells keep running.
	 *
	 * With `sessionId` set to WORKER_TERMINALS it is the Terminal page instead:
	 * the worker's terminals that belong to no chat.
	 */
	import { onMount, onDestroy, tick, untrack, type Snippet } from 'svelte';
	import { Plus, X, Search, Copy, Check, Eraser, Square, ChevronUp, ChevronDown, LoaderCircle, MessageSquarePlus } from 'lucide-svelte';
	import { ViewShell, ViewPanel, PanelIconButton, ListItem, EmptyDetail } from '$lib/components/layout/view';
	import PanelRowActions from '$lib/components/layout/view/PanelRowActions.svelte';
	import ChevronLeft from 'lucide-svelte/icons/chevron-left';
	import '$lib/components/settings/shell/settings.css';
	import TerminalView from './TerminalView.svelte';
	import MonitorWatch from './MonitorWatch.svelte';
	import NewTerminalPicker from './NewTerminalPicker.svelte';
	import PeerDeviceLinks from './PeerDeviceLinks.svelte';
	import PeerLinkCard from './PeerLinkCard.svelte';
	import TerminalKeyBar from './TerminalKeyBar.svelte';
	import { terminals, WORKER_TERMINALS, type TerminalRequest } from '$lib/stores/terminals.svelte';
	import { terminalAccess, blockAccess, chatWorkerTarget, folderName } from '$lib/terminal/gating';
	import { pasteNeedsConfirm, pasteConfirmLabel, pasteHint, normalizePaste, pasteLines, pasteRefusal } from '$lib/terminal/paste';
	import { mayTakeFocus } from '$lib/terminal/focus';
	import { ACTION_OPEN, ACTION_OPEN_AND_PASTE, isPeerTarget, localWorkerId, openErrorMessage, type OpenResult } from '$lib/terminal/peer';
	import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
	import type { CreateTerminal, TerminalInfo, TerminalTarget } from '$lib/api/terminals';
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	import { resourceSync } from '$lib/services/resource-sync';
	import { terminalSnippet } from '$lib/terminal/to-chat';
	import { requestComposerInsert } from '$lib/stores/composer-insert';

	let { sessionId, panelActions, mobile = false }: { sessionId: string; panelActions?: Snippet; mobile?: boolean } = $props();

	const chatless = $derived(sessionId === WORKER_TERMINALS);
	const blurb = $derived(chatless
		? 'Not tied to a chat: sign in to a CLI or finish a setup step. It keeps running when you leave this page.'
		: undefined);

	type View = ReturnType<typeof TerminalView>;
	let views = $state<Record<string, View | undefined>>({});
	let pickerOpen = $state(false);
	// A device waiting to link goes straight to the code box on a narrow screen,
	// where Review would otherwise land on a list row that hides it.
	let detailOpen = $state(peerTerminals.requests.length > 0);
	let newTerminal = $state(false);
	let watching = $state(false);
	let creating = $state(false);
	let searchOpen = $state(false);
	let query = $state('');
	let searchMiss = $state(false);
	let copied = $state(false);
	/** Tabs with text selected now (for "Send selection to chat"). */
	let selected = $state<Record<string, boolean>>({});
	let sentToChat = $state(false);
	let sentTimer: ReturnType<typeof setTimeout> | undefined;
	let ctrl = $state(false);
	let confirm = $state<{ kind: 'paste'; id: string; text: string } | { kind: 'close'; id: string } | null>(null);
	let searchInput = $state<HTMLInputElement>();
	let confirmButton = $state<HTMLButtonElement>();
	let plusWrap = $state<HTMLDivElement>();
	let panelEl = $state<HTMLElement>();
	let tabsEl = $state<HTMLElement>();

	// A narrow strip scrolls sideways with its scrollbar hidden, so bring the
	// current tab into view when it changes (a new tab lands off the edge).
	$effect(() => {
		const id = activeId;
		const strip = tabsEl;
		if (!id || !strip) return;
		void tick().then(() => {
			const tab = Array.from(strip.querySelectorAll('[role="tab"]')).find((el) => el.id === `terminal-tab-${id}`)?.closest('.tab');
			if (!tab) return;
			const box = strip.getBoundingClientRect();
			const at = tab.getBoundingClientRect();
			if (at.left < box.left) strip.scrollLeft -= box.left - at.left;
			else if (at.right > box.right) strip.scrollLeft += at.right - box.right;
		});
	});
	/** The button behind the request being handled, while it is handled. */
	let requestOrigin: Element | null = null;
	let destroyed = false;

	// A worker-wide denial is final; ensureProbe deliberately skips per-chat probes.
	const probe = $derived(terminals.capability.status === 'denied' ? terminals.capability : terminals.probeFor(sessionId));
	const access = $derived(terminalAccess(probe));
	const ready = $derived(probe.status === 'ready' && access.kind === 'ready' ? probe : null);
	const tabState = $derived(terminals.tabsFor(sessionId));
	const activeId = $derived(tabState.active);
	const activeInfo = $derived(activeId ? terminals.terminal(activeId) : null);
	const activeLink = $derived(activeId ? terminals.connection(activeId) : null);
	const notice = $derived(terminals.notice(sessionId));
	const counts = $derived(terminals.counts);
	const openCount = $derived(counts.open);
	const limit = $derived(counts.limit);
	const capReason = $derived(ready && openCount >= limit
		? `${limit} terminals are open on ${ready.worker}. Close one to open another.` : null);

	// Peer terminals (#196): only when this worker lists another worker as a
	// target, which it does only with peer terminals turned on here.
	const localId = $derived(probe.status === 'ready' ? localWorkerId(probe.targets) : undefined);
	const peerTerminalsOn = $derived(probe.status === 'ready' && (probe.targets ?? []).some((t) => isPeerTarget(t, localId)));
	/** Where a tab's shell runs: this worker, a peer worker, or an SSH grant. */
	function hostName(info: TerminalInfo | null): string {
		const fallback = ready?.worker ?? '';
		const target = info?.target as { kind: string; worker_id?: string; grant_id?: string } | undefined;
		if (!target || probe.status !== 'ready') return fallback;
		if (target.kind === 'worker' && (!target.worker_id || target.worker_id === localId)) return fallback;
		const row = (probe.targets ?? []).find((t) => t.kind === target.kind && (t.worker_id ?? t.grant_id) === (target.worker_id ?? target.grant_id));
		return row?.name ?? (target.kind === 'ssh' ? 'SSH machine' : 'Peer worker');
	}

	// Joining or leaving a network, and a peer coming or going, change the
	// targets this worker lists. The server announces those as `network`
	// changes, so a page left open picks up a newly enrolled peer without
	// being reopened.
	let stopNetworkSync: (() => void) | undefined;
	onMount(() => {
		void terminals.ensureProbe(sessionId, true);
		void terminals.load(sessionId);
		stopNetworkSync = resourceSync.subscribe(['network'], () => terminals.ensureProbe(sessionId, true));
	});

	// Closing the panel drops a request it has not acted on: it must never
	// replay the next time the panel opens.
	onDestroy(() => {
		destroyed = true;
		stopNetworkSync?.();
		terminals.dropRequest(sessionId);
		if (blockLink) peerTerminals.cancel(blockLink);
	});

	// ── Focus ──────────────────────────────────────────────────────────────
	/** Focus moves here only from the page body, the clicked button, or inside the panel. */
	function focusAllowed(): boolean {
		return mayTakeFocus(document.activeElement, panelEl, requestOrigin);
	}
	function focusView(id: string) {
		if (focusAllowed()) views[id]?.focus();
	}

	// ── Requests from command blocks ───────────────────────────────────────
	/** The worker a command block's terminal is waiting to link to. */
	let blockLink = $state<string | null>(null);
	let blockAttempt = 0;
	const blockFlow = $derived(blockLink ? peerTerminals.flow(blockLink) : null);
	const pending = $derived(terminals.pendingRequest(sessionId));
	$effect(() => {
		const request = pending;
		if (!request) return;
		// The panel can't act: say so and forget the request.
		const refused = probe.status === 'denied' ? 'Only the owner can use terminals.'
			: probe.status === 'error' ? probe.message || 'Terminals are unavailable right now.'
			: access.kind === 'disabled' ? access.reason : null;
		if (refused) {
			untrack(() => terminals.dropRequest(sessionId, `${request.action === 'paste' ? 'Nothing was pasted.' : 'No terminal opened.'} ${refused}`));
			return;
		}
		if (!tabState.loaded || !ready) return;
		untrack(() => void handleRequest(request));
	});

	async function handleRequest(request: TerminalRequest) {
		const origin = terminals.requestOrigin(sessionId);
		const taken = terminals.takeRequest(sessionId, request.seq);
		if (!taken || !ready) return;
		requestOrigin = origin;
		watching = false;
		try {
			const gate = blockAccess(taken.block, ready);
			if (gate.kind !== 'ready') {
				terminals.setNotice(sessionId, gate.kind === 'disabled' ? gate.reason : 'Terminals are unavailable here.');
				return;
			}
			const peer = chatWorkerTarget(ready);
			if (peer) { await handlePeerRequest(taken, peer); return; }
			const cwd = taken.block.cwd ?? ready.folder;
			const shell = taken.block.shell ?? ready.defaultShell;
			if (taken.action === 'paste') {
				const refusal = pasteRefusal(taken.block.command, shell);
				if (refusal) { terminals.setNotice(sessionId, refusal); return; }
			}
			const reusable = (id: string) => {
				const info = terminals.terminal(id);
				if (!info || info.state !== 'live' || info.busy || info.cwd !== cwd || info.shell !== shell) return false;
				// Never add to a prompt that may hold an earlier paste or typing:
				// "echo a" plus "rm -rf build" would become one command.
				return taken.action !== 'paste' || views[id]?.promptMayHoldText() === false;
			};
			let id = activeId && reusable(activeId) ? activeId : tabState.ids.findLast(reusable) ?? null;
			if (!id) {
				const created = await terminals.create(sessionId, { cwd: taken.block.cwd, shell: taken.block.shell });
				if (!created || destroyed) return;
				id = created.id;
				await tick();
				views[id]?.markFresh();
			}
			terminals.select(sessionId, id);
			if (taken.action === 'paste') await queuePaste(id, taken.block.command);
			else { await tick(); focusView(id); }
		} finally {
			requestOrigin = null;
		}
	}

	/**
	 * A command block on a chat that runs on another worker: the shell opens
	 * there. An open shell there can be reused to show it, but a paste always
	 * goes to a fresh one, so it can't add to whatever is at the prompt.
	 */
	async function handlePeerRequest(taken: TerminalRequest, peer: TerminalTarget) {
		const workerId = peer.worker_id!;
		const paste = taken.action === 'paste';
		if (paste) {
			const refusal = pasteRefusal(taken.block.command, taken.block.shell);
			if (refusal && taken.block.shell) { terminals.setNotice(sessionId, refusal); return; }
		}
		const reusable = (id: string) => {
			const info = terminals.terminal(id);
			return !!info && info.state === 'live' && !info.busy && info.target?.worker_id === workerId
				&& (!taken.block.cwd || info.cwd === taken.block.cwd) && (!taken.block.shell || info.shell === taken.block.shell);
		};
		let id = paste ? null : activeId && reusable(activeId) ? activeId : tabState.ids.findLast(reusable) ?? null;
		if (!id) {
			const attempt = ++blockAttempt;
			blockLink = workerId;
			const cwd = taken.block.cwd ?? null;
			const shell = taken.block.shell ?? null;
			const proof = await peerTerminals.authorize(workerId, peer.name, paste ? ACTION_OPEN_AND_PASTE : ACTION_OPEN, { session_id: sessionId, cwd, shell });
			// A newer press took over: the card and the outcome are now its own.
			if (destroyed || attempt !== blockAttempt) return;
			blockLink = null;
			if (!proof) {
				const flow = peerTerminals.flow(workerId);
				if (flow?.phase === 'ended') {
					terminals.setNotice(sessionId, `${paste ? 'Nothing was pasted.' : 'No terminal opened.'} ${flow.message}`);
					peerTerminals.clear(workerId);
				}
				return;
			}
			const result = await terminals.open(sessionId, { cwd, shell, target: { kind: 'worker', worker_id: workerId, proof } });
			if (destroyed) return;
			if (!('info' in result)) {
				terminals.setNotice(sessionId, `${paste ? 'Nothing was pasted.' : 'No terminal opened.'} ${openErrorMessage(result.code, result.message, peer.name)}`);
				return;
			}
			id = result.info.id;
			await tick();
			views[id]?.markFresh();
		}
		terminals.select(sessionId, id);
		if (paste) await queuePaste(id, taken.block.command);
		else { await tick(); focusView(id); }
	}

	// ── Paste: confirm multi-line, send through the paste route ────────────
	async function queuePaste(id: string, text: string) {
		const clean = normalizePaste(text);
		if (!clean || destroyed) return;
		// Backstop for the server: only bash holds several lines until Enter.
		const refusal = pasteRefusal(clean, terminals.terminal(id)?.shell);
		if (refusal) { terminals.setNotice(sessionId, refusal); return; }
		if (pasteNeedsConfirm(clean)) {
			confirm = { kind: 'paste', id, text: clean };
			await tick();
			if (focusAllowed()) confirmButton?.focus();
			return;
		}
		await sendPaste(id, clean);
	}

	async function sendPaste(id: string, text: string) {
		if (destroyed) return;
		const mode = await terminals.paste(sessionId, id, text);
		await tick();
		if (!mode || destroyed) return;
		views[id]?.showHint(pasteHint(text), mode);
		focusView(id);
	}

	async function acceptConfirm() {
		const current = confirm;
		confirm = null;
		if (!current) return;
		if (current.kind === 'paste') await sendPaste(current.id, current.text);
		else await terminals.close(sessionId, current.id);
	}

	function cancelConfirm() {
		const id = confirm?.id;
		confirm = null;
		if (id) focusView(id);
	}

	// ── Tabs ───────────────────────────────────────────────────────────────
	async function openTerminal(choice: CreateTerminal): Promise<OpenResult> {
		creating = true;
		// A peer open reports its failure to the picker, and the targets are
		// probed again so the row shows what the worker now reports.
		const peer = !!choice.target?.proof;
		const result = peer ? await terminals.open(sessionId, choice) : null;
		const info = result ? ('info' in result ? result.info : null) : await terminals.create(sessionId, choice);
		creating = false;
		if (peer) void terminals.ensureProbe(sessionId, true);
		if (info) {
			newTerminal = false;
			detailOpen = true;
			pickerOpen = false;
			await tick();
			views[info.id]?.markFresh();
			focusView(info.id);
			return { ok: true };
		}
		return result && !('info' in result) ? { ok: false, code: result.code, message: result.message } : { ok: false, code: 'unknown', message: '' };
	}

	function closeTab(id: string) {
		const info = terminals.terminal(id);
		if (info?.state === 'live' && info.busy) { confirm = { kind: 'close', id }; void tick().then(() => { if (focusAllowed()) confirmButton?.focus(); }); return; }
		void terminals.close(sessionId, id);
	}

	function tabKeydown(event: KeyboardEvent, index: number) {
		const ids = tabState.ids;
		const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
		if (!step || !ids.length) return;
		event.preventDefault();
		const next = ids[(index + step + ids.length) % ids.length];
		terminals.select(sessionId, next);
		document.getElementById(`terminal-tab-${next}`)?.focus();
	}

	// ── Tools ──────────────────────────────────────────────────────────────
	async function toggleSearch() {
		searchOpen = !searchOpen;
		searchMiss = false;
		if (searchOpen) { await tick(); searchInput?.select(); }
		else { if (activeId) views[activeId]?.clearSearch(); if (activeId) views[activeId]?.focus(); }
	}

	function find(back = false) {
		if (!activeId || !query) return;
		const view = views[activeId];
		searchMiss = !(back ? view?.findPrevious(query) : view?.findNext(query));
	}

	async function copyOutput() {
		if (!activeId) return;
		const text = views[activeId]?.outputText() ?? '';
		if (!text) return;
		try {
			await copyText(text);
			copied = true;
			setTimeout(() => (copied = false), 1200);
		} catch {
			terminals.setNotice(sessionId, COPY_ERROR_MESSAGE);
		}
	}

	// ── Send selection to chat ─────────────────────────────────────────────
	// The selected text goes to the end of this chat's draft as a fenced
	// block. It is not sent: the owner adds a question and sends it. Not in
	// the phone layout: xterm has no touch selection, and the header has no
	// room for a button that could never be used there.
	const offerSendToChat = $derived(!chatless && !mobile);
	const canSendSelection = $derived(offerSendToChat && !!activeId && !!selected[activeId]);
	function sendSelectionToChat() {
		if (!activeId || !offerSendToChat) return;
		const snippet = terminalSnippet(views[activeId]?.selectionText() ?? '');
		if (!snippet) return;
		requestComposerInsert(sessionId, snippet);
		sentToChat = true;
		clearTimeout(sentTimer);
		sentTimer = setTimeout(() => (sentToChat = false), 1600);
	}
	onDestroy(() => clearTimeout(sentTimer));

	const statusLabel = $derived.by(() => {
		if (!activeInfo || !activeLink) return null;
		if (activeInfo.state === 'exited') return { tone: 'bad', text: activeInfo.exit_code === null ? 'Exited' : `Exited · code ${activeInfo.exit_code}` };
		switch (activeLink.status) {
			case 'connecting': return { tone: 'wait', text: 'Connecting…' };
			case 'reconnecting': return { tone: 'warn', text: 'Reconnecting…' };
			case 'gone': case 'auth': case 'error': return { tone: 'bad', text: 'Disconnected' };
		}
		if (activeInfo.state === 'starting') return { tone: 'wait', text: 'Starting…' };
		return activeInfo.busy ? { tone: 'run', text: 'Running' } : { tone: 'ok', text: 'Live' };
	});
	const canType = $derived(activeInfo?.state !== 'exited' && activeLink?.status === 'live');

	function onWindowPointer(event: PointerEvent) {
		if (pickerOpen && plusWrap && !plusWrap.contains(event.target as Node)) pickerOpen = false;
	}
</script>

<svelte:window onpointerdown={onWindowPointer} />

{#snippet terminalBody(back: () => void)}
<section class="tpanel settings-shell" class:mobile class:page-terminal={chatless} aria-label="Terminal" bind:this={panelEl}>
	<header class="bar">
  {#if chatless}
   <button type="button" class="st-back page-back" aria-label="Back to Terminal" onclick={back}><ChevronLeft size={16} />Terminal</button>
   <div class="page-terminal-title">{!ready ? 'Terminal' : activeInfo && !newTerminal ? `${activeInfo.shell} · ${folderName(activeInfo.cwd)}` : 'New terminal'}</div>
  {/if}
		{#if !chatless && ready && tabState.ids.length && !watching}
			<div class="tabs" role="tablist" aria-label={chatless ? 'Terminals on this worker' : 'Terminals in this chat'} bind:this={tabsEl}>
				{#each tabState.ids as id, index (id)}
					{@const info = terminals.terminal(id)}
					{@const link = terminals.connection(id)}
					<div class="tab" class:current={id === activeId}>
						<button id={`terminal-tab-${id}`} class="tab-select" role="tab" aria-selected={id === activeId}
							tabindex={id === activeId ? 0 : -1} title={hostName(info) !== ready.worker ? `${hostName(info)}: ${info?.cwd ?? ''}` : info?.cwd}
							onclick={() => terminals.select(sessionId, id)} onkeydown={(e) => tabKeydown(e, index)}>
							<span class="dot" data-tone={info?.state === 'exited' ? 'bad' : link.status === 'live' ? (info?.busy ? 'run' : 'ok') : link.status === 'reconnecting' ? 'warn' : 'wait'} aria-hidden="true"></span>
							<span class="tab-shell">{info?.shell ?? 'shell'}</span>
							{#if hostName(info) !== ready.worker}<span class="tab-host" title={hostName(info)}>{hostName(info)}</span>{/if}
							<span class="tab-folder">{folderName(info?.cwd)}</span>
						</button>
						<button class="tab-close" aria-label={`Close terminal ${index + 1}`} title="Close terminal" onclick={() => closeTab(id)}><X size={12} /></button>
					</div>
				{/each}
			</div>
		{:else if !chatless}
			<div class="title"><span class="title-mark" aria-hidden="true">&gt;_</span>Terminal</div>
		{/if}
		{#if !chatless && ready && !watching}
			<div class="plus-wrap" bind:this={plusWrap}>
				<button class="tool" aria-label="New terminal" aria-haspopup="dialog" aria-expanded={pickerOpen}
					title={`New terminal (${openCount} of ${limit} open on ${ready.worker})`}
					onclick={() => {
						pickerOpen = !pickerOpen;
						// Fresh count for the cap line: other chats may have opened or closed shells.
						if (pickerOpen) void terminals.ensureProbe(sessionId, true);
					}}><Plus size={16} /></button>
				{#if pickerOpen}
					<div class="popover">
						<NewTerminalPicker linkCardShownFor={blockLink} probe={ready} chatId={chatless ? null : sessionId} busy={creating} disabledReason={capReason}
							onOpen={openTerminal} onTargetsChanged={() => terminals.ensureProbe(sessionId, true)} onCancel={() => (pickerOpen = false)} />
					</div>
				{/if}
			</div>
		{/if}
		<div class="spacer"></div>
		{#if ready && activeId && !watching}
			<div class="tools">
				<button class="tool" aria-label="Search output" title="Search output" aria-pressed={searchOpen} onclick={toggleSearch}><Search size={15} /></button>
				<button class="tool" aria-label="Copy output" title="Copy output (the selection, or everything)" onclick={copyOutput}>
					{#if copied}<Check size={15} />{:else}<Copy size={15} />{/if}
				</button>
				{#if offerSendToChat}
					<button class="tool to-chat" class:sent={sentToChat} aria-label="Send selection to chat" disabled={!canSendSelection && !sentToChat}
						title={sentToChat ? 'Added to your message. Nothing was sent.' : canSendSelection ? 'Add the selected text to your message as a code block. Nothing is sent.' : 'Select text in the terminal to add it to your message'}
						onclick={sendSelectionToChat}>
						{#if sentToChat}<Check size={15} />{:else}<MessageSquarePlus size={15} />{/if}
					</button>
				{/if}
				<button class="tool" aria-label="Clear" title="Clear the screen here. The shell keeps running." onclick={() => activeId && views[activeId]?.clearScreen()}><Eraser size={15} /></button>
				<button class="tool stop" aria-label="Stop" title="Stop the running command (Ctrl+C)" disabled={!activeInfo?.busy || !canType}
					onclick={() => activeId && views[activeId]?.type('\x03')}><Square size={13} /></button>
			</div>
		{/if}
		{#if !chatless && probe.status !== 'denied'}
			<button class="watch-toggle" aria-pressed={watching} onclick={() => { watching = !watching; pickerOpen = false; searchOpen = false; confirm = null; }}>{watching ? 'Back to shells' : 'Watch command'}</button>
		{/if}
		{@render panelActions?.()}
	</header>

	{#if searchOpen && activeId}
		<div class="search" role="search">
			<Search size={14} aria-hidden="true" />
			<input bind:this={searchInput} bind:value={query} placeholder="Find in output" aria-label="Find in output"
				spellcheck="false" autocomplete="off"
				oninput={() => { searchMiss = false; find(); }}
				onkeydown={(e) => {
					if (e.key === 'Enter') { e.preventDefault(); find(e.shiftKey); }
					else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); void toggleSearch(); }
				}} />
			{#if searchMiss && query}<span class="miss" role="status">No matches</span>{/if}
			<button class="tool" aria-label="Previous match" onclick={() => find(true)}><ChevronUp size={15} /></button>
			<button class="tool" aria-label="Next match" onclick={() => find()}><ChevronDown size={15} /></button>
			<button class="tool" aria-label="Close search" onclick={toggleSearch}><X size={14} /></button>
		</div>
	{/if}

	{#if peerTerminalsOn && ready && !watching}<PeerDeviceLinks workerName={ready.worker} />{/if}
	{#if blockFlow?.phase === 'linking' && !watching}<div class="block-link"><PeerLinkCard flow={blockFlow} /></div>{/if}

	{#if watching}<MonitorWatch {sessionId} />{/if}
	<div class="stage" class:watch-hidden={watching}>
		{#if probe.status === 'idle' || probe.status === 'loading' || (ready && !tabState.loaded)}
			<div class="state" aria-busy="true"><LoaderCircle size={18} class="spin" /><span>Loading terminals…</span></div>
		{:else if probe.status === 'denied'}
			<div class="state"><EmptyDetail title="Only the owner can use terminals" message="Sign in with the owner password on this worker to open one." /></div>
		{:else if access.kind === 'disabled'}
			<div class="state"><EmptyDetail title="Terminals aren't available here" message={access.reason} /></div>
		{:else if probe.status === 'error'}
			<div class="state"><EmptyDetail title="Terminals aren't available right now" message={probe.message} />
				<button class="st-btn" onclick={() => terminals.ensureProbe(sessionId, true)}>Try again</button></div>
		{:else if ready && (!tabState.ids.length || (chatless && newTerminal))}
			<!-- Backstop for a missed change: using the picker re-probes once the answer is stale. -->
			<div class="state empty" onfocusin={() => void terminals.ensureProbe(sessionId)}>
				{#if tabState.error}<p class="err" role="alert">{tabState.error}</p>{/if}
				<NewTerminalPicker inline linkCardShownFor={blockLink} probe={ready} chatId={chatless ? null : sessionId} busy={creating} disabledReason={capReason} {blurb} onOpen={openTerminal} onTargetsChanged={() => terminals.ensureProbe(sessionId, true)} />
			</div>
		{/if}
		{#if ready}
			{#each tabState.ids as id (id)}
				<TerminalView bind:this={views[id]} {id} {sessionId} active={id === activeId && !watching && !(chatless && newTerminal)}
					ctrlArmed={ctrl} onCtrlUsed={() => (ctrl = false)}
					onPasteRequest={(text) => void queuePaste(id, text)} mayFocus={focusAllowed}
					onSelectionChange={(has) => (selected[id] = has)} />
			{/each}
		{/if}
	</div>

	{#if confirm}
		<div class="confirm" role="alertdialog" aria-label={confirm.kind === 'paste' ? pasteConfirmLabel(confirm.text) : 'Close terminal'}
			tabindex="-1" onkeydown={(e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelConfirm(); } }}>
			{#if confirm.kind === 'paste'}
				<div class="confirm-text">
					<strong>{pasteConfirmLabel(confirm.text)}</strong>
					<span>They go to the prompt. Nothing runs until you press Enter.</span>
				</div>
				{@const lines = pasteLines(confirm.text)}
				<pre class="preview">{lines.slice(0, 3).join('\n')}{lines.length > 3 ? '\n…' : ''}</pre>
			{:else}
				<div class="confirm-text"><strong>A command is running. Close this terminal?</strong><span>The shell and everything running in it stop.</span></div>
			{/if}
			<div class="confirm-actions">
				<button class="st-btn" onclick={cancelConfirm}>Cancel</button>
				<button class="st-btn primary" class:danger={confirm.kind === 'close'} bind:this={confirmButton} onclick={acceptConfirm}>{confirm.kind === 'paste' ? 'Paste' : 'Close'}</button>
			</div>
		</div>
	{/if}

	<span class="sr-status" role="status" aria-live="polite">{sentToChat ? 'Selection added to your message. Nothing was sent.' : ''}</span>

	{#if notice}
		<div class="notice" role="alert"><span>{notice}</span>
			<button class="tool" aria-label="Dismiss" onclick={() => terminals.setNotice(sessionId, null)}><X size={13} /></button></div>
	{/if}

	{#if ready && activeInfo && statusLabel && !watching}
		<footer class="status" aria-live="polite">
			<span class="state-pill" data-tone={statusLabel.tone}><span class="dot" data-tone={statusLabel.tone} aria-hidden="true"></span>{statusLabel.text}</span>
			<span class="meta" title={activeInfo.cwd}>{activeInfo.shell} · {folderName(activeInfo.cwd)} · {hostName(activeInfo)}</span>
		</footer>
	{/if}

	{#if mobile && ready && activeId && !watching}
		<TerminalKeyBar {ctrl} disabled={!canType} onCtrl={() => (ctrl = !ctrl)}
			onKey={(data) => { ctrl = false; if (activeId) views[activeId]?.type(data); }} />
	{/if}
</section>

{/snippet}
{#if chatless}
 <ViewShell label="Terminal" {detailOpen} onBack={() => detailOpen = false}>
  {#snippet panel()}
   <ViewPanel title="Terminal" loading={probe.status === 'loading'}>
    {#snippet actions()}
     <PanelIconButton label="New terminal" icon={Plus} disabled={!ready || creating} onclick={() => { newTerminal = true; detailOpen = true; pickerOpen = false; void terminals.ensureProbe(sessionId, true); }} />
    {/snippet}
    {#each tabState.ids as id,index (id)}
     {@const info = terminals.terminal(id)}
     {@const link = terminals.connection(id)}
     <PanelRowActions>
      <ListItem {id} name={`${info?.shell ?? 'shell'} · ${folderName(info?.cwd)}`} summary={hostName(info)} selected={id === activeId}
       status={info?.state === 'exited' ? 'err' : link.status === 'live' ? (info?.busy ? 'busy' : 'ok') : link.status === 'reconnecting' ? 'warn' : 'off'}
       onselect={() => { newTerminal = false; terminals.select(sessionId,id); detailOpen = true; }} />
      {#snippet actions()}<PanelIconButton label={`Close terminal ${index + 1}`} icon={X} onclick={() => { detailOpen = true; closeTab(id); }} />{/snippet}
     </PanelRowActions>
    {:else}
     <ListItem id="new-terminal" name={ready ? 'Open a terminal' : 'Terminal access'} summary={ready ? 'A shell on this worker' : 'View availability and connection status'} selected={false} onselect={() => detailOpen = true} />
    {/each}
   </ViewPanel>
  {/snippet}
  {#snippet main(back)}{@render terminalBody(back)}{/snippet}
 </ViewShell>
{:else}
 {@render terminalBody(() => {})}
{/if}

<style>
 .page-terminal { min-width:0; }
 .page-terminal .bar { flex:none; min-height:60px; padding:8px 24px; background:var(--background); }
 .page-terminal-title { font-size:16px; font-weight:600; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
 .page-back { display:none; }
 :global(.view-shell[data-stacked]) .page-back { display:inline-flex; }
 :global(.view-shell[data-stacked]) .page-terminal-title { display:none; }

	.stage.watch-hidden { display: none; }
	.watch-toggle { flex-shrink: 0; min-height: 44px; padding: 0 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--card); color: var(--foreground); font-size: .75rem; cursor: pointer; }
	.watch-toggle:hover, .watch-toggle[aria-pressed="true"] { background: var(--hover-overlay); }
	.watch-toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.watch-toggle:active { transform: translateY(1px); }
	.tpanel { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--background); container-type: inline-size; font-family: var(--font-sans); }

	/* ── Header: tabs, +, tools, panel actions ── */
	.bar { display: flex; align-items: center; gap: 2px; min-height: 40px; padding: 4px 6px 0; border-bottom: 1px solid var(--border); background: var(--muted); }
	.title { display: flex; align-items: center; gap: 8px; padding: 0 8px 4px; font-size: 13px; font-weight: 600; color: var(--foreground); }
	.title-mark { font: 600 12px var(--font-sans); color: var(--muted-foreground); }
	.tabs { display: flex; gap: 2px; min-width: 0; overflow-x: auto; scrollbar-width: none; align-self: stretch; align-items: flex-end; }
	.tabs::-webkit-scrollbar { display: none; }
	.tab { position: relative; display: flex; align-items: center; flex: 0 1 168px; min-width: 96px; max-width: 168px; height: 34px;
		border: 1px solid transparent; border-bottom: 0; border-radius: 7px 7px 0 0; transition: background-color 0.15s ease-out; }
	.tab:hover:not(.current) { background: var(--hover-overlay); }
	.tab.current { background: var(--code-bg); border-color: var(--border); margin-bottom: -1px; height: 35px; }
	.tab-select { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; height: 100%; padding: 0 22px 0 10px; border: 0; background: transparent; color: var(--muted-foreground); font: 500 12px var(--font-sans); cursor: pointer; text-align: left; }
	.tab.current .tab-select { color: var(--foreground); }
	.tab-select:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; border-radius: 6px; }
	.tab-shell { flex-shrink: 0; }
	/* One line, cut with an ellipsis (the full name is its title). It may
	   shrink, so it never runs under the close button; the folder gives way first. */
	.tab-host { flex: 0 1 auto; min-width: 0; max-width: 16ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; padding: 1px 5px; border-radius: 4px; font: 600 10.5px var(--font-sans);
		color: var(--muted-foreground); background: var(--muted); }
	/* A shell on another worker: room for the worker's name next to the shell. */
	.tab:has(.tab-host) { flex-basis: 232px; max-width: 232px; }
	.tab-folder { flex-shrink: 100; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--subtle-foreground); }
	.tab-close { position: absolute; right: 3px; display: grid; place-items: center; width: 18px; height: 18px; padding: 0; border: 0; border-radius: 4px; background: transparent; color: var(--muted-foreground); cursor: pointer; opacity: 0; transition: opacity 0.12s; }
	.tab:hover .tab-close, .tab.current .tab-close, .tab-close:focus-visible { opacity: 1; }
	.tab-close:hover { background: var(--hover-overlay); color: var(--foreground); }
	.tab-close:focus-visible { outline: 2px solid var(--ring); }

	.dot { width: 7px; height: 7px; flex-shrink: 0; border-radius: 50%; background: var(--subtle-foreground); }
	.dot[data-tone='ok'] { background: var(--success); }
	.dot[data-tone='run'] { background: var(--success); box-shadow: 0 0 0 0 color-mix(in oklch, var(--success) 60%, transparent); animation: pulse 1.4s ease-out infinite; }
	.dot[data-tone='warn'] { background: var(--warning); animation: blink 1s steps(2) infinite; }
	.dot[data-tone='bad'] { background: var(--destructive); }

	/* The picker hangs from the header's right edge, so it stays inside the
	   panel however far the tabs push the + button. */
	.bar { position: relative; }
	.plus-wrap { padding-bottom: 4px; }
	.popover { position: absolute; top: calc(100% + 4px); right: 6px; z-index: 40; max-width: calc(100% - 12px); }
	.spacer { flex: 1; min-width: 4px; }
	.tools { display: flex; gap: 1px; padding-bottom: 4px; }
	.bar > :global(.panel-actions) { padding-bottom: 4px; }
	.tool { display: inline-grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 0; border-radius: calc(var(--radius) - 4px); background: transparent; color: var(--muted-foreground); cursor: pointer; transition: background-color 0.15s ease-out, color 0.15s ease-out; }
	.tool:hover:not(:disabled) { background: var(--hover-overlay); color: var(--foreground); }
	.tool:active:not(:disabled) { transform: translateY(1px); }
	.tool:focus-visible { outline: 2px solid var(--ring); outline-offset: -1px; }
	.tool:disabled { opacity: 0.35; cursor: not-allowed; }
	.tool[aria-pressed='true'], .tool[aria-expanded='true'] { background: var(--row-selected); color: var(--foreground); }
	.stop:not(:disabled) { color: var(--destructive); }
	.to-chat.sent { color: var(--success); }
	.sr-status { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

	/* ── Search ── */
	.search { display: flex; align-items: center; gap: 4px; padding: 4px 6px 4px 12px; border-bottom: 1px solid var(--border); background: var(--background); color: var(--muted-foreground); animation: slide 0.18s ease-out; }
	.search input { flex: 1; min-width: 0; height: 28px; border: 0; outline: 0; background: transparent; color: var(--foreground); font: 13px var(--font-sans); }
	.miss { font-size: 11.5px; color: var(--warning); white-space: nowrap; }

	/* ── Stage ── */
	.stage { position: relative; flex: 1; min-height: 0; background: var(--code-bg); overflow: hidden; }
	.state { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; padding: 24px; text-align: center; color: var(--muted-foreground); font-size: 12.5px; background: var(--background); }
	.state p { margin: 0; max-width: 34ch; line-height: 1.5; }
	.state.empty { align-items: stretch; justify-content: safe center; overflow-y: auto; padding: 24px clamp(16px, 8cqi, 48px); text-align: left; }
	.state.empty > :global(*) { align-self: center; }
	.err { color: var(--destructive); }
	:global(.tpanel .spin) { animation: spin 0.9s linear infinite; }

	/* ── Confirm, notice, status ── */
	.confirm { display: grid; gap: 8px; padding: 10px 12px; border-top: 1px solid var(--border); background: var(--card); animation: slide 0.2s cubic-bezier(0.2, 0.8, 0.2, 1); outline: none; }
	.confirm-text { display: grid; gap: 2px; font-size: 12.5px; color: var(--muted-foreground); }
	.confirm-text strong { font-size: 13px; color: var(--foreground); }
	.preview { margin: 0; max-height: 72px; overflow: hidden; padding: 6px 8px; border-radius: 6px; background: var(--code-bg); color: var(--foreground); font: 11.5px/1.45 var(--font-mono); white-space: pre-wrap; word-break: break-all; }
	.confirm-actions { display: flex; justify-content: flex-end; gap: 8px; }
	.confirm-actions button { min-height: 30px; padding: 0 14px; border-radius: calc(var(--radius) - 4px); font: 500 12.5px var(--font-sans); cursor: pointer; }
	.confirm-actions button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.notice { display: flex; align-items: center; gap: 8px; padding: 4px 6px 4px 12px; border-top: 1px solid color-mix(in oklch, var(--warning) 35%, transparent); background: color-mix(in oklab, var(--warning) 10%, var(--background)); color: var(--warning); font-size: 12px; }
	.notice span { flex: 1; min-width: 0; }
	.status { display: flex; align-items: center; gap: 10px; height: 26px; flex-shrink: 0; padding: 0 12px; border-top: 1px solid var(--border); background: var(--background); font: 12px var(--font-sans); color: var(--muted-foreground); white-space: nowrap; }
	.state-pill { display: inline-flex; align-items: center; gap: 6px; color: var(--foreground); }
	.state-pill[data-tone='warn'] { color: var(--warning); }
	.state-pill[data-tone='bad'] { color: var(--destructive); }
	.meta { min-width: 0; overflow: hidden; text-overflow: ellipsis; }

	.mobile .bar { padding-top: max(4px, env(safe-area-inset-top, 0px)); }
	.mobile .tool { width: 40px; height: 40px; }
	.mobile .tab { height: 40px; }
	.mobile .tab.current { height: 41px; }
	.mobile .tab-close { width: 28px; height: 28px; opacity: 1; }
	.mobile .tab-select { padding-right: 32px; }
	@container (max-width: 420px) {
		.tab-folder { display: none; }
		.tab { flex-basis: 96px; min-width: 76px; }
		/* A shell on another worker: the worker matters more than the shell
		   name here (the status bar still shows both), so the badge gets the room. */
		.tab:has(.tab-host) { flex-basis: 156px; min-width: 112px; max-width: 156px; }
		.tab-select:has(.tab-host) .tab-shell { display: none; }
		.tab-host { max-width: none; }
	}
	/* Too narrow for the tabs to share the row with the tools, Watch command
	   and the panel's buttons (#1309): the tabs were the only item that could
	   shrink and ended up a few pixels wide. They move to a row of their own
	   under the tools, with + right after them, so the current tab still meets
	   the terminal. The ::after is the line break between the two rows. Wider
	   than this (the default 480 px side panel) the header stays one row. */
	@container (max-width: 470px) {
		.bar:has(.tabs) { flex-wrap: wrap; row-gap: 0; }
		.bar:has(.tabs)::after { content: ''; order: 1; flex-basis: 100%; }
		/* Room for + on the same row, so a long strip scrolls instead of pushing + down. */
		.bar .tabs { order: 2; max-width: calc(100% - 46px); }
		.bar:has(.tabs) .plus-wrap { order: 2; }
	}

	@keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in oklch, var(--success) 55%, transparent); } 100% { box-shadow: 0 0 0 6px transparent; } }
	@keyframes blink { 50% { opacity: 0.35; } }
	@keyframes slide { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.dot[data-tone='run'], .dot[data-tone='warn'], .search, .confirm, :global(.tpanel .spin) { animation: none; }
	}
</style>
