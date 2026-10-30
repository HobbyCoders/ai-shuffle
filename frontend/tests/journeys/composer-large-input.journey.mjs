// Journey: newline-heavy and very large text in the composer (#912).
//
// Guards: a ~1 KB message of a thousand blank lines, filled (typed input:
// an IME commit, dictation or automation) or pasted from the clipboard, lands
// as one editor change instead of one browser edit per line (Chrome's native
// path fired ~1,000 `input` events and froze the tab for 11.7 s at 4x CPU).
// The field stays on screen at its capped height, the editable keeps its full
// content height inside it (it was shrunk to the cap and scrolled to y=-11,202),
// the caret stays visible, and the message sends once. A 200 KB plain paste
// (Ctrl/Cmd+Shift+V keeps any paste inline) does the same.
//
// The counts are the ratchet, not wall clocks: `input` events during the fill
// or paste, and where the caret and field are afterwards.
//
// Cases:
//   fill-newlines        fill the issue's input, check, send; reply settles
//   paste-newlines       the same text from the clipboard with Ctrl+V
//   plain-paste-200k     200 KB with Ctrl+Shift+V stays inline and usable
//
// --mobile-shell runs the phone variants in the built /mobile/ app (CI's
// hosted journeys build only the shared UI).
import { fakeAiTag, runJourney } from './runner.mjs';
import { composer, countUserRows, expect, gotoApp, seededChatId, sendButton, uniq, waitChatOpen, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
const BLANK_LINES = 1000;
const issueText = (first) => `${first}${'\n'.repeat(BLANK_LINES)}<system-reminder>\n# Memory\nunfinished`;

function bigLog() {
	const lines = [];
	let size = 0;
	for (let i = 0; size < 200 * 1024; i++) {
		const line = `line ${i} of a pasted build log with a few words${i % 7 === 0 ? '\n\n' : ''}`;
		lines.push(line);
		size += line.length + 1;
	}
	return lines.join('\n');
}

const openChat = async ({ page, status, variant }) => {
	const prefix = mobileShell && variant.viewport === 'phone' ? '/mobile/' : '/';
	const path = `${prefix}?session=${encodeURIComponent(seededChatId(status, 'no_project'))}`;
	if (prefix === '/') await gotoApp(page, status, path);
	else {
		await page.goto(new URL(path, status.urls.ui).toString());
		await composer(page).waitFor({ timeout: 90_000 });
	}
	await waitChatOpen(page, { messagesAtLeast: 2, hold: 1000 });
	if (mobileShell && variant.viewport === 'phone') {
		expect(new URL(page.url()).pathname === '/mobile/', 'the opt-in case must exercise the real mobile shell');
	}
	// Count the editor's `input` events from here on (every native edit fires one).
	await page.evaluate(() => {
		const box = document.querySelector('[role="textbox"][aria-label="Message"]');
		window.__composerInputs = 0;
		box.addEventListener('input', () => { window.__composerInputs += 1; });
	});
};

/** Where the field, the editable and the caret are, and how many `input` events fired. */
const composerState = (page) =>
	page.evaluate(() => {
		const box = document.querySelector('[role="textbox"][aria-label="Message"]');
		const field = box.parentElement;
		const editor = box.editor; // Tiptap puts its Editor on the editable
		const f = field.getBoundingClientRect();
		const b = box.getBoundingClientRect();
		const caret = editor.view.coordsAtPos(editor.state.selection.head);
		const viewportHeight = window.visualViewport?.height ?? innerHeight;
		return {
			inputs: window.__composerInputs,
			chars: editor.getText().length,
			field: { top: Math.round(f.top), bottom: Math.round(f.bottom), height: Math.round(f.height), scrollHeight: field.scrollHeight },
			editable: { top: Math.round(b.top), height: Math.round(b.height) },
			caret: { top: Math.round(caret.top), bottom: Math.round(caret.bottom) },
			viewportHeight: Math.round(viewportHeight)
		};
	});

function expectUsable(state, what) {
	const { field, editable, caret, viewportHeight } = state;
	const s = JSON.stringify(state);
	expect(field.top >= 0 && field.bottom <= viewportHeight + 1, `${what}: the field is on screen (${s})`);
	expect(field.height < viewportHeight / 2, `${what}: the field stays at its capped height (${s})`);
	expect(editable.height >= field.scrollHeight - 2, `${what}: the editable keeps its full content height, not shrunk to the field (${s})`);
	expect(caret.top >= field.top - 1 && caret.bottom <= field.bottom + 1, `${what}: the caret is visible in the field (${s})`);
}

async function clearComposer(page) {
	// Focus, not click: the editable is now taller than the screen and its
	// centre (where a click aims) lies outside the field's visible part.
	await composer(page).focus();
	await page.keyboard.press('ControlOrMeta+A');
	await page.keyboard.press('Backspace');
	await page.waitForFunction(() => document.querySelector('[role="textbox"][aria-label="Message"]').editor.isEmpty, null, { timeout: 10_000 });
}

await runJourney({
	name: 'composer-large-input',
	// The clipboard API needs a secure context: CI's loopback instance is one, a
	// LAN dev instance (http://<worker-ip>) is not without this flag.
	browser: (status) => ({
		args: [`--unsafely-treat-insecure-origin-as-secure=${new URL(status.urls.ui).origin}`],
		context: { permissions: ['clipboard-read', 'clipboard-write'] }
	}),
	cases: [
		{
			name: 'fill-newlines',
			setup: openChat,
			async act({ page, mark, variant }) {
				const token = `tok-${uniq()}`;
				const text = issueText(`Linear reminder regression ${token} ${fakeAiTag(variant)}`.trim());
				await mark('fill a newline-heavy message');
				await composer(page).click();
				await composer(page).fill(text);
				await page.waitForTimeout(500);
				const filled = await composerState(page);
				expect(filled.inputs <= 1, `the fill is one editor change, not one browser edit per line (${filled.inputs} input events)`);
				expect(filled.chars === text.length, `the whole message is in the composer (${filled.chars} of ${text.length})`);
				expectUsable(filled, 'after the fill');
				expect(await sendButton(page).isEnabled(), 'Send is enabled');

				await mark('send');
				await sendButton(page).click();
				await waitTurnSettled(page, { messagesAtLeast: 4 });
				expect((await countUserRows(page, token)) === 1, 'the message shows once');
				expect((await composer(page).innerText()).trim() === '', 'the composer is empty after the send');
			}
		},
		{
			name: 'paste-newlines',
			setup: openChat,
			async act({ page, mark }) {
				const text = issueText(`Pasted reminder ${uniq()}`);
				await page.evaluate((t) => navigator.clipboard.writeText(t), text);
				await mark('paste a newline-heavy message');
				await composer(page).click();
				await page.keyboard.press('ControlOrMeta+V');
				await page.waitForFunction((n) => document.querySelector('[role="textbox"][aria-label="Message"]').editor.getText().length >= n, text.length, { timeout: 30_000 });
				await page.waitForTimeout(500);
				const pasted = await composerState(page);
				expect(pasted.inputs <= 1, `the paste is one editor change (${pasted.inputs} input events)`);
				expectUsable(pasted, 'after the paste');

				await mark('clear the composer');
				await clearComposer(page);
			}
		},
		{
			name: 'plain-paste-200k',
			setup: openChat,
			async act({ page, mark }) {
				const text = bigLog();
				await page.evaluate((t) => navigator.clipboard.writeText(t), text);
				await mark('paste 200 KB inline (Ctrl+Shift+V)');
				await composer(page).click();
				await page.keyboard.press('ControlOrMeta+Shift+V');
				await page.waitForFunction((n) => document.querySelector('[role="textbox"][aria-label="Message"]').editor.getText().length >= n, text.length, { timeout: 30_000 });
				await page.waitForTimeout(500);
				const pasted = await composerState(page);
				expect(pasted.inputs <= 1, `the paste is one editor change (${pasted.inputs} input events)`);
				expectUsable(pasted, 'after the 200 KB paste');

				await mark('type after it');
				await page.keyboard.type(' end');
				await page.waitForTimeout(300);
				expectUsable(await composerState(page), 'after typing at the end');

				await mark('clear the composer');
				await clearComposer(page);
			}
		}
	],
	allow: [...DEV_INSTANCE_NOISE]
});
