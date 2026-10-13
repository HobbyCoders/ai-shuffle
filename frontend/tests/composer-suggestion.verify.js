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
	// Hints arrive through an authoritative read (fetch), not the WS frame.
	const flush = async () => {
		for (let i = 0; i < 3; i++) await new Promise(resolve => setTimeout(resolve, 10));
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
	await clear();
	const placeholder = () => editor().querySelector('p').getAttribute('data-placeholder');
	const originalPlaceholder = placeholder();
	const heights = () => ['.chat-input-container', '.tiptap-editor', '.ProseMirror'].map(
		selector => document.querySelector(selector).getBoundingClientRect().height
	);
	const placeholderStyle = getComputedStyle(editor().querySelector('p'), '::before');
	const originalColor = placeholderStyle.color;
	await offer();
	check(!!ghost(), 'ghost visible');
	const inlineHint = () => document.querySelector('.inline-suggestion');
	check(
		inlineHint()?.textContent.trim() === f.literal && !placeholder(),
		'suggestion shown inline in place of the placeholder'
	);
	check(getComputedStyle(inlineHint()).color === originalColor, 'suggestion matches placeholder color');
	// #1402: inline-only presentation; no separate Use reply / Dismiss row.
	const buttons = () => [...document.querySelectorAll('button')].map(
		button => button.getAttribute('aria-label') || button.textContent.trim()
	);
	check(
		!buttons().some(name => /use reply|dismiss|suggestion/i.test(name)) &&
			!document.querySelector('[aria-label*="suggestion actions" i]'),
		'no separate suggestion action row'
	);
	const buttonsWithSuggestion = buttons();
	const suggestionHeights = heights();
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
	check(editor().textContent !== f.literal && f.sends === 0, 'Enter neither accepts nor sends');
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
	await key('Enter', { keyCode: 229 });
	check(editor().textContent !== f.literal, 'IME Enter does not accept or send');
	// A synthetic IME Enter can become ordinary editor input in Chromium and
	// invalidate the hint. The touch path must work without keyboard events.
	await offer();
	editor().dispatchEvent(new MouseEvent('dblclick', { bubbles: true, cancelable: true }));
	await flush();
	check(editor().textContent === f.literal && f.sends === 0, 'double click accepts without keyboard or sending');
	await offer();
	await key('Escape');
	check(!ghost(), 'Escape dismisses');
	check(placeholder() === originalPlaceholder, 'Escape restores original placeholder');
	check(
		JSON.stringify(buttons()) === JSON.stringify(buttonsWithSuggestion),
		'suggestion adds no controls'
	);
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
	const reads = () => f.requests.filter(url => url.includes('/composer-suggestion')).length;
	// The client has no generation path; any non-read suggestion request is one.
	const generations = () =>
		f.requests.filter(url => !url.startsWith('GET ') && /suggest/i.test(url)).length;
	const before = generations();
	f.state({ sessionId: 'other', liveTurnId: null });
	await flush();
	check(!ghost(), 'switch away hides hint');
	const readsBefore = reads();
	f.state({ sessionId: oldSession });
	await flush();
	check(!!ghost() && reads() > readsBefore && generations() === before,
		'switch back restores retained hint by read, without generation');
	await key('Escape');
	f.state({ sessionId: 'other', liveTurnId: null });
	await flush();
	f.state({ sessionId: oldSession });
	await flush();
	check(!ghost(), 'switch away and back never restores a dismissed hint');
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
	const generationsBeforeToggle = generations();
	toggle.click();
	await flush();
	check(
		!!ghost() && generations() === generationsBeforeToggle,
		're-enable restores retained hint without generation'
	);
	await key('Escape');
	toggle.click();
	await flush();
	toggle.click();
	await flush();
	check(!ghost(), 're-enable does not resurrect a dismissed hint');
	await clear();
	const longReply = ('A long suggested reply that stays on the placeholder line. '.repeat(40) + '\nSecond line');
	await f.offer(longReply);
	await flush();
	check(inlineHint()?.textContent.trim() === longReply.trim() && !editor().textContent && !f.draft.includes(longReply), 'long multiline suggestion stays outside draft');
	check(heights().every((height, index) => height === suggestionHeights[index]), 'long suggestion does not increase preview height');
	const style = getComputedStyle(inlineHint());
	check(style.whiteSpace === 'nowrap' && style.overflowX === 'hidden' && style.textOverflow === 'ellipsis', 'long suggestion clipped to one placeholder line');
	await key('Tab');
	check([...editor().querySelectorAll('p')].map(p => p.textContent).join('\n') === longReply && f.sends === 0, 'long suggestion accepts in full without sending');
	check(document.documentElement.scrollWidth <= innerWidth, 'no horizontal overflow');
	await offer();
	return { viewport: [innerWidth, innerHeight], checks: results.length, results, sends: f.sends };
}
