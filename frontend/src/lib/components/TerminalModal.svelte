<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import '@xterm/xterm/css/xterm.css';

  // Dynamic imports for client-side only xterm modules
  let Terminal: any;
  let FitAddon: any;
  let WebLinksAddon: any;

  interface Props {
    sessionId: string;
    command?: string;
    onClose: () => void;
    onRewindComplete?: (checkpointMessage: string | null, selectedOption: number | null) => void;
  }

  let { sessionId, command = '/rewind', onClose, onRewindComplete }: Props = $props();

  let terminalContainer: HTMLDivElement;
  let terminal: any = null;
  let fitAddon: any = null;
  let ws: WebSocket | null = null;
  let isConnected = $state(false);
  let isReady = $state(false);
  let error = $state<string | null>(null);

  onMount(() => {
    // Dynamic import xterm modules only on client side
    (async () => {
      const xtermModule = await import('@xterm/xterm');
      const fitModule = await import('@xterm/addon-fit');
      const webLinksModule = await import('@xterm/addon-web-links');

      Terminal = xtermModule.Terminal;
      FitAddon = fitModule.FitAddon;
      WebLinksAddon = webLinksModule.WebLinksAddon;

      initTerminal();
      connectWebSocket();
    })();

    // Handle window resize
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
    };
  });

  onDestroy(() => {
    cleanup();
  });

  // Get CSS variable value as computed color
  function getCssVar(name: string, fallback: string): string {
    if (typeof document === 'undefined') return fallback;
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  // Build terminal theme from CSS variables
  function getTerminalTheme() {
    const bg = getCssVar('--terminal-bg', '#1a1a2e');
    const fg = getCssVar('--terminal-fg', '#eaeaea');
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
    terminal = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: getCssVar('--font-mono', 'Menlo, Monaco, "Courier New", monospace'),
      theme: getTerminalTheme(),
      allowProposedApi: true
    });

    fitAddon = new FitAddon();
    terminal.loadAddon(fitAddon);
    terminal.loadAddon(new WebLinksAddon());

    terminal.open(terminalContainer);
    fitAddon.fit();

    // Handle keyboard input
    terminal.onData((data: string) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data }));
      }
    });

    // Handle special keys
    terminal.attachCustomKeyEventHandler((event: KeyboardEvent) => {
      // Handle Escape to close
      if (event.key === 'Escape' && event.type === 'keydown') {
        // Send escape to terminal first
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'key', key: 'escape' }));
        }
        return false; // Don't close modal on escape - let CLI handle it
      }
      return true;
    });

    terminal.writeln('\x1b[1;36m=== Claude Code CLI Terminal ===\x1b[0m');
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
    // Cookie might be httpOnly, return null and let server check cookie directly
    return null;
  }

  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    let wsUrl = `${protocol}//${window.location.host}/api/v1/ws/cli/${sessionId}`;

    // Add auth token if available (same as chat WebSocket)
    const token = getAuthToken();
    if (token) {
      wsUrl = `${wsUrl}?token=${encodeURIComponent(token)}`;
    }

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      isConnected = true;
      terminal?.writeln('\x1b[32mConnected!\x1b[0m');
      terminal?.writeln(`\x1b[90mStarting ${command}...\x1b[0m\n`);

      // Start the CLI command
      ws?.send(JSON.stringify({ type: 'start', command }));

      // Send terminal size
      if (terminal && fitAddon) {
        ws?.send(JSON.stringify({
          type: 'resize',
          cols: terminal.cols,
          rows: terminal.rows
        }));
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
          terminal?.writeln(`\n\x1b[31mError: ${data.message}\x1b[0m`);
          break;

        case 'exit':
          terminal?.writeln(`\n\x1b[90m[Process exited with code ${data.exit_code}]\x1b[0m`);
          terminal?.writeln('\x1b[90mPress any key to close...\x1b[0m');
          // Auto-close after a short delay
          setTimeout(() => {
            onClose();
          }, 1500);
          break;

        case 'rewind_complete':
          terminal?.writeln('\n\x1b[32mRewind operation completed!\x1b[0m');
          if (onRewindComplete) {
            onRewindComplete(data.checkpoint_message, data.selected_option);
          }
          break;

        case 'ping':
          ws?.send(JSON.stringify({ type: 'pong' }));
          break;
      }
    };

    ws.onerror = (event) => {
      error = 'WebSocket error';
      terminal?.writeln('\n\x1b[31mConnection error\x1b[0m');
    };

    ws.onclose = () => {
      isConnected = false;
      terminal?.writeln('\n\x1b[90m[Disconnected]\x1b[0m');
    };
  }

  function handleResize() {
    if (fitAddon && terminal && ws && ws.readyState === WebSocket.OPEN) {
      fitAddon.fit();
      ws.send(JSON.stringify({
        type: 'resize',
        cols: terminal.cols,
        rows: terminal.rows
      }));
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

  function handleClose() {
    cleanup();
    onClose();
  }

  function handleBackdropClick(event: MouseEvent) {
    if (event.target === event.currentTarget) {
      handleClose();
    }
  }
</script>

<!-- Modal backdrop -->
<div
  class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
  onclick={handleBackdropClick}
  onkeydown={(e) => e.key === 'Escape' && handleClose()}
  role="dialog"
  aria-modal="true"
  aria-labelledby="terminal-title"
  tabindex="-1"
>
  <!-- Modal content -->
  <div class="terminal-modal-container">
    <!-- Header -->
    <div class="terminal-modal-header">
      <div class="flex items-center gap-3">
        <div class="flex gap-1.5">
          <button
            onclick={handleClose}
            class="w-3 h-3 rounded-full bg-red-500 hover:bg-red-400 transition-colors"
            title="Close"
          ></button>
          <div class="w-3 h-3 rounded-full bg-yellow-500"></div>
          <div class="w-3 h-3 rounded-full bg-green-500"></div>
        </div>
        <h2 id="terminal-title" class="terminal-modal-title">
          Claude Code - {command}
        </h2>
      </div>
      <div class="flex items-center gap-2 text-xs">
        {#if isConnected}
          <span class="terminal-status-connected">
            <span class="terminal-status-dot connected"></span>
            Connected
          </span>
        {:else}
          <span class="terminal-status-connecting">
            <span class="terminal-status-dot connecting"></span>
            Connecting...
          </span>
        {/if}
      </div>
    </div>

    <!-- Terminal container -->
    <div class="flex-1 p-2 overflow-hidden" bind:this={terminalContainer}></div>

    <!-- Footer with instructions -->
    <div class="terminal-modal-footer">
      <span class="mr-4"><kbd class="terminal-kbd">Arrow keys</kbd> Navigate</span>
      <span class="mr-4"><kbd class="terminal-kbd">Enter</kbd> Select</span>
      <span class="mr-4"><kbd class="terminal-kbd">Esc</kbd> Cancel/Exit</span>
      <span class="mr-4"><kbd class="terminal-kbd">1-4</kbd> Choose option</span>
    </div>
  </div>
</div>

<style>
  :global(.xterm) {
    height: 100%;
    padding: 8px;
  }

  :global(.xterm-viewport) {
    overflow-y: auto !important;
  }

  .terminal-modal-container {
    background: var(--terminal-bg);
    border-radius: 0.5rem;
    box-shadow: var(--shadow-xl);
    width: 90vw;
    max-width: 64rem;
    height: 70vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    border: 1px solid var(--border);
  }

  .terminal-modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.5rem 1rem;
    background: color-mix(in oklch, var(--terminal-bg) 80%, black);
    border-bottom: 1px solid var(--border);
  }

  .terminal-modal-title {
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--terminal-fg);
    opacity: 0.85;
  }

  .terminal-modal-footer {
    padding: 0.5rem 1rem;
    background: color-mix(in oklch, var(--terminal-bg) 80%, black);
    border-top: 1px solid var(--border);
    font-size: 0.75rem;
    color: var(--muted-foreground);
  }

  .terminal-kbd {
    padding: 0.125rem 0.375rem;
    background: var(--muted);
    border-radius: 0.25rem;
    font-family: inherit;
  }

  .terminal-status-connected {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    color: var(--success);
  }

  .terminal-status-connecting {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    color: var(--warning);
  }

  .terminal-status-dot {
    width: 0.5rem;
    height: 0.5rem;
    border-radius: 50%;
  }

  .terminal-status-dot.connected {
    background: var(--success);
  }

  .terminal-status-dot.connecting {
    background: var(--warning);
    animation: pulse 1s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
</style>
