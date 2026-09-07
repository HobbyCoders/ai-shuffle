/** Optional Android transport. The app injects this only into its worker's HTTPS document. */
interface NativeVoiceBridge {
	postMessage: (message: string) => void;
	addEventListener: (type: 'message', listener: (event: MessageEvent) => void) => void;
	removeEventListener: (type: 'message', listener: (event: MessageEvent) => void) => void;
}

let pending: { sessionId: string; result: Promise<boolean> } | null = null;
const UNAVAILABLE = 'Android voice did not open. Return to Shuffle and try Start voice again.';

/** Pass only the resolved session ID; native code uses its own authenticated worker connection. */
export function openNativeVoice(sessionId: string): Promise<boolean> {
	if (typeof window === 'undefined' || window !== window.top) return Promise.resolve(false);
	const bridge = (window as Window & { shuffleNativeVoice?: NativeVoiceBridge }).shuffleNativeVoice;
	if (!bridge) return Promise.resolve(false);
	if (![bridge.postMessage, bridge.addEventListener, bridge.removeEventListener].every(value => typeof value === 'function')) {
		return Promise.reject(new Error(UNAVAILABLE));
	}
	if (!/^[a-zA-Z0-9_-]{1,128}$/.test(sessionId)) return Promise.reject(new Error('Select a voice session before starting a call.'));
	if (pending) {
		return pending.sessionId === sessionId ? pending.result : Promise.reject(new Error('Another voice session is opening. Try again in a moment.'));
	}
	const id = crypto.randomUUID();
	const result = new Promise<boolean>((resolve, reject) => {
		let timer: ReturnType<typeof setTimeout> | undefined;
		function finish(error?: string) {
			clearTimeout(timer);
			try { bridge!.removeEventListener('message', onMessage); } catch { /* The document may have closed. */ }
			if (error) reject(new Error(error)); else resolve(true);
		}
		function onMessage(event: MessageEvent) {
			try {
				const reply = JSON.parse(event.data);
				if (reply.id !== id || reply.sessionId !== sessionId || typeof reply.ok !== 'boolean') return;
				finish(reply.ok ? undefined : typeof reply.error === 'string' && reply.error.length <= 250 ? reply.error : UNAVAILABLE);
			} catch { /* Ignore unrelated or malformed messages. */ }
		}
		try {
			bridge.addEventListener('message', onMessage);
			timer = setTimeout(() => finish(UNAVAILABLE), 5_000);
			bridge.postMessage(JSON.stringify({ type: 'start', id, sessionId }));
		} catch { finish(UNAVAILABLE); }
	});
	pending = { sessionId, result };
	void result.then(() => { if (pending?.result === result) pending = null; }, () => { if (pending?.result === result) pending = null; });
	return result;
}
