<script lang="ts">
	/**
	 * TerminalCard - Interactive terminal for Claude Code CLI commands
	 *
	 * Embeds xterm.js terminal connected to the backend CLI bridge via WebSocket.
	 * Supports commands like /resume, /rewind for interactive CLI operations.
	 *
	 * Supports both desktop (with BaseCard wrapper) and mobile (standalone) modes.
	 */

	import { onMount, onDestroy } from 'svelte';
	import { Play, Square, RefreshCw, AlertCircle } from 'lucide-svelte';
	import '@xterm/xterm/css/xterm.css';
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { allTabs } from '$lib/stores/tabs';
	import { deck } from '$lib/stores/deck';

	// Dynamic imports for client-side only xterm modules
	let XTerminal: typeof import('@xterm/xterm').Terminal;
	let FitAddon: typeof import('@xterm/addon-fit').FitAddon;
	let WebLinksAddon: typeof import('@xterm/addon-web-links').WebLinksAddon;

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// Terminal state
	let terminalContainer: HTMLDivElement | undefined = $state();
	let terminal: InstanceType<typeof import('@xterm/xterm').Terminal> | null = null;
	let fitAddon: InstanceType<typeof import('@xterm/addon-fit').FitAddon> | null = null;
	let ws: WebSocket | null = null;

	// Connection state
	let isConnected = $state(false);
	let isReady = $state(false);
	let error = $state<string | null>(null);
	let isTerminalInitialized = $state(false);

	// Session and command selection
	let selectedSessionId = $state(card.dataId || '');
	let selectedCommand = $state((card.meta?.command as string) || '/resume');
	let hasStarted = $state(false);

	// Available commands
	const commands = [
		{ value: '/resume', label: 'Resume Session' },
		{ value: '/rewind', label: 'Rewind History' }
	];

	// Get available sessions from tabs store
	const availableSessions = $derived(
		$allTabs
			.filter((tab) => tab.sessionId)
			.map((tab) => ({
				id: tab.sessionId!,
				label: tab.title || `Session ${tab.sessionId?.slice(0, 8)}`
			}))
	);

	// Sync selected session with card dataId
	$effect(() => {
		if (card.dataId && card.dataId !== selectedSessionId) {
			selectedSessionId = card.dataId;
		}
	});

	// Resize observer for terminal fitting
	let resizeObserver: ResizeObserver | null = null;
	let resizeTimeout: ReturnType<typeof setTimeout> | null = null;

	onMount(() => {
		// Load xterm modules dynamically (client-side only)
		loadXtermModules();

		return () => {
			cleanup();
		};
	});

	onDestroy(() => {
		cleanup();
	});

	async function loadXtermModules() {
		try {
			const [xtermModule, fitModule, webLinksModule] = await Promise.all([
				import('@xterm/xterm'),
				import('@xterm/addon-fit'),
				import('@xterm/addon-web-links')
			]);

			XTerminal = xtermModule.Terminal;
			FitAddon = fitModule.FitAddon;
			WebLinksAddon = webLinksModule.WebLinksAddon;

			isTerminalInitialized = true;
		} catch (e) {
			console.error('Failed to load xterm modules:', e);
			error = 'Failed to load terminal components';
		}
	}

	// Get CSS variable value as computed color
	function getCssVar(name: string, fallback: string): string {
		if (typeof document === 'undefined') return fallback;
		const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
		return value || fallback;
	}

	// Build terminal theme from CSS variables
	function getTerminalTheme() {
		const bg = getCssVar('--terminal-bg', '#0d0d14');
		const fg = getCssVar('--terminal-fg', '#e4e4e7');
		return {
			background: bg,
			foreground: fg,
			cursor: getCssVar('--terminal-cursor', '#f5f5f5'),
			cursorAccent: bg,
			selectionBackground: getCssVar('--accent', '#3d3d5c'),
			black: bg,
			red: getCssVar('--terminal-red', '#ff6b6b'),
			green: getCssVar('--terminal-green', '#4ade80'),
			yellow: getCssVar('--terminal-yellow', '#fbbf24'),
			blue: getCssVar('--terminal-blue', '#60a5fa'),
			magenta: getCssVar('--terminal-magenta', '#c084fc'),
			cyan: getCssVar('--terminal-cyan', '#22d3ee'),
			white: fg,
			// Bright variants - slightly lighter/more saturated
			brightBlack: getCssVar('--muted-foreground', '#4a4a6a'),
			brightRed: getCssVar('--terminal-red', '#ff8a8a'),
			brightGreen: getCssVar('--terminal-green', '#6ee7a0'),
			brightYellow: getCssVar('--terminal-yellow', '#fcd34d'),
			brightBlue: getCssVar('--terminal-blue', '#93c5fd'),
			brightMagenta: getCssVar('--terminal-magenta', '#d8b4fe'),
			brightCyan: getCssVar('--terminal-cyan', '#67e8f9'),
			brightWhite: '#ffffff'
		};
	}

	function initTerminal() {
		if (!XTerminal || !FitAddon || !WebLinksAddon || !terminalContainer) {
			return;
		}

		// Clean up existing terminal
		if (terminal) {
			terminal.dispose();
		}

		terminal = new XTerminal({
			cursorBlink: true,
			fontSize: 13,
			fontFamily: getCssVar('--font-mono', "'SF Mono', 'Fira Code', 'Consolas', monospace"),
			theme: getTerminalTheme(),
			allowProposedApi: true
		});

		fitAddon = new FitAddon();
		terminal.loadAddon(fitAddon);
		terminal.loadAddon(new WebLinksAddon());

		terminal.open(terminalContainer);

		// Initial fit
		requestAnimationFrame(() => {
			fitAddon?.fit();
		});

		// Handle keyboard input
		terminal.onData((data: string) => {
			if (ws && ws.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'input', data }));
			}
		});

		// Handle special keys
		terminal.attachCustomKeyEventHandler((event: KeyboardEvent) => {
			if (event.key === 'Escape' && event.type === 'keydown') {
				// Send escape to terminal, don't close card
				if (ws && ws.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'key', key: 'escape' }));
				}
				return false;
			}
			return true;
		});

		// Set up resize observer
		setupResizeObserver();

		// Show welcome message
		terminal.writeln('\x1b[1;36m╭─────────────────────────────────────╮\x1b[0m');
		terminal.writeln('\x1b[1;36m│     Claude Code CLI Terminal        │\x1b[0m');
		terminal.writeln('\x1b[1;36m╰─────────────────────────────────────╯\x1b[0m');
		terminal.writeln('');
		terminal.writeln('\x1b[90mSelect a session and command, then click Connect.\x1b[0m');
		terminal.writeln('');
	}

	function setupResizeObserver() {
		if (resizeObserver) {
			resizeObserver.disconnect();
		}

		resizeObserver = new ResizeObserver(() => {
			// Debounce resize
			if (resizeTimeout) {
				clearTimeout(resizeTimeout);
			}
			resizeTimeout = setTimeout(() => {
				handleTerminalResize();
			}, 100);
		});

		if (terminalContainer) {
			resizeObserver.observe(terminalContainer);
		}
	}

	function handleTerminalResize() {
		if (!fitAddon || !terminal) return;

		try {
			fitAddon.fit();

			// Send resize to backend if connected
			if (ws && ws.readyState === WebSocket.OPEN) {
				ws.send(
					JSON.stringify({
						type: 'resize',
						cols: terminal.cols,
						rows: terminal.rows
					})
				);
			}
		} catch (e) {
			// Ignore resize errors during initialization
		}
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

	function connect() {
		if (!selectedSessionId) {
			error = 'Please select a session';
			return;
		}

		if (!terminal) {
			initTerminal();
		}

		error = null;
		hasStarted = true;

		// Update card dataId if needed
		if (card.dataId !== selectedSessionId) {
			deck.setCardDataId(card.id, selectedSessionId);
		}

		// Clear terminal and show connecting message
		terminal?.clear();
		terminal?.writeln('\x1b[90mConnecting...\x1b[0m');

		connectWebSocket();
	}

	function connectWebSocket() {
		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		let wsUrl = `${protocol}//${window.location.host}/api/v1/ws/cli/${selectedSessionId}`;

		const token = getAuthToken();
		if (token) {
			wsUrl = `${wsUrl}?token=${encodeURIComponent(token)}`;
		}

		ws = new WebSocket(wsUrl);

		ws.onopen = () => {
			isConnected = true;
			terminal?.writeln('\x1b[32m✓ Connected!\x1b[0m');
			terminal?.writeln(`\x1b[90mStarting ${selectedCommand}...\x1b[0m\n`);

			// Start the CLI command
			ws?.send(JSON.stringify({ type: 'start', command: selectedCommand }));

			// Send terminal size
			if (terminal && fitAddon) {
				ws?.send(
					JSON.stringify({
						type: 'resize',
						cols: terminal.cols,
						rows: terminal.rows
					})
				);
			}
		};

		ws.onmessage = (event) => {
			const data = JSON.parse(event.data);

			switch (data.type) {
				case 'output':
					terminal?.write(data.data);
					break;

				case 'ready':
					isReady = true;
					break;

				case 'error':
					error = data.message;
					terminal?.writeln(`\n\x1b[31m✗ Error: ${data.message}\x1b[0m`);
					break;

				case 'exit':
					terminal?.writeln(`\n\x1b[90m[Process exited with code ${data.exit_code}]\x1b[0m`);
					terminal?.writeln('\x1b[90mClick Connect to start a new session.\x1b[0m');
					isConnected = false;
					isReady = false;
					hasStarted = false;
					break;

				case 'rewind_complete':
					terminal?.writeln('\n\x1b[32m✓ Rewind operation completed!\x1b[0m');
					break;

				case 'ping':
					ws?.send(JSON.stringify({ type: 'pong' }));
					break;
			}
		};

		ws.onerror = () => {
			error = 'Connection error';
			terminal?.writeln('\n\x1b[31m✗ Connection error\x1b[0m');
		};

		ws.onclose = () => {
			isConnected = false;
			isReady = false;
			terminal?.writeln('\n\x1b[90m[Disconnected]\x1b[0m');
		};
	}

	function disconnect() {
		if (ws) {
			if (ws.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'stop' }));
			}
			ws.close();
			ws = null;
		}
		isConnected = false;
		isReady = false;
		hasStarted = false;
		terminal?.writeln('\n\x1b[90m[Disconnected by user]\x1b[0m');
	}

	function reconnect() {
		disconnect();
		setTimeout(() => {
			connect();
		}, 100);
	}

	function cleanup() {
		if (resizeTimeout) {
			clearTimeout(resizeTimeout);
		}

		if (resizeObserver) {
			resizeObserver.disconnect();
			resizeObserver = null;
		}

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

	// Initialize terminal when container is ready and modules are loaded
	$effect(() => {
		if (isTerminalInitialized && terminalContainer && !terminal) {
			initTerminal();
		}
	});
</script>

{#if mobile}
	<!-- Mobile: Full-screen terminal with no BaseCard wrapper -->
	<div class="terminal-card mobile">
		<!-- Toolbar -->
		<div class="terminal-toolbar">
			<div class="toolbar-left">
				<select
					bind:value={selectedSessionId}
					disabled={isConnected}
					class="session-select"
					aria-label="Select session"
				>
					<option value="">Select session...</option>
					{#each availableSessions as session}
						<option value={session.id}>{session.label}</option>
					{/each}
				</select>

				<select
					bind:value={selectedCommand}
					disabled={isConnected}
					class="command-select"
					aria-label="Select command"
				>
					{#each commands as cmd}
						<option value={cmd.value}>{cmd.label}</option>
					{/each}
				</select>
			</div>

			<div class="toolbar-right">
				{#if isConnected}
					<button onclick={disconnect} class="toolbar-btn disconnect" title="Disconnect">
						<Square size={14} />
						<span>Stop</span>
					</button>
					<button onclick={reconnect} class="toolbar-btn" title="Reconnect">
						<RefreshCw size={14} />
					</button>
				{:else}
					<button
						onclick={connect}
						class="toolbar-btn connect"
						disabled={!selectedSessionId}
						title="Connect"
					>
						<Play size={14} />
						<span>Connect</span>
					</button>
				{/if}

				<div class="status-indicator" class:connected={isConnected} class:error={!!error}>
					{#if error}
						<AlertCircle size={12} />
					{/if}
				</div>
			</div>
		</div>

		<!-- Terminal -->
		<div class="terminal-container" bind:this={terminalContainer}></div>

		<!-- Footer -->
		<div class="terminal-footer">
			<span><kbd>↑↓</kbd> Navigate</span>
			<span><kbd>Enter</kbd> Select</span>
			<span><kbd>Esc</kbd> Cancel</span>
		</div>
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard {card} {onClose} {onMaximize} {onFocus} {onMove} {onResize} {onDragEnd} {onResizeEnd}>
		<div class="terminal-card">
			<!-- Toolbar -->
			<div class="terminal-toolbar">
				<div class="toolbar-left">
					<select
						bind:value={selectedSessionId}
						disabled={isConnected}
						class="session-select"
						aria-label="Select session"
					>
						<option value="">Select session...</option>
						{#each availableSessions as session}
							<option value={session.id}>{session.label}</option>
						{/each}
					</select>

					<select
						bind:value={selectedCommand}
						disabled={isConnected}
						class="command-select"
						aria-label="Select command"
					>
						{#each commands as cmd}
							<option value={cmd.value}>{cmd.label}</option>
						{/each}
					</select>
				</div>

				<div class="toolbar-right">
					{#if isConnected}
						<button onclick={disconnect} class="toolbar-btn disconnect" title="Disconnect">
							<Square size={14} />
							<span>Stop</span>
						</button>
						<button onclick={reconnect} class="toolbar-btn" title="Reconnect">
							<RefreshCw size={14} />
						</button>
					{:else}
						<button
							onclick={connect}
							class="toolbar-btn connect"
							disabled={!selectedSessionId}
							title="Connect"
						>
							<Play size={14} />
							<span>Connect</span>
						</button>
					{/if}

					<div class="status-indicator" class:connected={isConnected} class:error={!!error}>
						{#if error}
							<AlertCircle size={12} />
						{/if}
					</div>
				</div>
			</div>

			<!-- Terminal -->
			<div class="terminal-container" bind:this={terminalContainer}></div>

			<!-- Footer -->
			<div class="terminal-footer">
				<span><kbd>↑↓</kbd> Navigate</span>
				<span><kbd>Enter</kbd> Select</span>
				<span><kbd>Esc</kbd> Cancel</span>
				<span><kbd>1-4</kbd> Options</span>
			</div>
		</div>
	</BaseCard>
{/if}

<style>
	.terminal-card {
		flex: 1;
		display: flex;
		flex-direction: column;
		background: oklch(0.06 0.01 260);
		color: var(--foreground);
		font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
		min-height: 0;
		overflow: hidden;
		border-radius: 0 0 12px 12px;
	}

	.terminal-card.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* Toolbar */
	.terminal-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 8px 12px;
		background: oklch(0.1 0.01 260);
		border-bottom: 1px solid oklch(0.15 0.01 260);
		flex-shrink: 0;
	}

	.toolbar-left {
		display: flex;
		align-items: center;
		gap: 8px;
		flex: 1;
		min-width: 0;
	}

	.toolbar-right {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-shrink: 0;
	}

	.session-select,
	.command-select {
		background: oklch(0.08 0.01 260);
		color: oklch(0.9 0.01 260);
		border: 1px solid oklch(0.2 0.01 260);
		border-radius: 6px;
		padding: 6px 10px;
		font-size: 0.75rem;
		font-family: inherit;
		cursor: pointer;
		min-width: 0;
	}

	.session-select {
		flex: 1;
		max-width: 200px;
	}

	.command-select {
		width: 120px;
	}

	.session-select:disabled,
	.command-select:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.session-select:focus,
	.command-select:focus {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 2px oklch(from var(--primary) l c h / 0.2);
	}

	.session-select option,
	.command-select option {
		background: oklch(0.12 0.01 260);
		color: oklch(0.9 0.01 260);
	}

	.toolbar-btn {
		display: flex;
		align-items: center;
		gap: 4px;
		padding: 6px 10px;
		background: oklch(0.15 0.01 260);
		color: oklch(0.85 0.01 260);
		border: 1px solid oklch(0.2 0.01 260);
		border-radius: 6px;
		font-size: 0.75rem;
		font-family: inherit;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.toolbar-btn:hover:not(:disabled) {
		background: oklch(0.2 0.01 260);
		border-color: oklch(0.25 0.01 260);
	}

	.toolbar-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.toolbar-btn.connect {
		background: oklch(0.35 0.15 145);
		border-color: oklch(0.4 0.15 145);
		color: white;
	}

	.toolbar-btn.connect:hover:not(:disabled) {
		background: oklch(0.4 0.15 145);
	}

	.toolbar-btn.disconnect {
		background: oklch(0.35 0.15 25);
		border-color: oklch(0.4 0.15 25);
		color: white;
	}

	.toolbar-btn.disconnect:hover {
		background: oklch(0.4 0.15 25);
	}

	.status-indicator {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		background: oklch(0.4 0.01 260);
		transition: all 0.2s ease;
	}

	.status-indicator.connected {
		background: oklch(0.65 0.2 145);
		box-shadow: 0 0 6px oklch(0.65 0.2 145);
	}

	.status-indicator.error {
		background: oklch(0.6 0.2 25);
		box-shadow: 0 0 6px oklch(0.6 0.2 25);
	}

	/* Terminal container */
	.terminal-container {
		flex: 1;
		min-height: 0;
		padding: 8px;
		overflow: hidden;
	}

	:global(.terminal-container .xterm) {
		height: 100%;
	}

	:global(.terminal-container .xterm-viewport) {
		overflow-y: auto !important;
	}

	/* Footer */
	.terminal-footer {
		display: flex;
		align-items: center;
		gap: 16px;
		padding: 6px 12px;
		background: oklch(0.1 0.01 260);
		border-top: 1px solid oklch(0.15 0.01 260);
		font-size: 0.7rem;
		color: oklch(0.5 0.01 260);
		flex-shrink: 0;
	}

	.terminal-footer kbd {
		display: inline-block;
		padding: 2px 5px;
		background: oklch(0.15 0.01 260);
		border-radius: 3px;
		font-family: inherit;
		font-size: 0.65rem;
		margin-right: 4px;
	}

	/* Light mode adjustments (terminal stays dark for authenticity) */
	:global(.light) .terminal-card {
		background: oklch(0.1 0.01 260);
	}

	:global(.light) .terminal-toolbar {
		background: oklch(0.14 0.01 260);
		border-color: oklch(0.2 0.01 260);
	}

	:global(.light) .terminal-footer {
		background: oklch(0.14 0.01 260);
		border-color: oklch(0.2 0.01 260);
	}
</style>
