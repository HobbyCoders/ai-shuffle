import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '../src/app.css';
import BrowserWorkspaceParent from './BrowserWorkspaceParent.svelte';
import OpenAICredits from '../src/lib/components/deck/cards/settings/providers/OpenAICredits.svelte';
import type { ModelProvider } from '../src/lib/api/model-providers';
const providerLogin = new URLSearchParams(location.search).has('provider-login');
let loginOpened = false;
let loginSocketUrl = '';
import { activeSession } from '../src/lib/stores/active-session';
import { browserPanel } from '../src/lib/stores/browser-panel.svelte';
import type { BrowserWorkspaceState } from '../src/lib/browser-panel/workspace';
// Only this test entry understands the optional ephemeral live-backend config.
const live = (window as unknown as {
	browserWorkspaceLive?: { api: string; session: string; token: string };
}).browserWorkspaceLive;
const messages: Record<string, unknown>[] = [];
let viewerId: string | null = null;
const sockets: FixtureSocket[] = [];
let frames = 0;
let frameMetadata: Record<string, unknown> | null = null;
let commandFailures = 0;
const NativeWebSocket = window.WebSocket;
class LiveSocket extends NativeWebSocket {
	constructor(url: string | URL, protocols?: string | string[]) {
		const target = new URL(url, location.href);
		const isBrowser = target.pathname.startsWith('/api/v1/ws/browser/');
		if (isBrowser && live) {
			const api = new URL(live.api);
			target.protocol = api.protocol === 'https:' ? 'wss:' : 'ws:';
			target.host = api.host;
			target.pathname = `/api/v1/ws/browser/${encodeURIComponent(live.session)}`;
			target.search = '';
			target.searchParams.set('token', live.token);
		}
		super(target, protocols);
		if (isBrowser) this.addEventListener('message', (event) => {
			// Observe real protocol traffic only; never synthesize state or pixels.
			const message = JSON.parse(event.data);
			if (message.type === 'hello') viewerId = message.viewer_id;
			if (message.type === 'workspace') state = message.state;
			if (message.type === 'frame') {
				frames++;
				frameMetadata = message.metadata;
			}
			if (message.type === 'command-result' && !message.ok) commandFailures++;
		});
	}
	override send(data: string | ArrayBufferLike | Blob | ArrayBufferView) {
		if (typeof data === 'string') {
			const message = JSON.parse(data);
			if (message.op) messages.push(message);
		}
		super.send(data);
	}
}
let state: BrowserWorkspaceState = {
	runtime_id: 'fixture-runtime',
	revision: 0,
	available: !live,
	target_id: null,
	active_tab_id: null,
	tabs: [],
	viewport: { mode: 'fixed', width: 1280, height: 800 },
	control: { owner: 'agent', viewer_id: null, pending_viewer_id: null },
	busy: false,
};
class FixtureSocket {
	static OPEN = 1;
	static CONNECTING = 0;
	static CLOSED = 3;
	static CLOSING = 2;
	heartbeat = setInterval(() => this.receive({ type: 'ping' }), 20_000);
	readyState = 1;
	onopen: (() => void) | null = null;
	onmessage: ((e: { data: string }) => void) | null = null;
	onclose: ((e: { code: number; target: unknown }) => void) | null = null;
	constructor(url: string) {
		sockets.push(this);
		if (providerLogin) {
			loginSocketUrl = url;
			queueMicrotask(() => { this.onopen?.(); loginOpened = true; });
			return;
		}
		queueMicrotask(() => {
			this.onopen?.();
			this.receive({
				type: 'hello',
				protocol: 2,
				viewer_id: 'fixture-viewer',
				capabilities: ['workspace-control'],
			});
			this.publish();
		});
	}
	receive(message: unknown) {
		this.onmessage?.({ data: JSON.stringify(message) });
	}
	publish() {
		this.receive({ type: 'workspace', state });
		this.receive({
			type: 'status',
			state: state.active_tab_id ? 'live' : 'no_page',
		});
	}
	send(raw: string) {
		const m = JSON.parse(raw);
		if (m.type === 'pong') return;
		messages.push(m);
		if (providerLogin) return;
		if (m.op === 'takeover')
			state.control = {
				owner: 'human',
				viewer_id: 'fixture-viewer',
				pending_viewer_id: null,
			};
		if (m.op === 'release')
			state.control = {
				owner: 'agent',
				viewer_id: null,
				pending_viewer_id: null,
			};
		if (m.op === 'viewport') state.viewport = m.args;
		if (m.op === 'tabs') {
			if (m.args.action === 'create') {
				const id = `tab-${state.tabs.length + 1}`;
				state.tabs.push({
					id,
					title: 'Workspace preview',
					url: 'https://example.test/preview',
					current: true,
				});
				state.active_tab_id = id;
				state.target_id = id;
			}
			if (m.args.action === 'select') state.active_tab_id = m.args.tab_id;
			if (m.args.action === 'close') {
				state.tabs = state.tabs.filter((t) => t.id !== m.args.tab_id);
				state.active_tab_id = state.tabs[0]?.id ?? null;
			}
			state.tabs.forEach((t) => (t.current = t.id === state.active_tab_id));
		}
		state = { ...state, revision: state.revision + 1 };
		this.publish();
		this.receive({ type: 'command-result', id: m.id, ok: true });
		if (state.active_tab_id) {
			this.frame();
		}
	}
	frame() {
		const c = document.createElement('canvas');
		c.width = state.viewport.width;
		c.height = state.viewport.height;
		const ctx = c.getContext('2d')!;
		ctx.fillStyle = '#e8eef7';
		ctx.fillRect(0, 0, c.width, c.height);
		ctx.fillStyle = '#24364b';
		ctx.font = '32px sans-serif';
		ctx.fillText('Workspace preview', 40, 75);
		ctx.font = '20px sans-serif';
		ctx.fillText(`${c.width} × ${c.height} CSS pixels`, 40, 120);
		this.receive({
			type: 'frame',
			data: c.toDataURL('image/jpeg').split(',')[1],
			metadata: {
				deviceWidth: c.width,
				deviceHeight: c.height,
				...(providerLogin ? {} : { runtime_id: state.runtime_id, tab_id: state.active_tab_id }),
			},
		});
	}
	close(code = 1000) {
		clearInterval(this.heartbeat);
		this.readyState = 3;
		this.onclose?.({ code, target: this });
	}
}
Object.assign(window, {
	WebSocket: live ? LiveSocket : FixtureSocket,
	browserFixture: {
		messages,
		get loginOpened() { return loginOpened; },
		get loginSocketUrl() { return loginSocketUrl; },
		loginHello() { sockets.at(-1)!.receive({ type: 'hello', protocol: 1 }); },
		loginLive() {
			const socket = sockets.at(-1)!;
			const url = 'https://auth.example.test/private-login?secret=never-display';
			socket.receive({ type: 'page', url, loading: false });
			socket.receive({ type: 'status', state: 'live', url });
			socket.frame();
		},
		get socketCount() { return sockets.length; },
		get openSockets() { return sockets.filter(s => s.readyState === 1).length; },
		// Seed identity without starting the unrelated chat transport. Notifications
		// and same-ID replacements use the real store's public update method.
		setSession(id: string) {
			activeSession.current.sessionId = id;
			activeSession.setWorktreeBranch('fixture');
		},
		churn(index: number) {
			activeSession.setWorktreeBranch(`stream-update-${index}`);
			state = { ...state, revision: state.revision + 1, busy: index % 2 === 0 };
			const socket = sockets.at(-1)!;
			socket.publish();
			socket.frame();
		},
		get viewerId() { return viewerId; },
		get frames() { return frames; },
		get frameMetadata() { return frameMetadata; },
		get commandFailures() { return commandFailures; },
		get state() {
			return state;
		},
		setWidth: (width: number) => browserPanel.setWidth(width),
	},
});
activeSession.current.sessionId = live?.session ?? 'isolated-fixture';
if (providerLogin) {
	mount(OpenAICredits, {
		target: document.getElementById('app')!,
		props: { provider: { id: 'p1', name: 'OpenAI', enabled: true, provider_type: 'openai', base_url: 'https://api.openai.com/v1' } as ModelProvider },
	});
} else {
	browserPanel.openPanel();
	mount(BrowserWorkspaceParent, { target: document.getElementById('app')! });
}
