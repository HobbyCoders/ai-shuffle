/*
 * The call pop-out page. It draws the state the main window sends through
 * `window.shuffleCall` (src/preload/call-popout.ts) and sends button presses
 * back; the call itself, and its audio, stay in the main window.
 */
(function (root) {
	'use strict';

	/** Draws `state` into the page. Unknown fields are ignored; text goes in as text. */
	function render(doc, state) {
		if (!state || typeof state !== 'object') return;
		const label = doc.getElementById('label');
		const title = doc.getElementById('title');
		const mic = doc.getElementById('mic');
		const agent = doc.getElementById('agent');
		label.textContent = String(state.label || 'Voice call');
		label.classList.toggle('mic-problem', state.micProblem === true);
		title.textContent = String(state.title || 'Voice session');
		mic.setAttribute('aria-pressed', String(state.micMuted === true));
		mic.setAttribute('aria-label', state.micMuted === true ? 'Unmute microphone' : 'Mute microphone');
		agent.setAttribute('aria-pressed', String(state.agentMuted === true));
		agent.setAttribute('aria-label', state.agentMuted === true ? 'Unmute speaker' : 'Mute speaker');
		doc.body.classList.add('ready');
	}

	/** Wires the buttons to `bridge.command` and the bridge's state to `render`. */
	function start(doc, bridge) {
		const buttons = { return: 'return', mic: 'toggle-mic', agent: 'toggle-agent', dock: 'dock', hangup: 'hangup' };
		for (const [id, command] of Object.entries(buttons)) {
			doc.getElementById(id).addEventListener('click', () => bridge.command(command));
		}
		// Escape puts the bar back in the app, as it does for the in-app bar.
		doc.addEventListener('keydown', (event) => {
			if (event.key === 'Escape') bridge.command('dock');
		});
		return bridge.onState((state) => render(doc, state));
	}

	if (typeof module === 'object' && module.exports) module.exports = { render, start };
	else if (root.document && root.shuffleCall) {
		const run = () => start(root.document, root.shuffleCall);
		if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', run);
		else run();
	}
})(typeof window === 'undefined' ? globalThis : window);
