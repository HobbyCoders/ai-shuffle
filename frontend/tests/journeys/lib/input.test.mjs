import assert from 'node:assert/strict';
import { it } from 'node:test';
import { typeAndSend } from './app.mjs';

function fixture() {
	const calls = [];
	// A slow focus round trip outlives Chromium's 500ms recent-input window.
	// fill() emits insertText but no keydown; keyboard entry must supply its own
	// input attribution rather than depending on the earlier click.
	let now = 0;
	const box = {
		async click() { calls.push(['focus', now]); now += 700; },
		async fill(text) { calls.push(['fill', text, now]); },
		async press(key) { calls.push(['key', key, now]); },
		async pressSequentially(text) { calls.push(['type', text, now]); }
	};
	const page = {
		keyboard: { async insertText(text) { calls.push(['literal', text, now]); } },
		getByRole(role, options) {
			if (role === 'textbox' && options.name === 'Message') return box;
			assert.equal(options.name, 'Send');
			return { async click() { calls.push(['send']); } };
		},
		async evaluate() { return {}; },
		// The optional later diagnostic snapshot need not keep this unit test alive.
		waitForTimeout() { return new Promise(() => {}); }
	};
	return { page, calls };
}

it('uses keyboard input after slow focus, preserving multiline text without submitting early', async () => {
	const { page, calls } = fixture();
	await typeAndSend(page, 'one\n\nthree');
	assert.deepEqual(calls, [
		['focus', 0], ['key', 'ControlOrMeta+A', 700], ['key', 'Backspace', 700],
		['type', 'one', 700], ['literal', '\n', 700],
		['literal', '\n', 700], ['type', 'three', 700], ['send']
	]);
});

it('types literal punctuation and Unicode, with only the explicit Send click submitting', async () => {
	const { page, calls } = fixture();
	await typeAndSend(page, 'é <tag> [script:default]');
	assert.deepEqual(calls.filter(([kind]) => kind === 'type'), [['type', 'é <tag> [script:default]', 700]]);
	assert.deepEqual(calls.at(-1), ['send']);
	assert.ok(!calls.some(([kind]) => kind === 'fill'));
});

it('bounds keyboard entry for large fixtures, attributing bulk growth before filling and preserving all text', async () => {
	const { page, calls } = fixture();
	const text = '😀 history\n' + 'history '.repeat(2000) + '\nfinish é';
	await typeAndSend(page, text);
	const fillAt = calls.findIndex(([kind]) => kind === 'fill');
	assert.ok(fillAt > 0, 'large text must use one bulk fill rather than thousands of key events');
	assert.equal(calls.filter(([kind]) => kind === 'fill').length, 1);
	assert.ok(calls.slice(0, fillAt).some(([kind]) => kind === 'type'), 'real keyboard input precedes bulk growth');
	assert.ok(calls.slice(fillAt + 1).some(([kind]) => kind === 'type'), 'finish with real keyboard input');
	assert.ok(calls.filter(([kind]) => kind === 'type').reduce((n, [, value]) => n + Array.from(value).length, 0) <= 32);
	let actual = '';
	for (const [kind, value] of calls) {
		if (kind === 'fill') actual = value;
		else if (kind === 'type') actual += value;
		else if (kind === 'literal') actual += value;
	}
	assert.equal(actual, text, 'bulk and typed portions preserve multiline Unicode exactly');
	assert.deepEqual(calls.at(-1), ['send']);
});

it('normalizes CRLF and bare CR before keyboard or bulk segmentation, with exactly one Send', async () => {
	for (const text of ['first\r\nsecond\rthird', '😀 first\r\n' + 'history '.repeat(2000) + '\r\nfinish é']) {
		const { page, calls } = fixture();
		await typeAndSend(page, text);
		let actual = '';
		for (const [kind, value] of calls) {
			if (kind === 'fill') actual = value;
			else if (kind === 'type' || kind === 'literal') actual += value;
			if (kind === 'type' || kind === 'fill') assert.ok(!value.includes('\r'), 'no CR may generate an Enter key');
		}
		assert.equal(actual, text.replace(/\r\n?/g, '\n'));
		assert.equal(calls.filter(([kind]) => kind === 'send').length, 1);
		assert.ok(!calls.some(([kind, key]) => kind === 'key' && key.includes('Enter')), 'literal newlines must not accept autocomplete');
		assert.deepEqual(calls.at(-1), ['send']);
	}
});
