<script lang="ts">
	/**
	 * One terminal tab: an xterm.js emulator wired to its socket.
	 *
	 * Output bytes go straight from the socket into the emulator and input
	 * goes straight from the emulator to the socket. Neither is stored,
	 * logged or copied into Svelte state. Clipboard paste is intercepted and
	 * sent through the paste route, which cleans it and never runs it.
	 */
	import { onMount, onDestroy } from 'svelte';
	import { Terminal } from '@xterm/xterm';
	import { FitAddon } from '@xterm/addon-fit';
	import { SearchAddon } from '@xterm/addon-search';
	import '@xterm/xterm/css/xterm.css';
	import { RotateCcw, X, RefreshCw } from 'lucide-svelte';
	import { TerminalConnection } from '$lib/terminal/connection';
	import { terminals } from '$lib/stores/terminals.svelte';
	import { escClearKeys, type PasteHint } from '$lib/terminal/paste';
	import { terminalTheme } from './theme';
	import { exitDetail } from '$lib/terminal/gating';

	let {
		id,
		sessionId,
		active,
		ctrlArmed = false,
		onCtrlUsed,
		onPasteRequest,
		mayFocus,
	}: {
		id: string;
		sessionId: string;
		active: boolean;
		/** Phone key bar: the next key is sent with Ctrl. */
		ctrlArmed?: boolean;
		onCtrlUsed?: () => void;
		/** Clipboard text the owner pasted; the panel confirms and sends it. */
		onPasteRequest: (text: string) => void;
		/** Asked before this tab takes focus on becoming active. Absent means yes. */
		mayFocus?: () => boolean;
	} = $props();

	let host: HTMLDivElement;
	let term: Terminal | null = null;
	let fit: FitAddon | null = null;
	let search: SearchAddon | null = null;
	let connection: TerminalConnection | null = null;
	let observer: ResizeObserver | null = null;
	let frame = 0;
	let hint = $state<(PasteHint & { mode: 'bracketed' | 'plain' }) | null>(null);
	let inputError = $state<string | null>(null);
	/**
	 * Whether text may be sitting at the prompt. Unknown (true) until this
	 * view sees Enter or Ctrl-C, or the panel says the shell is new; the
	 * panel never pastes onto a prompt that may hold text.
	 */
	let promptDirty = true;

	const info = $derived(terminals.terminal(id));
	const link = $derived(terminals.connection(id));
	const exited = $derived(info?.state === 'exited');

	function send(data: string) {
		if (!connection || !data) return;
		// Esc from the phone key bar clears a waiting paste, like a real Esc.
		const clear = escClearKeys(data, hint?.mode ?? null);
		if (clear) {
			connection.input(clear);
			// Ctrl-E Ctrl-U empties a readline prompt; a plain prompt may keep
			// text after the cursor.
			promptDirty = hint?.mode !== 'bracketed';
			hint = null;
			return;
		}
		if (hint && /[\r\x03]/.test(data)) hint = null;
		promptDirty = !/[\r\x03]$/.test(data);
		inputError = null;
		connection.input(data);
	}

	/** Phone Ctrl key: turn the next letter into its control character. */
	function withCtrl(data: string): string {
		if (!ctrlArmed || data.length !== 1) return data;
		onCtrlUsed?.();
		const code = data.toUpperCase().charCodeAt(0);
		return code >= 64 && code <= 95 ? String.fromCharCode(code & 31) : data;
	}

	function refit() {
		if (!term || !fit || !active || !host?.isConnected || host.clientWidth === 0) return;
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(() => {
			if (!term || !fit) return;
			try { fit.fit(); } catch { return; }
			connection?.resize(term.cols, term.rows);
		});
	}

	onMount(() => {
		term = new Terminal({
			allowProposedApi: true,
			cursorBlink: true,
			convertEol: false,
			fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() || 'monospace',
			fontSize: window.matchMedia('(max-width: 700px)').matches ? 12 : 13,
			lineHeight: 1.15,
			// xterm's console messages can quote fragments of output; keep them out.
			logLevel: 'off',
			scrollback: 5000,
			theme: terminalTheme(host),
		});
		fit = new FitAddon();
		search = new SearchAddon();
		term.loadAddon(fit);
		term.loadAddon(search);
		term.open(host);
		term.onData((data) => send(withCtrl(data)));
		term.attachCustomKeyEventHandler((event) => {
			// Esc right after a paste clears it from the prompt instead.
			if (hint && event.type === 'keydown' && event.key === 'Escape') {
				send('\x1b');
				event.preventDefault();
				return false;
			}
			return true;
		});
		// Clipboard paste goes through the paste route, never as typed input.
		host.addEventListener('paste', handlePaste, true);

		connection = new TerminalConnection(id, {
			onReset: () => term?.reset(),
			onData: (bytes) => term?.write(bytes),
			onState: (next) => terminals.update(next),
			onStatus: (status, message) => terminals.setConnection(id, status, message),
			onDropped: () => terminals.drop(id),
			onInputError: (_code, message) => { inputError = message; },
			onReplayed: () => refit(),
		});
		connection.connect();
		document.fonts?.ready.then(() => refit());
		observer = new ResizeObserver(() => refit());
		observer.observe(host);
		refit();
	});

	onDestroy(() => {
		cancelAnimationFrame(frame);
		observer?.disconnect();
		host?.removeEventListener('paste', handlePaste, true);
		connection?.dispose();
		term?.dispose();
		term = null;
		connection = null;
	});

	$effect(() => {
		if (active) {
			refit();
			queueMicrotask(() => { if (!mayFocus || mayFocus()) term?.focus(); });
		}
	});

	function handlePaste(event: ClipboardEvent) {
		event.preventDefault();
		event.stopPropagation();
		const text = event.clipboardData?.getData('text/plain') ?? '';
		if (text) onPasteRequest(text);
	}

	// ── API for the panel (bind:this) ──────────────────────────────────────
	export function focus() { term?.focus(); }
	export function type(data: string) { send(data); term?.focus(); }
	/** Show the hint after a paste. The panel decides whether focus moves here. */
	export function showHint(next: PasteHint, mode: 'bracketed' | 'plain') { hint = { ...next, mode }; promptDirty = true; }
	export function clearScreen() { term?.clear(); term?.focus(); }
	export function findNext(query: string): boolean { return !!query && !!search?.findNext(query); }
	export function findPrevious(query: string): boolean { return !!query && !!search?.findPrevious(query); }
	export function clearSearch() { search?.clearDecorations(); term?.clearSelection(); }
	/** The selection, or the whole buffer when nothing is selected. */
	export function outputText(): string {
		if (!term) return '';
		if (term.hasSelection()) return term.getSelection();
		const buffer = term.buffer.active;
		const lines: string[] = [];
		for (let row = 0; row < buffer.length; row += 1) lines.push(buffer.getLine(row)?.translateToString(true) ?? '');
		return lines.join('\n').replace(/\s+$/, '');
	}
	export function hintActive(): boolean { return hint !== null; }
	/** True unless this view knows the prompt is empty. */
	export function promptMayHoldText(): boolean { return hint !== null || promptDirty; }
	/** The panel just opened this shell: nothing is at its prompt yet. */
	export function markFresh() { if (hint === null) promptDirty = false; }

	function retry() { connection?.retry(); }
</script>

<div class="term-view" class:hidden={!active} class:dim={link.status === 'reconnecting' || link.status === 'connecting'}>
	<div class="term-host" bind:this={host}></div>

	{#if hint}
		<div class="hint" class:risky={hint.risky} role="status">
			<kbd>⏎</kbd><span>{hint.text}</span>
		</div>
	{:else if inputError}
		<div class="hint risky" role="alert"><span>{inputError}</span></div>
	{/if}

	{#if exited}
		<div class="ended" role="group" aria-label="Shell exited">
			<p><strong>The shell exited</strong>{exitDetail(info?.exit_code)}</p>
			<p class="sub">Its output stays here until you close or restart it.</p>
			<div class="ended-actions">
				<button class="primary" onclick={() => terminals.restart(sessionId, id)}><RotateCcw size={14} />Restart</button>
				<button onclick={() => terminals.close(sessionId, id)}><X size={14} />Close</button>
			</div>
		</div>
	{:else if link.status === 'gone' || link.status === 'auth' || link.status === 'error'}
		<div class="ended" role="alert">
			<p><strong>{link.status === 'gone' ? 'This terminal ended' : link.status === 'auth' ? 'Signed out' : 'Connection lost'}</strong></p>
			{#if link.message}<p class="sub">{link.message}</p>{/if}
			<div class="ended-actions">
				{#if link.status === 'error'}<button class="primary" onclick={retry}><RefreshCw size={14} />Retry</button>{/if}
				<button onclick={() => (link.status === 'gone' ? terminals.drop(id) : terminals.close(sessionId, id))}><X size={14} />Close tab</button>
			</div>
		</div>
	{/if}
</div>

<style>
	.term-view { position: absolute; inset: 0; display: flex; flex-direction: column; background: var(--code-bg); transition: opacity 0.2s ease-out; }
	.term-view.hidden { visibility: hidden; pointer-events: none; }
	.term-view.dim .term-host { opacity: 0.55; }
	.term-host { flex: 1; min-height: 0; padding: 8px 4px 4px 10px; transition: opacity 0.2s ease-out; }
	.term-host :global(.xterm) { height: 100%; }
	.term-host :global(.xterm-viewport) { background-color: transparent !important; scrollbar-width: thin; scrollbar-color: var(--border-strong) transparent; }
	.term-host :global(.xterm:focus-within) { outline: none; }

	.hint {
		flex-shrink: 0; display: flex; align-items: center; gap: 8px; min-height: 28px;
		padding: 4px 12px; border-top: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
		background: color-mix(in oklch, var(--primary) 9%, var(--code-bg));
		color: var(--foreground); font: 500 11.5px/1.3 var(--font-sans);
		animation: rise 0.22s cubic-bezier(0.2, 0.8, 0.2, 1);
	}
	.hint.risky { border-top-color: color-mix(in oklch, var(--warning) 45%, transparent); background: color-mix(in oklch, var(--warning) 12%, var(--code-bg)); color: var(--warning); }
	kbd { display: inline-grid; place-items: center; min-width: 20px; height: 18px; padding: 0 4px; border: 1px solid var(--border-strong); border-bottom-width: 2px; border-radius: 4px; font: 600 11px/1 var(--font-mono); color: inherit; }

	.ended {
		position: absolute; left: 50%; bottom: 20px; transform: translateX(-50%);
		width: min(340px, calc(100% - 24px)); box-sizing: border-box; padding: 14px 16px;
		border: 1px solid var(--border-strong); border-radius: var(--radius);
		background: color-mix(in oklch, var(--popover) 94%, transparent); backdrop-filter: blur(8px);
		box-shadow: 0 12px 32px oklch(0 0 0 / 0.35); animation: rise 0.25s cubic-bezier(0.2, 0.8, 0.2, 1);
	}
	.ended p { margin: 0; font-size: 13px; color: var(--foreground); }
	.ended .sub { margin-top: 3px; font-size: 12px; color: var(--muted-foreground); }
	.ended-actions { display: flex; gap: 8px; margin-top: 12px; }
	.ended-actions button {
		display: inline-flex; align-items: center; gap: 6px; min-height: 32px; padding: 0 12px;
		border: 1px solid var(--border-strong); border-radius: calc(var(--radius) - 4px);
		background: transparent; color: var(--foreground); font: 500 12.5px var(--font-sans); cursor: pointer;
		transition: background-color 0.15s ease-out, border-color 0.15s ease-out;
	}
	.ended-actions button:hover { background: var(--hover-overlay); }
	.ended-actions button:active { transform: translateY(1px); }
	.ended-actions button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.ended-actions .primary { background: var(--primary); border-color: var(--primary); color: var(--primary-foreground); }
	.ended-actions .primary:hover { background: color-mix(in oklch, var(--primary) 88%, white); }

	@keyframes rise { from { opacity: 0; transform: translate(var(--tx, 0), 6px); } to { opacity: 1; transform: translate(var(--tx, 0), 0); } }
	.ended { --tx: -50%; }
	@media (max-width: 700px) {
		.ended-actions button { min-height: 44px; }
		.term-host { padding-left: 6px; }
	}
	@media (prefers-reduced-motion: reduce) { .hint, .ended { animation: none; } .term-view, .term-host { transition: none; } }
</style>
