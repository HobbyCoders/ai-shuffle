/* Connect screen logic. Talks to the shell only through the two bridges the preload exposes. */
(() => {
	const desktop = window.shuffleDesktop;
	const servers = window.shuffleDesktopServers;
	if (!desktop || !servers) return;

	const $ = (id) => document.getElementById(id);
	const errorBox = $('error');
	const list = $('servers');
	const empty = $('empty');
	const local = $('local');
	const localVersion = $('local-version');
	const localOpen = $('local-open');
	const form = $('add');
	const urlInput = $('url');
	const nameInput = $('name');
	const submit = $('submit');
	const rowTemplate = $('row');

	$('version').textContent = `AI Shuffle Desktop ${desktop.version}`;

	const params = new URLSearchParams(window.location.search);
	const initialError = params.get('error');
	if (initialError) showError(initialError);

	let localOrigin = null;
	let busy = false;

	function showError(message) {
		errorBox.textContent = message;
		errorBox.hidden = !message;
	}

	function setBusy(value) {
		busy = value;
		submit.disabled = value;
		localOpen.disabled = value;
		for (const button of list.querySelectorAll('button')) button.disabled = value;
	}

	function describe(result) {
		if (!result) return 'Checking…';
		if (result.ok) return result.setupRequired ? 'Needs first-run setup' : result.version ? `v${result.version}` : 'Online';
		return result.error || 'Unreachable';
	}

	async function render() {
		const saved = await servers.list();
		list.replaceChildren();
		empty.hidden = saved.length > 0;
		for (const server of saved) {
			const node = rowTemplate.content.firstElementChild.cloneNode(true);
			const dot = node.querySelector('.dot');
			const name = node.querySelector('.server-name');
			const meta = node.querySelector('.server-meta');
			name.textContent = server.name;
			meta.textContent = `${server.url} · Checking…`;
			dot.classList.add('dot-checking');

			node.querySelector('.server-open').addEventListener('click', () => open(server.id));
			node.querySelector('.server-remove').addEventListener('click', async () => {
				if (busy) return;
				await servers.remove(server.id);
				await render();
			});
			node.querySelector('.server-rename').addEventListener('click', async () => {
				if (busy) return;
				const next = window.prompt('Name this worker', server.name);
				if (next && next.trim() && next.trim() !== server.name) {
					await servers.rename(server.id, next.trim());
					await render();
				}
			});
			list.appendChild(node);

			servers
				.probe(server.url)
				.then((result) => {
					dot.classList.remove('dot-checking');
					dot.classList.add(result.ok ? (result.setupRequired ? 'dot-warn' : 'dot-ok') : 'dot-bad');
					meta.textContent = `${server.url} · ${describe(result)}`;
				})
				.catch(() => {
					dot.classList.remove('dot-checking');
					dot.classList.add('dot-bad');
					meta.textContent = `${server.url} · Unreachable`;
				});
		}
		await detectLocal(saved);
	}

	async function detectLocal(saved) {
		const found = await servers.detectLocal().catch(() => null);
		if (!found) {
			local.hidden = true;
			localOrigin = null;
			return;
		}
		localOrigin = found.origin;
		const alreadySaved = saved.some((s) => s.url === found.origin);
		local.hidden = alreadySaved;
		localVersion.textContent = found.result.version ? ` (v${found.result.version})` : '';
	}

	async function open(id, pairingCode) {
		if (busy) return;
		setBusy(true);
		showError('');
		try {
			await servers.connect(id, pairingCode || null);
		} catch (error) {
			showError(error && error.message ? stripIpcPrefix(error.message) : 'Could not open that worker.');
			setBusy(false);
		}
	}

	localOpen.addEventListener('click', async () => {
		if (!localOrigin || busy) return;
		setBusy(true);
		try {
			const { server } = await servers.add({ url: localOrigin, name: 'This computer' });
			await open(server.id);
		} catch (error) {
			showError(stripIpcPrefix(error.message));
			setBusy(false);
		}
	});

	form.addEventListener('submit', async (event) => {
		event.preventDefault();
		if (busy) return;
		setBusy(true);
		showError('');
		try {
			const { server, pairingCode } = await servers.add({ url: urlInput.value, name: nameInput.value });
			const check = await servers.probe(server.url);
			if (!check.ok) {
				await servers.remove(server.id);
				throw new Error(`Could not reach ${server.url}: ${check.error || 'no answer'}`);
			}
			urlInput.value = '';
			nameInput.value = '';
			await open(server.id, pairingCode);
		} catch (error) {
			showError(stripIpcPrefix(error && error.message ? error.message : 'Could not add that worker.'));
			setBusy(false);
		}
	});

	function stripIpcPrefix(message) {
		// Errors thrown in the main process arrive as "Error invoking remote method 'x': Error: message".
		return String(message).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
	}

	urlInput.focus();
	void render();
})();
