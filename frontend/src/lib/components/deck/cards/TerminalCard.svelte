<script lang="ts">
	/**
	 * TerminalCard - Raw terminal access card for The Deck
	 *
	 * Extends BaseCard with:
	 * - Full xterm.js terminal embed
	 * - Clear and copy buttons in header
	 * - WebSocket connection to backend CLI
	 */
	import { onMount, onDestroy } from 'svelte';
	import BaseCard from './BaseCard.svelte';

	// xterm.js imports - loaded dynamically
	let Terminal: typeof import('@xterm/xterm').Terminal;
	let FitAddon: typeof import('@xterm/addon-fit').FitAddon;
	let WebLinksAddon: typeof import('@xterm/addon-web-links').WebLinksAddon;

	interface Props {
		id: string;
		title?: string;
		sessionId: string;
		pinned?: boolean;
		minimized?: boolean;
		active?: boolean;
		onpin?: () => void;
		onminimize?: () => void;
		onclose?: () => void;
		onactivate?: () => void;
	}

	let {
		id,
		title = 'Terminal',
		sessionId,
		pinned = false,
		minimized = false,
		active = false,
		onpin,
		onminimize,
		onclose,
		onactivate
	}: Props = $props();

	// Terminal state
	let terminalContainer: HTMLDivElement | undefined = $state();
	let terminal: InstanceType<typeof import('@xterm/xterm').Terminal> | null = null;
	let fitAddon: InstanceType<typeof import('@xterm/addon-fit').FitAddon> | null = null;
	let ws: WebSocket | null = null;
	let isConnected = $state(false);
	let isTerminalReady = $state(false);
	let error = $state<string | null>(null);

	// Load xterm.js dynamically
	async function loadXterm() {
		try {
			const [xtermModule, fitModule, webLinksModule] = await Promise.all([
				import('@xterm/xterm'),
				import('@xterm/addon-fit'),
				import('@xterm/addon-web-links')
			]);
			Terminal = xtermModule.Terminal;
			FitAddon = fitModule.FitAddon;
			WebLinksAddon = webLinksModule.WebLinksAddon;

			// Also import the CSS
			await import('@xterm/xterm/css/xterm.css');

			return true;
		} catch (e) {
			console.error('Failed to load xterm.js:', e);
			error = 'Failed to load terminal';
			return false;
		}
	}

	onMount(async () => {
		const loaded = await loadXterm();
		if (loaded && terminalContainer) {
			initTerminal();
			connectWebSocket();
		}

		// Handle window resize
		window.addEventListener('resize', handleResize);
	});

	onDestroy(() => {
		window.removeEventListener('resize', handleResize);
		cleanup();
	});

	// Re-fit terminal when active state changes
	$effect(() => {
		if (active && fitAddon && terminal) {
			setTimeout(() => {
				fitAddon?.fit();
				sendResize();
			}, 100);
		}
	});

	// Re-fit when minimized changes
	$effect(() => {
		if (!minimized && fitAddon && terminal) {
			setTimeout(() => {
				fitAddon?.fit();
				sendResize();
			}, 100);
		}
	});

	function initTerminal() {
		if (!Terminal || !FitAddon || !WebLinksAddon || !terminalContainer) return;

		terminal = new Terminal({
			cursorBlink: true,
			fontSize: 13,
			fontFamily: 'ui-monospace, "Monaco", "Menlo", "Ubuntu Mono", "Consolas", monospace',
			theme: {
				background: '#1a1a2e',
				foreground: '#eaeaea',
				cursor: '#f5f5f5',
				cursorAccent: '#1a1a2e',
				selectionBackground: '#3d3d5c',
				black: '#1a1a2e',
				red: '#ff6b6b',
				green: '#4ade80',
				yellow: '#fbbf24',
				blue: '#60a5fa',
				magenta: '#c084fc',
				cyan: '#22d3ee',
				white: '#eaeaea',
				brightBlack: '#4a4a6a',
				brightRed: '#ff8a8a',
				brightGreen: '#6ee7a0',
				brightYellow: '#fcd34d',
				brightBlue: '#93c5fd',
				brightMagenta: '#d8b4fe',
				brightCyan: '#67e8f9',
				brightWhite: '#ffffff'
			},
			allowProposedApi: true
		});

		fitAddon = new FitAddon();
		terminal.loadAddon(fitAddon);
		terminal.loadAddon(new WebLinksAddon());

		terminal.open(terminalContainer);
		fitAddon.fit();
		isTerminalReady = true;

		// Handle keyboard input
		terminal.onData((data) => {
			if (ws && ws.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'input', data }));
			}
		});

		terminal.writeln('\x1b[1;36m=== Terminal ===\x1b[0m');
		terminal.writeln('\x1b[90mConnecting...\x1b[0m');
	}

	function getAuthToken(): string | null {
		const cookies = document.cookie.split(';');
		for (const cookie of cookies) {
			const [name, value] = cookie.trim().split('=');
			if (name === 'session') {
				return value;
			}
		}
		return null;
	}

	function connectWebSocket() {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		let wsUrl = `${protocol}//${window.location.host}/api/v1/ws/cli/${sessionId}`;

		const token = getAuthToken();
		if (token) {
			wsUrl = `${wsUrl}?token=${encodeURIComponent(token)}`;
		}

		ws = new WebSocket(wsUrl);

		ws.onopen = () => {
			isConnected = true;
			error = null;
			terminal?.writeln('\x1b[32mConnected!\x1b[0m\n');

			// Start a shell session
			ws?.send(JSON.stringify({ type: 'start', command: '/bin/bash' }));

			// Send terminal size
			sendResize();
		};

		ws.onmessage = (event) => {
			try {
				const data = JSON.parse(event.data);

				switch (data.type) {
					case 'output':
						terminal?.write(data.data);
						break;
					case 'error':
						error = data.message;
						terminal?.writeln(`\n\x1b[31mError: ${data.message}\x1b[0m`);
						break;
					case 'exit':
						terminal?.writeln(`\n\x1b[90m[Process exited with code ${data.exit_code}]\x1b[0m`);
						break;
					case 'ping':
						ws?.send(JSON.stringify({ type: 'pong' }));
						break;
				}
			} catch (e) {
				// Handle raw output
				terminal?.write(event.data);
			}
		};

		ws.onerror = () => {
			error = 'Connection error';
			terminal?.writeln('\n\x1b[31mConnection error\x1b[0m');
		};

		ws.onclose = () => {
			isConnected = false;
			terminal?.writeln('\n\x1b[90m[Disconnected]\x1b[0m');
		};
	}

	function sendResize() {
		if (terminal && fitAddon && ws && ws.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify({
				type: 'resize',
				cols: terminal.cols,
				rows: terminal.rows
			}));
		}
	}

	function handleResize() {
		if (fitAddon && terminal && !minimized) {
			fitAddon.fit();
			sendResize();
		}
	}

	function cleanup() {
		if (ws) {
			if (ws.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'stop' }));
			}
			ws.close();
			ws = null;
		}

		if (terminal) {
			terminal.dispose();
			terminal = null;
		}
	}

	function handleClear() {
		terminal?.clear();
	}

	function handleCopy() {
		const selection = terminal?.getSelection();
		if (selection) {
			navigator.clipboard.writeText(selection);
		}
	}

	function handleReconnect() {
		if (ws) {
			ws.close();
		}
		error = null;
		if (terminal) {
			terminal.clear();
			terminal.writeln('\x1b[90mReconnecting...\x1b[0m');
		}
		connectWebSocket();
	}
</script>

<BaseCard
	{id}
	{title}
	type="terminal"
	{pinned}
	{minimized}
	{active}
	{onpin}
	{onminimize}
	{onclose}
	{onactivate}
>
	{#snippet headerActions()}
		<!-- Connection status -->
		<span class="flex items-center gap-1.5 text-xs px-2 py-1 rounded-full {isConnected ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning'}">
			<span class="w-1.5 h-1.5 rounded-full bg-current {isConnected ? '' : 'animate-pulse'}"></span>
			{isConnected ? 'Connected' : 'Connecting'}
		</span>

		<!-- Clear button -->
		<button
			type="button"
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
			onclick={handleClear}
			title="Clear terminal"
			disabled={!isTerminalReady}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
			</svg>
		</button>

		<!-- Copy button -->
		<button
			type="button"
			class="p-1.5 rounded-lg text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
			onclick={handleCopy}
			title="Copy selection"
			disabled={!isTerminalReady}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3" />
			</svg>
		</button>
	{/snippet}

	<div class="flex flex-col h-full bg-[#1a1a2e] overflow-hidden">
		<!-- Error banner -->
		{#if error}
			<div class="flex items-center justify-between px-3 py-2 bg-destructive/10 border-b border-destructive/20">
				<span class="text-sm text-destructive">{error}</span>
				<button
					type="button"
					class="px-2 py-1 text-xs bg-destructive/20 text-destructive hover:bg-destructive/30 rounded transition-colors"
					onclick={handleReconnect}
				>
					Reconnect
				</button>
			</div>
		{/if}

		<!-- Terminal container -->
		<div
			bind:this={terminalContainer}
			class="flex-1 overflow-hidden p-2"
		></div>

		<!-- Loading state -->
		{#if !isTerminalReady}
			<div class="absolute inset-0 flex items-center justify-center bg-[#1a1a2e]">
				<div class="text-center">
					<svg class="w-8 h-8 animate-spin text-primary mx-auto mb-3" fill="none" viewBox="0 0 24 24">
						<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
						<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
					</svg>
					<p class="text-sm text-muted-foreground">Loading terminal...</p>
				</div>
			</div>
		{/if}
	</div>

	{#snippet footer()}
		<div class="flex items-center justify-between px-4 py-2 bg-[#16162a] border-t border-gray-700 text-xs text-gray-400">
			<span class="font-mono">{sessionId.slice(0, 8)}</span>
			<div class="flex items-center gap-4">
				<span><kbd class="px-1.5 py-0.5 bg-gray-700 rounded">Ctrl+C</kbd> Interrupt</span>
				<span><kbd class="px-1.5 py-0.5 bg-gray-700 rounded">Ctrl+D</kbd> Exit</span>
			</div>
		</div>
	{/snippet}
</BaseCard>

<style>
	:global(.xterm) {
		height: 100%;
		padding: 4px;
	}

	:global(.xterm-viewport) {
		overflow-y: auto !important;
	}

	:global(.xterm-screen) {
		width: 100% !important;
	}
</style>
