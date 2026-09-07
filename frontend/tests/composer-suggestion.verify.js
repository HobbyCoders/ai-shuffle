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
	check(placeholder() === f.literal, 'suggestion uses existing placeholder');
	check(getComputedStyle(editor().querySelector('p'), '::before').color === originalColor,
		'suggestion matches placeholder color');
	const useReply = () => [...document.querySelectorAll('button')].find(button => button.textContent.trim() === 'Use reply');
	const dismiss = () => [...document.querySelectorAll('button')].find(button => button.textContent.trim() === 'Dismiss');
	check(!!useReply() && !!dismiss(), 'touch-accessible suggestion actions');
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
	await key('Enter', { keyCode: 229 });
	check(editor().textContent !== f.literal, 'IME Enter does not accept or send');
	// A synthetic IME Enter can become ordinary editor input in Chromium and
	// invalidate the hint. The touch path must work without keyboard events.
	await offer();
	useReply().click();
	await flush();
	check(editor().textContent === f.literal && f.sends === 0, 'button accepts without keyboard or sending');
	await offer();
	dismiss().click();
	await flush();
	check(!ghost() && !editor().textContent, 'button dismisses without changing draft');
	await offer();
	await key('Escape');
	check(!ghost(), 'Escape dismisses');
	check(placeholder() === originalPlaceholder, 'Escape restores original placeholder');
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
	await clear();
	const longReply = ('A long suggested reply that stays on the placeholder line. '.repeat(40) + '\nSecond line');
	await f.offer(longReply);
	await flush();
	check(placeholder() === longReply && !editor().textContent && !f.draft.includes(longReply), 'long multiline suggestion stays outside draft');
	check(heights().every((height, index) => height === suggestionHeights[index]), 'long suggestion does not increase preview height');
	const style = getComputedStyle(editor().querySelector('p'), '::before');
	check(style.whiteSpace === 'nowrap' && style.overflowX === 'hidden' && style.textOverflow === 'ellipsis', 'long suggestion clipped to one placeholder line');
	await key('Tab');
	check([...editor().querySelectorAll('p')].map(p => p.textContent).join('\n') === longReply && f.sends === 0, 'long suggestion accepts in full without sending');
	check(document.documentElement.scrollWidth <= innerWidth, 'no horizontal overflow');
	await offer();
	return { viewport: [innerWidth, innerHeight], checks: results.length, results, sends: f.sends };
}
