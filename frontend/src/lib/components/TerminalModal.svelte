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

  function initTerminal() {
    terminal = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
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
  class="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
  onclick={handleBackdropClick}
  onkeydown={(e) => e.key === 'Escape' && handleClose()}
  role="dialog"
  aria-modal="true"
  aria-labelledby="terminal-title"
  tabindex="-1"
>
  <!-- Modal content -->
  <div class="bg-[#1a1a2e] rounded-lg shadow-2xl w-[90vw] max-w-4xl h-[70vh] flex flex-col overflow-hidden border border-gray-700">
    <!-- Header -->
    <div class="flex items-center justify-between px-4 py-2 bg-[#16162a] border-b border-gray-700">
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
        <h2 id="terminal-title" class="text-sm font-medium text-gray-300">
          Claude Code - {command}
        </h2>
      </div>
      <div class="flex items-center gap-2 text-xs">
        {#if isConnected}
          <span class="flex items-center gap-1 text-green-400">
            <span class="w-2 h-2 rounded-full bg-green-400"></span>
            Connected
          </span>
        {:else}
          <span class="flex items-center gap-1 text-yellow-400">
            <span class="w-2 h-2 rounded-full bg-yellow-400 animate-pulse"></span>
            Connecting...
          </span>
        {/if}
      </div>
    </div>

    <!-- Terminal container -->
    <div class="flex-1 p-2 overflow-hidden" bind:this={terminalContainer}></div>

    <!-- Footer with instructions -->
    <div class="px-4 py-2 bg-[#16162a] border-t border-gray-700 text-xs text-gray-400">
      <span class="mr-4"><kbd class="px-1.5 py-0.5 bg-gray-700 rounded">Arrow keys</kbd> Navigate</span>
      <span class="mr-4"><kbd class="px-1.5 py-0.5 bg-gray-700 rounded">Enter</kbd> Select</span>
      <span class="mr-4"><kbd class="px-1.5 py-0.5 bg-gray-700 rounded">Esc</kbd> Cancel/Exit</span>
      <span class="mr-4"><kbd class="px-1.5 py-0.5 bg-gray-700 rounded">1-4</kbd> Choose option</span>
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

  kbd {
    font-family: inherit;
  }
</style>
