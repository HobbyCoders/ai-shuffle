import assert from 'node:assert/strict';
import { it } from 'node:test';
import { runInNewContext } from 'node:vm';
import { installSessionEventDeliveryHold } from './event-delivery.mjs';

async function fixture(path = '/api/v1/sessions/created/events') {
	let controller, cancelled;
	const source = new ReadableStream({ start(value) { controller = value; }, cancel(reason) { cancelled = reason; } });
	const original = new Response(source, { status: 200, headers: { 'Content-Type': 'text/event-stream', 'X-Real': 'yes' } });
	const window = { fetch: async () => original };
	runInNewContext(`(${installSessionEventDeliveryHold})()`, {
		window, location: { href: 'http://journey.test/' }, URL, ReadableStream, Response, TextDecoder, setTimeout, clearTimeout
	});
	const response = await window.fetch(path);
	return { response, original, hold: window.__journeyEventDelivery, reader: response.body.getReader(),
		push(text) { controller.enqueue(new TextEncoder().encode(text)); }, close() { controller.close(); },
		get cancelled() { return cancelled; } };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const decode = value => new TextDecoder().decode(value);

it('buffers real bytes and counts held text across arbitrary chunk boundaries, releasing in order', async () => {
	const f = await fixture();
	assert.equal(f.response.headers.get('x-real'), 'yes');
	f.push(': live\n\n');
	assert.equal(decode((await f.reader.read()).value), ': live\n\n');
	f.hold.pause();
	try {
		const first = 'data: {"type":"assistant.text_delta","payload":{"text":"hel';
		const second = 'lo"}}\n\n: keepalive\n\n';
		f.push(first); f.push(second);
		await tick();
		assert.equal(f.hold.textChars, 5);
		let delivered = false;
		const waiting = f.reader.read().then(result => { delivered = true; return result; });
		await tick();
		assert.equal(delivered, false, 'the actual stream is held');
		f.hold.release();
		assert.equal(decode((await waiting).value), first);
		assert.equal(decode((await f.reader.read()).value), second);
		f.push('data: {"type":"caught_up"}\n\n');
		assert.equal(decode((await f.reader.read()).value), 'data: {"type":"caught_up"}\n\n');
	} finally { f.hold.release(); await f.reader.cancel(); }
});
it('does not wrap unrelated fetch responses', async () => {
	const f = await fixture('/api/v1/tags');
	assert.equal(f.response, f.original);
	await f.reader.cancel();
});
it('cancellation unregisters a held stream before release', async () => {
	const f = await fixture();
	f.hold.pause();
	f.push(': held\n\n'); await tick();
	await f.reader.cancel('case finished');
	assert.equal(f.cancelled, 'case finished');
	assert.doesNotThrow(() => f.hold.release());
});
it('stream completion forwards all held bytes rather than dropping the final frame', async () => {
	const f = await fixture();
	f.hold.pause();
	try {
		f.push('data: {"type":"turn.completed"}\n\n'); f.close();
		assert.equal(decode((await f.reader.read()).value), 'data: {"type":"turn.completed"}\n\n');
		assert.equal((await f.reader.read()).done, true);
		assert.doesNotThrow(() => f.hold.release());
	} finally { f.hold.release(); }
});
