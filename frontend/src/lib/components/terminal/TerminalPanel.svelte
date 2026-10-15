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
	import { Plus, X, Search, Copy, Check, Eraser, Square, ChevronUp, ChevronDown, LoaderCircle } from 'lucide-svelte';
	import TerminalView from './TerminalView.svelte';
	import MonitorWatch from './MonitorWatch.svelte';
	import NewTerminalPicker from './NewTerminalPicker.svelte';
	import TerminalKeyBar from './TerminalKeyBar.svelte';
	import { terminals, WORKER_TERMINALS, type TerminalRequest } from '$lib/stores/terminals.svelte';
	import { terminalAccess, blockAccess, folderName } from '$lib/terminal/gating';
	import { pasteNeedsConfirm, pasteConfirmLabel, pasteHint, normalizePaste, pasteLines, pasteRefusal } from '$lib/terminal/paste';
	import { mayTakeFocus } from '$lib/terminal/focus';
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';

	let { sessionId, panelActions, mobile = false }: { sessionId: string; panelActions?: Snippet; mobile?: boolean } = $props();

	const chatless = $derived(sessionId === WORKER_TERMINALS);
	const blurb = $derived(chatless
		? 'Not tied to a chat: sign in to a CLI or finish a setup step. It keeps running when you leave this page.'
		: undefined);

	type View = ReturnType<typeof TerminalView>;
	let views = $state<Record<string, View | undefined>>({});
	let pickerOpen = $state(false);
	let watching = $state(false);
	let creating = $state(false);
	let searchOpen = $state(false);
	let query = $state('');
	let searchMiss = $state(false);
	let copied = $state(false);
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

	const probe = $derived(terminals.probeFor(sessionId));
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

	onMount(() => {
		void terminals.ensureProbe(sessionId, true);
		void terminals.load(sessionId);
	});

	// Closing the panel drops a request it has not acted on: it must never
	// replay the next time the panel opens.
	onDestroy(() => {
		destroyed = true;
		terminals.dropRequest(sessionId);
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
	async function openTerminal(choice: { cwd: string | null; shell: string | null }) {
		creating = true;
		const info = await terminals.create(sessionId, choice);
		creating = false;
		if (info) {
			pickerOpen = false;
			await tick();
			views[info.id]?.markFresh();
			focusView(info.id);
		}
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

<section class="tpanel" class:mobile aria-label="Terminal" bind:this={panelEl}>
	<header class="bar">
		{#if ready && tabState.ids.length && !watching}
			<div class="tabs" role="tablist" aria-label={chatless ? 'Terminals on this worker' : 'Terminals in this chat'} bind:this={tabsEl}>
				{#each tabState.ids as id, index (id)}
					{@const info = terminals.terminal(id)}
					{@const link = terminals.connection(id)}
					<div class="tab" class:current={id === activeId}>
						<button id={`terminal-tab-${id}`} class="tab-select" role="tab" aria-selected={id === activeId}
							tabindex={id === activeId ? 0 : -1} title={info?.cwd}
							onclick={() => terminals.select(sessionId, id)} onkeydown={(e) => tabKeydown(e, index)}>
							<span class="dot" data-tone={info?.state === 'exited' ? 'bad' : link.status === 'live' ? (info?.busy ? 'run' : 'ok') : link.status === 'reconnecting' ? 'warn' : 'wait'} aria-hidden="true"></span>
							<span class="tab-shell">{info?.shell ?? 'shell'}</span>
							<span class="tab-folder">{folderName(info?.cwd)}</span>
						</button>
						<button class="tab-close" aria-label={`Close terminal ${index + 1}`} title="Close terminal" onclick={() => closeTab(id)}><X size={12} /></button>
					</div>
				{/each}
			</div>
		{:else}
			<div class="title"><span class="title-mark" aria-hidden="true">&gt;_</span>Terminal</div>
		{/if}
		{#if ready && !watching}
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
						<NewTerminalPicker probe={ready} busy={creating} disabledReason={capReason}
							onOpen={openTerminal} onCancel={() => (pickerOpen = false)} />
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

	{#if watching}<MonitorWatch {sessionId} />{/if}
	<div class="stage" class:watch-hidden={watching}>
		{#if probe.status === 'idle' || probe.status === 'loading' || (ready && !tabState.loaded)}
			<div class="state" aria-busy="true"><LoaderCircle size={18} class="spin" /><span>Loading terminals…</span></div>
		{:else if probe.status === 'denied'}
			<div class="state"><p class="state-title">Only the owner can use terminals</p><p>Sign in with the owner password on this worker to open one.</p></div>
		{:else if access.kind === 'disabled'}
			<div class="state"><p class="state-title">Terminals aren't available here</p><p>{access.reason}</p></div>
		{:else if probe.status === 'error'}
			<div class="state"><p class="state-title">Terminals aren't available right now</p><p>{probe.message}</p>
				<button class="retry" onclick={() => terminals.ensureProbe(sessionId, true)}>Try again</button></div>
		{:else if ready && !tabState.ids.length}
			<div class="state empty">
				{#if tabState.error}<p class="err" role="alert">{tabState.error}</p>{/if}
				<NewTerminalPicker inline probe={ready} busy={creating} disabledReason={capReason} {blurb} onOpen={openTerminal} />
			</div>
		{/if}
		{#if ready}
			{#each tabState.ids as id (id)}
				<TerminalView bind:this={views[id]} {id} {sessionId} active={id === activeId && !watching}
					ctrlArmed={ctrl} onCtrlUsed={() => (ctrl = false)}
					onPasteRequest={(text) => void queuePaste(id, text)} mayFocus={focusAllowed} />
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
				<button class="ghost" onclick={cancelConfirm}>Cancel</button>
				<button class="go" class:danger={confirm.kind === 'close'} bind:this={confirmButton} onclick={acceptConfirm}>{confirm.kind === 'paste' ? 'Paste' : 'Close'}</button>
			</div>
		</div>
	{/if}

	{#if notice}
		<div class="notice" role="alert"><span>{notice}</span>
			<button class="tool" aria-label="Dismiss" onclick={() => terminals.setNotice(sessionId, null)}><X size={13} /></button></div>
	{/if}

	{#if ready && activeInfo && statusLabel && !watching}
		<footer class="status" aria-live="polite">
			<span class="state-pill" data-tone={statusLabel.tone}><span class="dot" data-tone={statusLabel.tone} aria-hidden="true"></span>{statusLabel.text}</span>
			<span class="meta" title={activeInfo.cwd}>{activeInfo.shell} · {folderName(activeInfo.cwd)} · {ready.worker}</span>
		</footer>
	{/if}

	{#if mobile && ready && activeId && !watching}
		<TerminalKeyBar {ctrl} disabled={!canType} onCtrl={() => (ctrl = !ctrl)}
			onKey={(data) => { ctrl = false; if (activeId) views[activeId]?.type(data); }} />
	{/if}
</section>

<style>
	.stage.watch-hidden { display: none; }
	.watch-toggle { flex-shrink: 0; min-height: 44px; padding: 0 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--card); color: var(--foreground); font-size: .75rem; cursor: pointer; }
	.watch-toggle:hover, .watch-toggle[aria-pressed="true"] { background: var(--hover-overlay); }
	.watch-toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.watch-toggle:active { transform: translateY(1px); }
	.tpanel { display: flex; flex-direction: column; height: 100%; min-height: 0; background: var(--background); container-type: inline-size; font-family: var(--font-sans); }

	/* ── Header: tabs, +, tools, panel actions ── */
	.bar { display: flex; align-items: center; gap: 2px; min-height: 40px; padding: 4px 6px 0; border-bottom: 1px solid var(--border); background: var(--muted); }
	.title { display: flex; align-items: center; gap: 8px; padding: 0 8px 4px; font-size: 13px; font-weight: 600; color: var(--foreground); }
	.title-mark { font: 700 11px var(--font-mono); color: var(--primary); letter-spacing: -0.04em; }
	.tabs { display: flex; gap: 2px; min-width: 0; overflow-x: auto; scrollbar-width: none; align-self: stretch; align-items: flex-end; }
	.tabs::-webkit-scrollbar { display: none; }
	.tab { position: relative; display: flex; align-items: center; flex: 0 1 168px; min-width: 96px; max-width: 168px; height: 34px;
		border: 1px solid transparent; border-bottom: 0; border-radius: 7px 7px 0 0; transition: background-color 0.15s ease-out; }
	.tab:hover:not(.current) { background: var(--hover-overlay); }
	.tab.current { background: var(--code-bg); border-color: var(--border); margin-bottom: -1px; height: 35px; }
	.tab.current::before { content: ''; position: absolute; inset: -1px -1px auto; height: 2px; border-radius: 7px 7px 0 0; background: var(--primary); }
	.tab-select { display: flex; align-items: center; gap: 6px; flex: 1; min-width: 0; height: 100%; padding: 0 22px 0 10px; border: 0; background: transparent; color: var(--muted-foreground); font: 500 12px var(--font-mono); cursor: pointer; text-align: left; }
	.tab.current .tab-select { color: var(--foreground); }
	.tab-select:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; border-radius: 6px; }
	.tab-shell { flex-shrink: 0; }
	.tab-folder { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--subtle-foreground); }
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
	.tool[aria-pressed='true'], .tool[aria-expanded='true'] { background: color-mix(in oklch, var(--primary) 14%, transparent); color: var(--primary); }
	.stop:not(:disabled) { color: var(--destructive); }

	/* ── Search ── */
	.search { display: flex; align-items: center; gap: 4px; padding: 4px 6px 4px 12px; border-bottom: 1px solid var(--border); background: var(--background); color: var(--muted-foreground); animation: slide 0.18s ease-out; }
	.search input { flex: 1; min-width: 0; height: 28px; border: 0; outline: 0; background: transparent; color: var(--foreground); font: 12.5px var(--font-mono); }
	.miss { font-size: 11.5px; color: var(--warning); white-space: nowrap; }

	/* ── Stage ── */
	.stage { position: relative; flex: 1; min-height: 0; background: var(--code-bg); overflow: hidden; }
	.state { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 6px; padding: 24px; text-align: center; color: var(--muted-foreground); font-size: 12.5px; background:
		radial-gradient(circle at 50% 38%, color-mix(in oklch, var(--primary) 7%, transparent), transparent 60%),
		repeating-linear-gradient(0deg, transparent 0 23px, color-mix(in oklch, var(--foreground) 3%, transparent) 23px 24px); }
	.state p { margin: 0; max-width: 34ch; line-height: 1.5; }
	.state-title { font-size: 13.5px; font-weight: 600; color: var(--foreground); }
	.state.empty { align-items: stretch; justify-content: center; padding: 24px clamp(16px, 8cqi, 48px); text-align: left; }
	.state.empty > :global(*) { align-self: center; }
	.err { color: var(--destructive); }
	.retry { margin-top: 8px; min-height: 32px; padding: 0 14px; border: 1px solid var(--border-strong); border-radius: calc(var(--radius) - 4px); background: transparent; color: var(--foreground); cursor: pointer; }
	.retry:hover { background: var(--hover-overlay); }
	:global(.tpanel .spin) { animation: spin 0.9s linear infinite; }

	/* ── Confirm, notice, status ── */
	.confirm { display: grid; gap: 8px; padding: 10px 12px; border-top: 1px solid color-mix(in oklch, var(--primary) 35%, transparent); background: color-mix(in oklch, var(--primary) 7%, var(--background)); animation: slide 0.2s cubic-bezier(0.2, 0.8, 0.2, 1); outline: none; }
	.confirm-text { display: grid; gap: 2px; font-size: 12.5px; color: var(--muted-foreground); }
	.confirm-text strong { font-size: 13px; color: var(--foreground); }
	.preview { margin: 0; max-height: 72px; overflow: hidden; padding: 6px 8px; border-radius: 6px; background: var(--code-bg); color: var(--foreground); font: 11.5px/1.45 var(--font-mono); white-space: pre-wrap; word-break: break-all; }
	.confirm-actions { display: flex; justify-content: flex-end; gap: 8px; }
	.confirm-actions button { min-height: 30px; padding: 0 14px; border-radius: calc(var(--radius) - 4px); font: 500 12.5px var(--font-sans); cursor: pointer; }
	.confirm-actions button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.ghost { border: 1px solid var(--border-strong); background: transparent; color: var(--foreground); }
	.ghost:hover { background: var(--hover-overlay); }
	.go { border: 1px solid var(--primary); background: var(--primary); color: var(--primary-foreground); }
	.go.danger { border-color: var(--destructive); background: var(--destructive); color: var(--destructive-foreground); }
	.notice { display: flex; align-items: center; gap: 8px; padding: 4px 6px 4px 12px; border-top: 1px solid color-mix(in oklch, var(--warning) 35%, transparent); background: color-mix(in oklch, var(--warning) 10%, var(--background)); color: var(--warning); font-size: 12px; }
	.notice span { flex: 1; min-width: 0; }
	.status { display: flex; align-items: center; gap: 10px; height: 26px; flex-shrink: 0; padding: 0 12px; border-top: 1px solid var(--border); background: var(--background); font: 11px var(--font-mono); color: var(--muted-foreground); white-space: nowrap; }
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
	@container (max-width: 420px) { .tab-folder { display: none; } .tab { flex-basis: 96px; min-width: 76px; } }

	@keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in oklch, var(--success) 55%, transparent); } 100% { box-shadow: 0 0 0 6px transparent; } }
	@keyframes blink { 50% { opacity: 0.35; } }
	@keyframes slide { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
	@keyframes spin { to { transform: rotate(360deg); } }
	@media (prefers-reduced-motion: reduce) {
		.dot[data-tone='run'], .dot[data-tone='warn'], .search, .confirm, :global(.tpanel .spin) { animation: none; }
	}
</style>
