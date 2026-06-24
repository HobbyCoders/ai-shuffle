<script lang="ts">
	/**
	 * Live, interactive view of the headless container browser over CDP.
	 *
	 * Renders Page.screencastFrame JPEGs and forwards mouse/keyboard back as
	 * Input.dispatchMouseEvent / Input.insertText / Input.dispatchKeyEvent, all
	 * through the authenticated /api/v1/chrome-mcp/cdp WebSocket proxy. Used for
	 * the one-time Claude in Chrome sign-in/pairing.
	 */
	import { chromeCdpWsUrl } from '$lib/api/chromeMcp';

	interface Props {
		targetId?: string;
		/** Reported connection state: 'connecting' | 'live' | 'closed' | 'error' */
		onState?: (state: string, detail?: string) => void;
	}

	let { targetId, onState }: Props = $props();

	let frameSrc = $state<string | null>(null);
	let connState = $state<'connecting' | 'live' | 'closed' | 'error'>('connecting');
	let imgEl: HTMLImageElement | null = $state(null);

	// CDP viewport metadata from the latest frame (for input coordinate mapping).
	let meta = { deviceWidth: 0, deviceHeight: 0, offsetTop: 0, pageScaleFactor: 1 };

	let ws: WebSocket | null = null;
	let cmdId = 0;
	let buttons = 0; // CDP buttons bitmask currently pressed

	const KEY_MAP: Record<string, { code: string; vk: number; text?: string }> = {
		Enter: { code: 'Enter', vk: 13, text: '\r' },
		Backspace: { code: 'Backspace', vk: 8 },
		Tab: { code: 'Tab', vk: 9 },
		Escape: { code: 'Escape', vk: 27 },
		Delete: { code: 'Delete', vk: 46 },
		ArrowUp: { code: 'ArrowUp', vk: 38 },
		ArrowDown: { code: 'ArrowDown', vk: 40 },
		ArrowLeft: { code: 'ArrowLeft', vk: 37 },
		ArrowRight: { code: 'ArrowRight', vk: 39 },
		Home: { code: 'Home', vk: 36 },
		End: { code: 'End', vk: 35 }
	};

	function setState(s: typeof connState, detail?: string) {
		connState = s;
		onState?.(s, detail);
	}

	function send(method: string, params: Record<string, unknown> = {}) {
		if (ws && ws.readyState === WebSocket.OPEN) {
			ws.send(JSON.stringify({ id: ++cmdId, method, params }));
		}
	}

	/** Navigate the attached page to a URL (used by the parent panel). */
	export function navigate(url: string) {
		send('Page.navigate', { url });
	}

	function modifiers(e: MouseEvent | KeyboardEvent): number {
		return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
	}

	function toPage(e: MouseEvent): { x: number; y: number } {
		if (!imgEl || !meta.deviceWidth || !meta.deviceHeight) return { x: 0, y: 0 };
		const rect = imgEl.getBoundingClientRect();
		const x = ((e.clientX - rect.left) / rect.width) * meta.deviceWidth;
		const y = ((e.clientY - rect.top) / rect.height) * meta.deviceHeight;
		return { x: Math.max(0, Math.round(x)), y: Math.max(0, Math.round(y)) };
	}

	function cdpButton(b: number): string {
		return b === 0 ? 'left' : b === 1 ? 'middle' : b === 2 ? 'right' : 'none';
	}

	function onPointerDown(e: MouseEvent) {
		e.preventDefault();
		imgEl?.focus();
		const bit = e.button === 0 ? 1 : e.button === 2 ? 2 : e.button === 1 ? 4 : 0;
		buttons |= bit;
		const { x, y } = toPage(e);
		send('Input.dispatchMouseEvent', {
			type: 'mousePressed',
			x,
			y,
			button: cdpButton(e.button),
			buttons,
			clickCount: 1,
			modifiers: modifiers(e)
		});
	}

	function onPointerUp(e: MouseEvent) {
		e.preventDefault();
		const bit = e.button === 0 ? 1 : e.button === 2 ? 2 : e.button === 1 ? 4 : 0;
		buttons &= ~bit;
		const { x, y } = toPage(e);
		send('Input.dispatchMouseEvent', {
			type: 'mouseReleased',
			x,
			y,
			button: cdpButton(e.button),
			buttons,
			clickCount: 1,
			modifiers: modifiers(e)
		});
	}

	function onPointerMove(e: MouseEvent) {
		const { x, y } = toPage(e);
		send('Input.dispatchMouseEvent', {
			type: 'mouseMoved',
			x,
			y,
			button: buttons ? 'left' : 'none',
			buttons,
			modifiers: modifiers(e)
		});
	}

	function onWheel(e: WheelEvent) {
		e.preventDefault();
		const { x, y } = toPage(e);
		send('Input.dispatchMouseEvent', {
			type: 'mouseWheel',
			x,
			y,
			deltaX: -e.deltaX,
			deltaY: -e.deltaY,
			modifiers: modifiers(e)
		});
	}

	function onKeyDown(e: KeyboardEvent) {
		// Let the parent handle nothing global; we own keys while focused.
		const special = KEY_MAP[e.key];
		if (special) {
			e.preventDefault();
			const base = {
				key: e.key,
				code: special.code,
				windowsVirtualKeyCode: special.vk,
				nativeVirtualKeyCode: special.vk,
				modifiers: modifiers(e)
			};
			send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...base, text: special.text });
			if (special.text) send('Input.dispatchKeyEvent', { type: 'char', ...base, text: special.text });
			send('Input.dispatchKeyEvent', { type: 'keyUp', ...base });
			return;
		}
		// Printable character (no ctrl/meta) → insert as typed text.
		if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
			e.preventDefault();
			send('Input.insertText', { text: e.key });
		}
	}

	function teardown() {
		if (ws) {
			try {
				ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
				ws.close();
			} catch {
				/* ignore */
			}
			ws = null;
		}
	}

	function connect(id?: string) {
		teardown();
		frameSrc = null;
		buttons = 0;
		setState('connecting');
		let sock: WebSocket;
		try {
			sock = new WebSocket(chromeCdpWsUrl(id));
		} catch (e) {
			setState('error', String(e));
			return;
		}
		ws = sock;
		sock.onopen = () => {
			setState('live');
			send('Page.enable');
			send('Page.startScreencast', {
				format: 'jpeg',
				quality: 60,
				maxWidth: 1280,
				maxHeight: 800,
				everyNthFrame: 1
			});
		};
		sock.onmessage = (ev) => {
			let msg: any;
			try {
				msg = JSON.parse(typeof ev.data === 'string' ? ev.data : '');
			} catch {
				return;
			}
			if (msg?.method === 'Page.screencastFrame') {
				const p = msg.params || {};
				if (p.metadata) {
					meta = {
						deviceWidth: p.metadata.deviceWidth || meta.deviceWidth,
						deviceHeight: p.metadata.deviceHeight || meta.deviceHeight,
						offsetTop: p.metadata.offsetTop || 0,
						pageScaleFactor: p.metadata.pageScaleFactor || 1
					};
				}
				frameSrc = `data:image/jpeg;base64,${p.data}`;
				if (p.sessionId != null) send('Page.screencastFrameAck', { sessionId: p.sessionId });
			}
		};
		sock.onclose = (ev) => {
			if (ws === sock) ws = null;
			setState(ev.code === 4001 ? 'error' : 'closed', ev.reason);
		};
		sock.onerror = () => setState('error', 'WebSocket error');
	}

	// (Re)connect whenever the target changes.
	$effect(() => {
		const id = targetId;
		connect(id);
		return () => teardown();
	});
</script>

<div class="screencast">
	{#if frameSrc}
		<!-- The screencast image must be focusable to forward keyboard input to the
		     remote page; it is the interactive surface, so the a11y hints don't apply. -->
		<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
		<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
		<img
			bind:this={imgEl}
			src={frameSrc}
			alt="Live browser"
			draggable="false"
			tabindex="0"
			onmousedown={onPointerDown}
			onmouseup={onPointerUp}
			onmousemove={onPointerMove}
			onwheel={onWheel}
			onkeydown={onKeyDown}
			oncontextmenu={(e) => e.preventDefault()}
		/>
	{:else}
		<div class="placeholder">
			{#if connState === 'connecting'}
				Connecting to the browser…
			{:else if connState === 'error'}
				Couldn’t connect to the browser. Check that Chrome is running with CHROME_CDP=1.
			{:else if connState === 'closed'}
				Disconnected.
			{:else}
				Waiting for the first frame…
			{/if}
		</div>
	{/if}
</div>

<style>
	.screencast {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 100%;
		height: 100%;
		min-height: 320px;
		background: #1a1a1a;
		border-radius: 8px;
		overflow: hidden;
	}
	.screencast img {
		max-width: 100%;
		max-height: 100%;
		display: block;
		cursor: default;
		outline: none;
		user-select: none;
		-webkit-user-drag: none;
	}
	.placeholder {
		color: var(--muted-foreground, #aaa);
		font-size: 13px;
		padding: 24px;
		text-align: center;
	}
</style>
