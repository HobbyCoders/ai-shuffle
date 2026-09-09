/* Status page for the local worker and its bundled database. Uses only the preload bridges. */
(() => {
	const desktop = window.shuffleDesktop;
	const worker = window.shuffleDesktopWorker;
	if (!desktop || !worker) return;

	const $ = (id) => document.getElementById(id);
	const params = new URLSearchParams(window.location.search);

	$('version').textContent = `AI Shuffle Desktop ${desktop.version}`;
	const status = {
		section: $('status'),
		dot: $('status-dot'),
		heading: $('status-heading'),
		detail: $('status-detail'),
		postgres: $('status-postgres'),
		error: $('status-error'),
		log: $('log'),
		restart: $('restart'),
		openLogs: $('open-logs')
	};

	function describePostgres(pg) {
		if (!pg) return '';
		switch (pg.phase) {
			case 'starting':
				return 'Bundled PostgreSQL: starting…';
			case 'ready':
				return 'Bundled PostgreSQL: running on this computer.';
			case 'stopping':
				return 'Bundled PostgreSQL: stopping…';
			case 'stopped':
				return 'Bundled PostgreSQL: stopped.';
			case 'failed':
				return 'Bundled PostgreSQL: could not start.';
			default:
				return 'AI Shuffle starts its bundled PostgreSQL automatically.';
		}
	}

	function renderState(state) {
		status.dot.className = 'dot';
		status.restart.hidden = true;
		status.error.hidden = true;
		const pg = state.postgres;
		status.postgres.textContent = describePostgres(pg);
		// The database comes first: while it starts or when it fails, the worker has not been asked yet.
		if (pg && (pg.phase === 'starting' || pg.phase === 'failed')) {
			if (pg.phase === 'starting') {
				status.dot.classList.add('dot-checking');
				status.heading.textContent = 'Starting PostgreSQL…';
				status.detail.textContent = 'The first start creates the database cluster and can take a moment.';
			} else {
				status.dot.classList.add('dot-bad');
				status.heading.textContent = 'The bundled PostgreSQL could not start';
				status.detail.textContent = 'Check the message and startup log, then restart the worker. The PostgreSQL log is also available under Worker → Show PostgreSQL Log.';
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
		status.openLogs.addEventListener('click', () => worker.openLogs());
	}

	void initStatus();
})();
