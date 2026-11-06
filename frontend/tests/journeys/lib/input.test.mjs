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
		['type', 'one', 700], ['key', 'Shift+Enter', 700],
		['key', 'Shift+Enter', 700], ['type', 'three', 700], ['send']
	]);
});

it('types literal punctuation and Unicode, with only the explicit Send click submitting', async () => {
	const { page, calls } = fixture();
	await typeAndSend(page, 'é <tag> [script:default]');
	assert.deepEqual(calls.filter(([kind]) => kind === 'type'), [['type', 'é <tag> [script:default]', 700]]);
	assert.deepEqual(calls.at(-1), ['send']);
	assert.ok(!calls.some(([kind]) => kind === 'fill'));
});
