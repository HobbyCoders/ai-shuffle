// @ts-nocheck — standalone browser verification, asserts its own fixture contract.
// Executable in the isolated browser fixture:
// await (await import('/tests/composer-suggestion.verify.js')).verifyComposerSuggestions()
export async function verifyComposerSuggestions() {
	const f = window.suggestionFixture;
	const results = [];
	const check = (condition, label) => {
		if (!condition) throw new Error(label);
		results.push(label);
	};
	const flush = async () => {
		await Promise.resolve();
		await Promise.resolve();
		await Promise.resolve();
	};
	const editor = () => document.querySelector('[contenteditable="true"]');
	const ghost = () => document.querySelector('[data-testid="composer-suggestion"]');
	const key = async (key, extra = {}) => {
		const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...extra });
		editor().dispatchEvent(ev);
		await flush();
		return ev;
	};
	const clear = async () => {
		await f.clear();
		await flush();
	};
	const offer = async () => {
		await clear();
		await f.offer();
		await flush();
	};
	await offer();
	check(!!ghost(), 'ghost visible');
	check(
		!editor().textContent && !f.draft.includes('Keep this literal'),
		'ghost absent from editor and draft'
	);
	check((await key('Tab')).defaultPrevented, 'Tab consumed');
	check(editor().textContent === f.literal && f.sends === 0, 'Tab accepts without sending');
	check(
		!editor().querySelector('img,strong') && !window.unsafeSuggestion,
		'literal HTML and markdown stay text'
	);
	await offer();
	await key('Enter');
	check(editor().textContent === f.literal && f.sends === 0, 'Enter accepts without sending');
	await offer();
	for (const extra of [
		{ shiftKey: true },
		{ ctrlKey: true },
		{ altKey: true },
		{ metaKey: true },
		{ isComposing: true },
		{ repeat: true },
		{ keyCode: 229 }
	]) {
		await offer();
		await key('Enter', extra);
		check(
			editor().textContent !== f.literal && f.sends === 0,
			`Enter guard ${Object.keys(extra)[0]}`
		);
	}
	await offer();
	await key('Escape');
	check(!ghost(), 'Escape dismisses');
	f.receive();
	await flush();
	check(!ghost(), 'dismissal rejects late duplicate');
	await offer();
	editor().focus();
	document.execCommand('insertText', false, 'mine');
	await flush();
	check(!ghost() && editor().textContent === 'mine', 'typing dismisses');
	await clear();
	f.receive();
	await flush();
	check(!ghost(), 'typing then erasing rejects late reply');
	await clear();
	const stale = await f.start();
	editor().focus();
	document.execCommand('insertText', false, 'x');
	await clear();
	await f.settle();
	f.receive(stale);
	await flush();
	check(!ghost(), 'input during generation rejects late reply');
	await offer();
	const oldSession = f.session;
	f.state({ sessionId: 'other', liveTurnId: null });
	await flush();
	f.state({ sessionId: oldSession });
	await flush();
	f.receive();
	await flush();
	check(!ghost(), 'switch away and back never restores');
	await offer();
	const oldTurn = await f.start();
	await f.start();
	await f.settle();
	f.receive(oldTurn);
	await flush();
	check(!ghost(), 'new turn rejects old suggestion');
	f.receive();
	await flush();
	check(!!ghost(), 'current turn receives suggestion after settlement');
	const toggle = document.querySelector('[aria-label="Suggest next reply"]');
	toggle.click();
	await flush();
	check(
		toggle.getAttribute('aria-checked') === 'false' && !ghost(),
		'setting disable clears preview'
	);
	toggle.click();
	await flush();
	f.receive();
	await flush();
	check(!ghost(), 're-enable does not resurrect');
	await offer();
	document.querySelector('[aria-label="Use suggested reply"]').click();
	await flush();
	check(
		editor().textContent === f.literal && f.sends === 0,
		'touch affordance accepts without sending'
	);
	check(document.documentElement.scrollWidth <= innerWidth, 'no horizontal overflow');
	await offer();
	return { viewport: [innerWidth, innerHeight], checks: results.length, results, sends: f.sends };
}
