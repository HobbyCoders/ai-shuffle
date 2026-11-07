// Delay delivery, never production, of real session SSE bytes. Used to make the
// growing-reply/older-paging race deterministic without seeding a synthetic feed.
// Self-contained: Playwright serializes this function into each new document.
export function installSessionEventDeliveryHold() {
	const hold = window.__journeyEventDelivery = { paused: false, textChars: 0, safetyReleased: false };
	const streams = new Set();
	let safety;
	hold.pause = () => {
		hold.paused = true;
		hold.textChars = 0;
		safety = setTimeout(() => { hold.safetyReleased = true; hold.release(); }, 20_000);
	};
	hold.release = () => {
		clearTimeout(safety);
		hold.paused = false;
		for (const stream of streams) stream.flush();
	};
	const original = window.fetch.bind(window);
	window.fetch = async (...args) => {
		const response = await original(...args);
		const input = args[0];
		const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
		if (!/^\/api\/v1\/sessions\/[^/]+\/events$/.test(path) || !response.ok || !response.body) return response;
		const reader = response.body.getReader();
		let cancelled = false;
		const queue = [];
		let registration;
		const stream = new ReadableStream({
			start(controller) {
				registration = { flush() { for (const chunk of queue.splice(0)) controller.enqueue(chunk); } };
				streams.add(registration);
				const decoder = new TextDecoder();
				let partial = '';
				(async () => {
					try {
						while (!cancelled) {
							const { done, value } = await reader.read();
							if (cancelled) return;
							if (done) { registration.flush(); controller.close(); return; }
							// Count actual assistant text, not transport chunk boundaries.
							// Observation only: forward every original byte unchanged/in order.
							partial += decoder.decode(value, { stream: true });
							const lines = partial.split('\n');
							partial = lines.pop();
							for (const line of lines) {
								if (!hold.paused || !line.startsWith('data:')) continue;
								const event = JSON.parse(line.slice(5));
								if (event.type === 'assistant.text_delta') hold.textChars += (event.payload?.text ?? event.text ?? '').length;
							}
							if (hold.paused) queue.push(value); else controller.enqueue(value);
						}
					} catch (error) {
						if (!cancelled) controller.error(error);
					} finally { streams.delete(registration); }
				})();
			},
			cancel(reason) {
				cancelled = true;
				streams.delete(registration);
				queue.length = 0;
				return reader.cancel(reason);
			}
		});
		return new Response(stream, { status: response.status, statusText: response.statusText, headers: response.headers });
	};
}
