/* Setup and status pages for the local worker. Talks to the shell only through the preload bridges. */
(() => {
	const desktop = window.shuffleDesktop;
	const worker = window.shuffleDesktopWorker;
	if (!desktop || !worker) return;

	const $ = (id) => document.getElementById(id);
	const params = new URLSearchParams(window.location.search);
	const mode = params.get('mode') || 'status';

	$('version').textContent = `AI Shuffle Desktop ${desktop.version}`;
	$('link-pg').addEventListener('click', (event) => {
		event.preventDefault();
		desktop.shell.openExternal('https://www.postgresql.org/download/');
	});

	const setup = {
		section: $('setup'),
		form: $('pg-form'),
		host: $('pg-host'),
		port: $('pg-port'),
		user: $('pg-user'),
		password: $('pg-password'),
		database: $('pg-database'),
		error: $('setup-error'),
		ok: $('setup-ok'),
		test: $('pg-test'),
		create: $('pg-create'),
		cancel: $('pg-cancel'),
		save: $('pg-save'),
		note: $('password-note'),
		bundledNote: $('bundled-note'),
		bundledSwitch: $('bundled-switch'),
		bundledMissing: $('bundled-missing'),
		useBundled: $('pg-bundled')
	};
	const status = {
		section: $('status'),
		dot: $('status-dot'),
		heading: $('status-heading'),
		detail: $('status-detail'),
		postgres: $('status-postgres'),
		error: $('status-error'),
		log: $('log'),
		restart: $('restart'),
		dbSettings: $('db-settings'),
		openLogs: $('open-logs')
	};

	let busy = false;

	function readForm() {
		return {
			host: setup.host.value,
			port: Number(setup.port.value),
			user: setup.user.value,
			password: setup.password.value,
			database: setup.database.value
		};
	}

	function showSetupMessage(kind, text) {
		setup.error.hidden = kind !== 'error' || !text;
		setup.ok.hidden = kind !== 'ok' || !text;
		if (kind === 'error') setup.error.textContent = text;
		if (kind === 'ok') setup.ok.textContent = text;
	}

	function setSetupBusy(value, label) {
		busy = value;
		for (const b of [setup.test, setup.create, setup.save]) b.disabled = value;
		setup.save.textContent = value && label ? label : 'Save and start';
	}

	function describeFailure(result) {
		switch (result.code) {
			case 'no-database':
				setup.create.hidden = false;
				return `The user connected, but the database "${setup.database.value}" does not exist. Create it below, or enter the name of an existing one.`;
			case 'auth':
				return 'PostgreSQL rejected the user name or password.';
			case 'no-role':
				return `PostgreSQL has no user named "${setup.user.value}". Create it first (see the help below).`;
			case 'unreachable':
				return `Nothing answered at ${setup.host.value}:${setup.port.value}. Check that PostgreSQL is running and accepting TCP connections.`;
			case 'hba':
				return 'PostgreSQL refused the connection because of its pg_hba.conf rules. Allow this user and address, then try again.';
			default:
				return result.error || 'The connection failed.';
		}
	}

	async function runCheck(action) {
		setup.create.hidden = true;
		showSetupMessage('error', '');
		showSetupMessage('ok', '');
		setSetupBusy(true);
		try {
			const result = await worker.checkDatabase(readForm(), action);
			if (result.ok) {
				const tables = result.tables > 0 ? `${result.tables} tables present` : 'empty database, tables are created on first start';
				showSetupMessage('ok', `Connected as ${result.user} to ${result.version}. ${tables}.`);
				return true;
			}
			showSetupMessage('error', describeFailure(result));
			return false;
		} catch (error) {
			showSetupMessage('error', stripIpcPrefix(error && error.message ? error.message : 'The connection check failed.'));
			return false;
		} finally {
			setSetupBusy(false);
		}
	}

	async function initSetup() {
		const current = await worker.getSettings();
		setup.host.value = current.postgres.host;
		setup.port.value = String(current.postgres.port);
		setup.user.value = current.postgres.user;
		setup.database.value = current.postgres.database;
		setup.note.hidden = current.keyringAvailable;
		// The bundled server is the default; the way back to it shows once an external one is in use.
		setup.bundledNote.hidden = !current.managed || !current.bundledAvailable;
		setup.bundledSwitch.hidden = current.managed || !current.bundledAvailable;
		setup.bundledMissing.hidden = current.bundledAvailable;
		if (!current.bundledAvailable) setup.bundledMissing.textContent = current.bundledError || 'The bundled PostgreSQL is not available.';
		if (params.get('error')) showSetupMessage('error', params.get('error'));
		setup.section.hidden = false;
		(current.configured && !current.managed ? setup.password : setup.host).focus();

		setup.test.addEventListener('click', () => void runCheck('test'));
		setup.create.addEventListener('click', () => void runCheck('create'));
		setup.cancel.addEventListener('click', () => worker.cancelSetup());
		setup.useBundled.addEventListener('click', async () => {
			if (busy) return;
			setSetupBusy(true, 'Switching…');
			setup.useBundled.disabled = true;
			try {
				await worker.useBundled();
			} catch (error) {
				showSetupMessage('error', stripIpcPrefix(error && error.message ? error.message : 'Could not switch to the bundled PostgreSQL.'));
				setSetupBusy(false);
				setup.useBundled.disabled = false;
			}
		});
		setup.form.addEventListener('submit', async (event) => {
			event.preventDefault();
			if (busy) return;
			if (!(await runCheck('test'))) return;
			setSetupBusy(true, 'Starting…');
			try {
				await worker.saveAndStart(readForm());
			} catch (error) {
				showSetupMessage('error', stripIpcPrefix(error && error.message ? error.message : 'Could not start the worker.'));
				setSetupBusy(false);
			}
		});
	}

	function describePostgres(pg) {
		if (!pg) return '';
		if (!pg.managed) return `External PostgreSQL at ${pg.host}:${pg.port}.`;
		switch (pg.phase) {
			case 'starting':
				return 'Bundled PostgreSQL: starting…';
			case 'ready':
				return `Bundled PostgreSQL: running on ${pg.host}:${pg.port}.`;
			case 'stopped':
				return 'Bundled PostgreSQL: stopped.';
			case 'failed':
				return 'Bundled PostgreSQL: could not start.';
			default:
				return 'Bundled PostgreSQL.';
		}
	}

	function renderState(state) {
		status.dot.className = 'dot';
		status.restart.hidden = true;
		status.error.hidden = true;
		const pg = state.postgres;
		status.postgres.textContent = describePostgres(pg);
		// The database comes first: while it starts or when it fails, the worker has not been asked yet.
		if (pg && pg.managed && (state.phase === 'idle' || state.phase === 'stopped') && (pg.phase === 'starting' || pg.phase === 'failed')) {
			if (pg.phase === 'starting') {
				status.dot.classList.add('dot-checking');
				status.heading.textContent = 'Starting PostgreSQL…';
				status.detail.textContent = 'The first start creates the database cluster and can take a moment.';
			} else {
				status.dot.classList.add('dot-bad');
				status.heading.textContent = 'The bundled PostgreSQL could not start';
				status.detail.textContent = 'Check the message and the log, then restart. Worker → Advanced switches to an external server.';
				status.error.textContent = pg.error || '';
				status.error.hidden = !pg.error;
				status.restart.hidden = false;
			}
			return;
		}
		switch (state.phase) {
			case 'starting':
				status.dot.classList.add('dot-checking');
				status.heading.textContent = 'Starting the worker…';
				status.detail.textContent = state.pid ? `Process ${state.pid} is starting. The first start creates the database tables and can take a minute.` : 'Checking the port and the database.';
				break;
			case 'stopping':
				status.dot.classList.add('dot-warn');
				status.heading.textContent = 'Stopping the worker…';
				status.detail.textContent = '';
				break;
			case 'ready':
				status.dot.classList.add('dot-ok');
				status.heading.textContent = 'Worker ready';
				status.detail.textContent = state.url;
				break;
			case 'stopped':
				status.dot.classList.add('dot-warn');
				status.heading.textContent = 'Worker stopped';
				status.detail.textContent = 'The worker is not running.';
				status.restart.hidden = false;
				break;
			case 'failed':
				status.dot.classList.add('dot-bad');
				status.heading.textContent = 'The worker could not start';
				status.detail.textContent = 'Check the message and the log, then restart it.';
				status.error.textContent = state.error || '';
				status.error.hidden = !state.error;
				status.restart.hidden = false;
				break;
			default:
				status.heading.textContent = 'Worker';
				status.detail.textContent = '';
		}
	}

	function renderLog(lines) {
		const atBottom = status.log.scrollTop + status.log.clientHeight >= status.log.scrollHeight - 8;
		status.log.textContent = lines.join('\n');
		if (atBottom) status.log.scrollTop = status.log.scrollHeight;
	}

	async function initStatus() {
		status.section.hidden = false;
		renderState(await worker.getState());
		const loadError = params.get('error');
		if (loadError) {
			status.error.textContent = loadError;
			status.error.hidden = false;
			status.restart.hidden = false;
		}
		renderLog(await worker.tail());
		worker.onState(renderState);
		worker.onLog(() => void worker.tail().then(renderLog));
		status.restart.addEventListener('click', () => {
			status.restart.hidden = true;
			void worker.restart();
		});
		status.dbSettings.addEventListener('click', () => worker.openSetup());
		status.openLogs.addEventListener('click', () => worker.openLogs());
	}

	function stripIpcPrefix(message) {
		return String(message).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');
	}

	if (mode === 'setup') void initSetup();
	else void initStatus();
})();
