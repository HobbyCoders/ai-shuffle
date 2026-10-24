<script lang="ts">
	/**
	 * PhonePanel — the worker's Android emulator in the session side panel.
	 *
	 * Status first: the panel explains a host without KVM, offers the one-time
	 * SDK setup (license required), shows download progress, and only then the
	 * device. The device view draws the live screen (PhoneScreen), streams over
	 * a reconnecting socket, and carries Android's three nav buttons plus
	 * rotate, volume, power and APK install.
	 *
	 * The emulator belongs to the worker, not the chat: every chat's panel shows
	 * the same phone.
	 */
	import { onDestroy, onMount, untrack, type Snippet } from 'svelte';
	import {
		Smartphone,
		LoaderCircle,
		PlugZap,
		Cpu,
		ShieldAlert,
		Download,
		TriangleAlert,
		RotateCwSquare,
		PackagePlus,
		Keyboard,
		Power,
		Play,
		Square,
		Circle,
		Triangle,
		Volume1,
		Volume2,
		Copy,
		Check,
		X,
		ExternalLink,
		MousePointer2,
		HardDrive,
	} from 'lucide-svelte';
	import PhoneScreen from './PhoneScreen.svelte';
	import {
		fetchEmulatorStatus,
		setupEmulator,
		startDevice,
		stopDevice,
		installApk,
		errorText,
	} from '$lib/api/emulator';
	import {
		panelView,
		statusPollMs,
		installPercent,
		keyMessage,
		STALE_AFTER_MS,
		NOTICE_MS,
		type AndroidKey,
		type ClientMessage,
		type DeviceState,
		type EmulatorStatus,
		type FrameMessage,
	} from '$lib/emulator/protocol';
	import { createEmulatorStream, type EmulatorStreamClient, type LinkState } from '$lib/emulator/stream-client';
	import { copyText } from '$lib/utils/clipboard';
	import { phonePanel } from '$lib/stores/phone-panel.svelte';

	interface Props {
		panelActions?: Snippet;
		mobile?: boolean;
		/** Tests inject a fake socket. */
		WebSocketImpl?: typeof WebSocket;
	}
	let { panelActions, mobile = false, WebSocketImpl }: Props = $props();

	// ── Status ─────────────────────────────────────────────────────────────
	let root = $state<HTMLElement | undefined>();
	// "Show phone" unmounts the button that was focused; take focus here so
	// keyboard and screen-reader users land in the panel (Esc then closes it).
	// Opened by the agent's own call: leave focus where the user is typing.
	onMount(() => { if (phonePanel.takeFocusRequest()) root?.focus(); });
	let status = $state<EmulatorStatus | null>(null);
	let loadError = $state<string | null>(null);
	let refreshing = $state(false);
	let destroyed = false;
	let statusSeq = 0;
	let pollTimer: ReturnType<typeof setTimeout> | undefined;

	async function refresh() {
		const seq = ++statusSeq;
		refreshing = true;
		try {
			const next = await fetchEmulatorStatus();
			if (destroyed || seq !== statusSeq) return;
			status = next;
			loadError = null;
			liveState = null;
		} catch (error) {
			if (destroyed || seq !== statusSeq) return;
			loadError = errorText(error, 'The worker did not answer.');
		} finally {
			if (seq === statusSeq) refreshing = false;
		}
	}
	void refresh();
	onDestroy(() => {
		destroyed = true;
		clearTimeout(pollTimer);
	});

	const view = $derived(panelView(status, loadError));
	const device = $derived(view.kind === 'device' ? view.device : null);
	/** The socket's word beats the last status poll until the next poll. */
	let liveState = $state<DeviceState | null>(null);
	let pendingState = $state<DeviceState | null>(null);
	const deviceState = $derived<DeviceState | null>(device ? (pendingState ?? liveState ?? device.state) : null);

	// Poll while the worker changes on its own (download, boot).
	$effect(() => {
		const ms = statusPollMs(status);
		const booting = deviceState === 'booting';
		const delay = ms ?? (booting ? 2000 : null);
		void status;
		clearTimeout(pollTimer);
		if (delay === null) return;
		pollTimer = setTimeout(() => void refresh(), delay);
		return () => clearTimeout(pollTimer);
	});

	// ── Setup ──────────────────────────────────────────────────────────────
	let licenseAccepted = $state(false);
	let settingUp = $state(false);
	let actionError = $state('');

	async function beginSetup() {
		if (!licenseAccepted || settingUp) return;
		settingUp = true;
		actionError = '';
		try {
			await setupEmulator();
			await refresh();
		} catch (error) {
			actionError = errorText(error, 'Setup did not start. Try again.');
		} finally {
			settingUp = false;
		}
	}

	async function retry() {
		actionError = '';
		// A failed download is retried by asking for setup again (the license
		// was already accepted to get here); anything else re-reads the status.
		if (status?.install?.state === 'error') {
			settingUp = true;
			try { await setupEmulator(); } catch (error) { actionError = errorText(error, 'Setup did not start. Try again.'); }
			finally { settingUp = false; }
		}
		await refresh();
	}

	// ── Start / stop ───────────────────────────────────────────────────────
	let powerBusy = $state(false);
	async function start() {
		if (!device || powerBusy) return;
		powerBusy = true;
		actionError = '';
		pendingState = 'booting';
		try {
			await startDevice(device.id);
			await refresh();
		} catch (error) {
			actionError = errorText(error, 'The phone did not start.');
		} finally {
			pendingState = null;
			powerBusy = false;
		}
	}
	async function stop() {
		if (!device || powerBusy) return;
		powerBusy = true;
		actionError = '';
		try {
			await stopDevice(device.id);
			frame = null;
			liveState = 'stopped';
			await refresh();
		} catch (error) {
			actionError = errorText(error, 'The phone did not stop.');
		} finally {
			powerBusy = false;
		}
	}

	// ── Stream ─────────────────────────────────────────────────────────────
	let frame = $state.raw<FrameMessage | null>(null);
	let link = $state<LinkState | 'idle'>('idle');
	/** Why the stream stopped for good (unknown device, bad input, not allowed). */
	let linkFailure = $state<string | null>(null);
	/** The device's own words for its state (error reason, progress). */
	let stateMessage = $state('');
	/** An input the phone could not apply: shown briefly, the stream carries on. */
	let notice = $state<string | null>(null);
	let noticeTimer: ReturnType<typeof setTimeout> | undefined;
	function showNotice(text: string) {
		notice = text;
		clearTimeout(noticeTimer);
		noticeTimer = setTimeout(() => (notice = null), NOTICE_MS);
	}
	onDestroy(() => clearTimeout(noticeTimer));
	let client: EmulatorStreamClient | null = null;
	const wantStream = $derived(!!device && (deviceState === 'booting' || deviceState === 'running'));
	const streamDeviceId = $derived(wantStream ? device!.id : null);

	function getAuthToken(): string | null {
		if (typeof document === 'undefined') return null;
		for (const cookie of document.cookie.split(';')) {
			const [name, value] = cookie.trim().split('=');
			if (name === 'session') return value ?? null;
		}
		return null;
	}

	$effect(() => {
		const id = streamDeviceId;
		if (!id) return;
		return untrack(() => {
			const c = createEmulatorStream({
				deviceId: id,
				getAuthToken,
				WebSocketImpl,
				onFrame: (f) => { frame = f; },
				onState: (s) => {
					liveState = s.state;
					stateMessage = s.message;
					if (s.state === 'stopped') frame = null;
				},
				onError: (e) => showNotice(e.message),
				onLink: (s, detail) => {
					link = s;
					linkFailure = s === 'failed' ? (detail?.failure ?? 'The connection to the phone was closed.') : null;
				},
			});
			client = c;
			c.connect();
			return () => {
				c.destroy();
				if (client === c) client = null;
				link = 'idle';
				linkFailure = null;
			};
		});
	});

	const live = $derived(link === 'live');
	const interactive = $derived(live && deviceState === 'running');
	// Debounced like the Browser panel's badge: a sub-second blip is not news.
	// A terminal stop is stale at once (and says so in its own card).
	let stale = $state(false);
	$effect(() => {
		if (live || !frame) { stale = false; return; }
		if (linkFailure) { stale = true; return; }
		const t = setTimeout(() => (stale = true), STALE_AFTER_MS);
		return () => clearTimeout(t);
	});

	function send(msg: ClientMessage) {
		if (!client?.send(msg)) showNotice('Not connected to the phone yet.');
	}
	function reconnect() {
		linkFailure = null;
		client?.retry();
		void refresh();
	}
	const press = (key: AndroidKey) => send(keyMessage(key));

	// ── Keyboard (phones) ──────────────────────────────────────────────────
	let screen = $state<ReturnType<typeof PhoneScreen>>();
	let keyboardOpen = $state(false);
	function toggleKeyboard() {
		if (keyboardOpen) screen?.closeKeyboard();
		else screen?.openKeyboard();
	}

	// ── APK install ────────────────────────────────────────────────────────
	let fileInput = $state<HTMLInputElement>();
	let installing = $state<string | null>(null);
	let installed = $state<string | null>(null);
	let installError = $state('');
	async function onApkPicked(e: Event) {
		const input = e.currentTarget as HTMLInputElement;
		const file = input.files?.[0];
		input.value = '';
		if (!file || !device) return;
		installing = file.name;
		installed = null;
		installError = '';
		try {
			const result = await installApk(device.id, file);
			installed = result.package || file.name;
		} catch (error) {
			installError = errorText(error, 'The app did not install.');
		} finally {
			installing = null;
		}
	}

	// ── Copyable commands in the host help ─────────────────────────────────
	let copied = $state<string | null>(null);
	async function copy(text: string) {
		try {
			await copyText(text);
			copied = text;
			setTimeout(() => { if (copied === text) copied = null; }, 1500);
		} catch { /* the text stays selectable */ }
	}

	const stateLabel: Record<DeviceState, string> = {
		stopped: 'Off',
		booting: 'Starting',
		running: 'Running',
		error: 'Error',
	};
	const dotTone = $derived(
		!deviceState ? 'off'
			: linkFailure ? 'bad'
			: deviceState === 'running' ? (live ? 'ok' : 'warn')
			: deviceState === 'booting' ? 'warn'
			: deviceState === 'error' ? 'bad' : 'off',
	);
	const aspect = $derived(device && device.width > 0 && device.height > 0 ? `${device.width} / ${device.height}` : '9 / 19.5');
</script>

{#snippet command(text: string)}
	<div class="cmd">
		<code>{text}</code>
		<button class="cmd-copy" onclick={() => copy(text)} aria-label={`Copy ${text}`} title="Copy">
			{#if copied === text}<Check size={13} />{:else}<Copy size={13} />{/if}
		</button>
	</div>
{/snippet}

<section bind:this={root} class="phone-panel" class:mobile aria-label="Phone" tabindex="-1">
	<header class="bar">
		<div class="title">
			<Smartphone size={15} strokeWidth={1.75} aria-hidden="true" />
			<span>Phone</span>
		</div>
		{#if device}
			<span class="device-chip" title={`${device.name} · Android API ${device.api}`}>
				<span class="dot" data-tone={dotTone} aria-hidden="true"></span>
				<span class="device-name">{device.name}</span>
				<span class="device-meta">API {device.api}</span>
				<span class="sr-only">{deviceState ? stateLabel[deviceState] : ''}</span>
			</span>
		{/if}
		<div class="spacer"></div>
		{#if device}
			<div class="tools">
				{#if deviceState === 'running'}
					<button class="tool-btn keyboard-btn" class:active={keyboardOpen} onclick={toggleKeyboard} disabled={!interactive}
						aria-pressed={keyboardOpen} aria-label={keyboardOpen ? 'Hide keyboard' : 'Show keyboard'} title={keyboardOpen ? 'Hide keyboard' : 'Type on the phone'}>
						<Keyboard size={15} strokeWidth={1.75} />
					</button>
					<button class="tool-btn" onclick={() => send({ type: 'rotate' })} disabled={!interactive} aria-label="Rotate" title="Rotate">
						<RotateCwSquare size={15} strokeWidth={1.75} />
					</button>
					<button class="tool-btn wide-only" onclick={() => press('VOLUME_DOWN')} disabled={!interactive} aria-label="Volume down" title="Volume down">
						<Volume1 size={15} strokeWidth={1.75} />
					</button>
					<button class="tool-btn wide-only" onclick={() => press('VOLUME_UP')} disabled={!interactive} aria-label="Volume up" title="Volume up">
						<Volume2 size={15} strokeWidth={1.75} />
					</button>
					<button class="tool-btn wide-only" onclick={() => press('POWER')} disabled={!interactive} aria-label="Power button" title="Power button (screen on/off)">
						<Power size={15} strokeWidth={1.75} />
					</button>
					<button class="tool-btn" class:busy={!!installing} onclick={() => fileInput?.click()} disabled={!!installing || deviceState !== 'running'}
						aria-label="Install APK" title={installing ? `Installing ${installing}…` : 'Install an APK'}>
						{#if installing}<LoaderCircle class="spin" size={15} strokeWidth={1.75} />{:else}<PackagePlus size={15} strokeWidth={1.75} />{/if}
					</button>
				{/if}
				{#if deviceState === 'running' || deviceState === 'booting'}
					<button class="pill-btn" onclick={stop} disabled={powerBusy} aria-label="Stop phone" title="Shut the phone down">
						{#if powerBusy}<LoaderCircle class="spin" size={13} strokeWidth={2} />{:else}<Square size={11} strokeWidth={2.25} />{/if}
						<span>Stop</span>
					</button>
				{/if}
			</div>
		{/if}
		{@render panelActions?.()}
	</header>
	<input bind:this={fileInput} type="file" accept=".apk,application/vnd.android.package-archive" hidden onchange={onApkPicked} />

	<div class="body">
		{#if actionError}
			<div class="banner bad" role="alert">
				<TriangleAlert size={14} strokeWidth={1.75} />
				<span>{actionError}</span>
				<button onclick={() => (actionError = '')} aria-label="Dismiss"><X size={13} /></button>
			</div>
		{/if}
		{#if notice}
			<div class="banner notice" role="status">
				<TriangleAlert size={14} strokeWidth={1.75} />
				<span>{notice}</span>
				<button onclick={() => (notice = null)} aria-label="Dismiss notice"><X size={13} /></button>
			</div>
		{/if}
		{#if installed || installError}
			<div class="banner" class:bad={!!installError} class:ok={!!installed} role="status">
				{#if installError}<TriangleAlert size={14} strokeWidth={1.75} /><span>{installError}</span>
				{:else}<Check size={14} strokeWidth={2} /><span>Installed <code>{installed}</code></span>{/if}
				<button onclick={() => { installed = null; installError = ''; }} aria-label="Dismiss"><X size={13} /></button>
			</div>
		{/if}

		{#if view.kind === 'loading'}
			<div class="empty-state" role="status">
				<LoaderCircle class="spin" size={26} strokeWidth={1.5} />
				<p class="empty-title">Checking the Android emulator</p>
				<p class="empty-copy">Asking this worker what it can run…</p>
			</div>
		{:else if view.kind === 'unreachable'}
			<div class="empty-state">
				<PlugZap size={26} strokeWidth={1.5} />
				<p class="empty-title">Couldn’t reach the emulator</p>
				<p class="empty-copy">{view.message}</p>
				<button class="pill-btn" onclick={retry} disabled={refreshing}>{refreshing ? 'Checking…' : 'Try again'}</button>
			</div>
		{:else if view.kind === 'host'}
			<div class="card-scroll">
				<article class="card" aria-labelledby="phone-host-title">
					<div class="card-icon warn">{#if view.reason === 'unsupported_arch'}<Cpu size={20} strokeWidth={1.6} />{:else}<ShieldAlert size={20} strokeWidth={1.6} />{/if}</div>
					{#if view.reason === 'no_kvm'}
						<h2 id="phone-host-title">This worker can’t run a virtual phone yet</h2>
						<p>The Android emulator needs hardware virtualization (KVM), and this worker doesn’t have it.</p>
						<h3>Running in Docker</h3>
						<p>Start the container with access to KVM:</p>
						{@render command('--device /dev/kvm')}
						<p class="fine">With Docker Compose, add <code>/dev/kvm</code> under <code>devices:</code>. The host itself must have KVM.</p>
						<h3>On a Linux desktop</h3>
						<p>Turn on virtualization (Intel VT-x or AMD-V) in the BIOS/UEFI settings, then check that <code>/dev/kvm</code> exists:</p>
						{@render command('ls -l /dev/kvm')}
					{:else if view.reason === 'kvm_permission'}
						<h2 id="phone-host-title">KVM is there, but the worker can’t use it</h2>
						<p>The worker is not allowed to open <code>/dev/kvm</code>, so the emulator can’t start.</p>
						<h3>Running in Docker</h3>
						<p>Pass the device in, and make sure the container’s user is in the group that owns it (usually <code>kvm</code>):</p>
						{@render command('--device /dev/kvm --group-add kvm')}
						<h3>On a Linux desktop</h3>
						<p>Add your user to the <code>kvm</code> group, then sign out and back in:</p>
						{@render command('sudo usermod -aG kvm $USER')}
					{:else}
						<h2 id="phone-host-title">This processor isn’t supported</h2>
						<p>The Android emulator here needs a 64-bit Intel or AMD (x86-64) machine. Run AI Shuffle on one to use the Phone panel.</p>
					{/if}
					{#if view.message}<p class="fine detail">{view.message}</p>{/if}
					<button class="pill-btn" onclick={retry} disabled={refreshing}>{refreshing ? 'Checking…' : 'Check again'}</button>
				</article>
			</div>
		{:else if view.kind === 'setup'}
			<div class="card-scroll">
				<article class="card" aria-labelledby="phone-setup-title">
					<div class="card-icon"><Smartphone size={20} strokeWidth={1.6} /></div>
					<h2 id="phone-setup-title">Set up a virtual Android phone</h2>
					<p>Run and test Android apps here: tap, swipe and type on a live phone screen, and install your own APKs.</p>
					<ul class="facts">
						<li><Download size={13} strokeWidth={1.75} aria-hidden="true" /><span>Downloads about <strong>2.1 GB</strong> (the Android SDK and a system image) to this worker, once.</span></li>
						<li><HardDrive size={13} strokeWidth={1.75} aria-hidden="true" /><span>Takes about <strong>6.5 GB</strong> on disk once unpacked; have about <strong>8.6 GB</strong> free before you start.</span></li>
					</ul>
					<label class="consent">
						<input type="checkbox" bind:checked={licenseAccepted} />
						<span>I accept the
							{#if view.licenseUrl}<a href={view.licenseUrl} target="_blank" rel="noopener noreferrer">Android SDK License Agreement<ExternalLink size={11} strokeWidth={1.75} aria-hidden="true" /></a>{:else}Android SDK License Agreement{/if}
						</span>
					</label>
					<button class="pill-btn primary big" onclick={beginSetup} disabled={!licenseAccepted || settingUp}>
						{#if settingUp}<LoaderCircle class="spin" size={14} strokeWidth={2} />{/if}
						<span>Set up Android emulator</span>
					</button>
					{#if view.message}<p class="fine detail">{view.message}</p>{/if}
				</article>
			</div>
		{:else if view.kind === 'installing'}
			{@const pct = installPercent(view.install)}
			<div class="card-scroll">
				<article class="card" aria-labelledby="phone-install-title">
					<div class="card-icon"><Download size={20} strokeWidth={1.6} /></div>
					<h2 id="phone-install-title">{view.install.state === 'extracting' ? 'Unpacking the Android SDK' : 'Downloading the Android SDK'}</h2>
					<div class="progress" role="progressbar" aria-label="Setup progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
						<div class="progress-fill" style:width={`${pct}%`}></div>
					</div>
					<div class="progress-meta">
						<span>{view.install.message || view.message || 'Working…'}</span>
						<span class="pct">{pct}%</span>
					</div>
					<p class="fine">You can close this panel; setup keeps going on the worker.</p>
				</article>
			</div>
		{:else if view.kind === 'error'}
			<div class="empty-state">
				<TriangleAlert size={26} strokeWidth={1.5} />
				<p class="empty-title">The Android emulator hit a problem</p>
				<p class="empty-copy wide">{view.message}</p>
				<button class="pill-btn" onclick={retry} disabled={refreshing || settingUp}>{refreshing || settingUp ? 'Retrying…' : 'Retry'}</button>
			</div>
		{:else if view.kind === 'no_device'}
			<div class="empty-state">
				<Smartphone size={26} strokeWidth={1.5} />
				<p class="empty-title">No virtual phone yet</p>
				<p class="empty-copy">The emulator is installed but has no device. Check again in a moment.</p>
				<button class="pill-btn" onclick={retry} disabled={refreshing}>Check again</button>
			</div>
		{:else if device}
			<div class="stage">
				<div class="screen-slot">
					{#if deviceState === 'running' || (deviceState === 'booting' && frame)}
						<div class="screen-frame" style:--aspect={frame ? `${frame.width} / ${frame.height}` : aspect}>
							<PhoneScreen bind:this={screen} bind:keyboardOpen {frame} {interactive} {stale} onSend={send} />
							{#if !frame && !linkFailure}
								<div class="screen-wait" role="status"><LoaderCircle class="spin" size={20} strokeWidth={1.5} /><span>{live ? 'Waiting for the first frame…' : 'Connecting to the phone…'}</span></div>
							{/if}
							{#if linkFailure}
								<div class="link-fail" role="alert">
									<PlugZap size={20} strokeWidth={1.6} />
									<p class="fail-title">Disconnected from the phone</p>
									<p class="fail-copy">{linkFailure}</p>
									<button class="pill-btn primary" onclick={reconnect}>Reconnect</button>
								</div>
							{:else if stale}
								<div class="stream-badge" role="status"><LoaderCircle class="spin" size={12} strokeWidth={2} /><span>Reconnecting · last image is stale</span></div>
							{:else if deviceState === 'booting'}
								<div class="stream-badge" role="status"><LoaderCircle class="spin" size={12} strokeWidth={2} /><span>Starting Android…</span></div>
							{/if}
						</div>
					{:else}
						<div class="device-ghost" class:booting={deviceState === 'booting'} style:--aspect={aspect}>
							{#if deviceState === 'booting' && linkFailure}
								<PlugZap size={24} strokeWidth={1.5} />
								<p class="empty-title">Disconnected from the phone</p>
								<p class="empty-copy">{linkFailure}</p>
								<button class="pill-btn primary" onclick={reconnect}>Reconnect</button>
							{:else if deviceState === 'booting'}
								<LoaderCircle class="spin" size={24} strokeWidth={1.5} />
								<p class="empty-title">Starting Android</p>
								<p class="empty-copy">{stateMessage || 'The first boot can take a few minutes.'}</p>
							{:else if deviceState === 'error'}
								<TriangleAlert size={24} strokeWidth={1.5} />
								<p class="empty-title">The phone stopped with an error</p>
								<p class="empty-copy">{stateMessage || status?.message || 'Start it again to retry.'}</p>
								<button class="pill-btn primary" onclick={start} disabled={powerBusy}><Play size={11} strokeWidth={2.25} /><span>Start again</span></button>
							{:else}
								<Power size={24} strokeWidth={1.5} />
								<p class="empty-title">{device.name} is off</p>
								<p class="empty-copy">Android {device.api > 0 ? `API ${device.api}` : ''} · {device.width}×{device.height}</p>
								<button class="pill-btn primary" onclick={start} disabled={powerBusy}>
									{#if powerBusy}<LoaderCircle class="spin" size={13} strokeWidth={2} />{:else}<Play size={11} strokeWidth={2.25} />{/if}
									<span>Start phone</span>
								</button>
							{/if}
						</div>
					{/if}
				</div>
				<nav class="android-nav" aria-label="Android navigation">
					<button onclick={() => press('BACK')} disabled={!interactive} aria-label="Back" title="Back (Esc)"><Triangle size={15} strokeWidth={1.9} style="transform: rotate(-90deg)" /></button>
					<button onclick={() => press('HOME')} disabled={!interactive} aria-label="Home" title="Home"><Circle size={15} strokeWidth={1.9} /></button>
					<button onclick={() => press('APP_SWITCH')} disabled={!interactive} aria-label="Recent apps" title="Recent apps"><Square size={14} strokeWidth={1.9} /></button>
				</nav>
			</div>
		{/if}
	</div>

	{#if device && deviceState === 'running'}
		<footer class="panel-note">
			<MousePointer2 size={12} strokeWidth={1.5} />
			<span>{mobile ? 'Tap, swipe, and use the keyboard button to type.' : 'Click to tap · drag to swipe · scroll · type when focused · Esc is Back'}</span>
		</footer>
	{/if}
</section>

<style>
	.phone-panel {
		outline: none;
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--background);
		container-type: inline-size;
	}

	/* ── Bar: same height, tint and controls as the Browser/Terminal bars ── */
	.bar {
		display: flex;
		align-items: center;
		gap: 6px;
		min-height: 40px;
		padding: 3px 6px 3px 10px;
		border-bottom: 1px solid var(--border);
		background: var(--muted);
		flex-shrink: 0;
	}
	.mobile .bar { padding-top: max(3px, env(safe-area-inset-top, 0px)); }
	.title {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.8125rem;
		font-weight: 600;
		color: var(--foreground);
		flex-shrink: 0;
	}
	.title :global(svg) { color: var(--muted-foreground); }
	.device-chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		height: 24px;
		padding: 0 9px;
		border-radius: 999px;
		background: color-mix(in oklch, var(--background) 70%, transparent);
		border: 1px solid color-mix(in oklch, var(--border) 70%, transparent);
		font-size: 0.6875rem;
		color: var(--foreground);
		overflow: hidden;
	}
	.device-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
	.device-meta { color: var(--muted-foreground); font-family: var(--font-mono, monospace); white-space: nowrap; }
	.dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; background: var(--muted-foreground); }
	.dot[data-tone='ok'] { background: var(--success); box-shadow: 0 0 0 3px color-mix(in oklch, var(--success) 25%, transparent); }
	.dot[data-tone='warn'] { background: var(--warning); animation: dot-pulse 1.4s ease-in-out infinite; }
	.dot[data-tone='bad'] { background: var(--destructive); }
	.spacer { flex: 1; min-width: 0; }
	.tools { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
	.bar > :global(.panel-actions) { margin-left: 2px; }

	.tool-btn {
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius) - 4px);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background 150ms ease, color 150ms ease, opacity 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.tool-btn:hover:not(:disabled) { background: var(--hover-overlay); color: var(--foreground); }
	.tool-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.tool-btn:disabled { opacity: 0.4; cursor: default; }
	.tool-btn.active { background: color-mix(in oklch, var(--primary) 14%, transparent); color: var(--primary); }
	.tool-btn.busy { opacity: 1; color: var(--foreground); }

	.pill-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		height: 28px;
		padding: 0 12px;
		border-radius: 999px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--foreground);
		font-size: 0.75rem;
		font-weight: 500;
		cursor: pointer;
		white-space: nowrap;
		transition: background 150ms ease, border-color 150ms ease, opacity 150ms ease, transform 120ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.pill-btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.pill-btn:active:not(:disabled) { transform: scale(0.97); }
	.pill-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.pill-btn:disabled { opacity: 0.45; cursor: default; }
	.pill-btn.primary { background: var(--primary); border-color: var(--primary); color: var(--primary-foreground, #fff); }
	.pill-btn.primary:hover:not(:disabled) { background: color-mix(in oklab, var(--primary) 88%, var(--foreground)); }
	.pill-btn.big { height: 36px; padding: 0 18px; font-size: 0.8125rem; }

	.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

	/* ── Body ───────────────────────────────────────────────────────────── */
	.body {
		position: relative;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
		background: color-mix(in oklch, var(--muted) 30%, transparent);
	}
	.banner {
		position: absolute;
		top: 8px;
		left: 8px;
		right: 8px;
		z-index: 4;
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 7px 8px 7px 10px;
		border-radius: calc(var(--radius) - 2px);
		border: 1px solid var(--border);
		background: var(--popover, var(--background));
		color: var(--foreground);
		font-size: 0.75rem;
		box-shadow: 0 6px 20px -8px color-mix(in oklch, var(--foreground) 30%, transparent);
		animation: drop 200ms ease-out;
	}
	.banner + .banner { top: 48px; }
	.banner + .banner + .banner { top: 88px; }
	.banner.notice :global(svg:first-child) { color: var(--warning); }

	/* A stop that reconnecting can't fix: a card over the greyed screen. */
	.link-fail {
		position: absolute;
		left: 50%;
		top: 50%;
		transform: translate(-50%, -50%);
		z-index: 3;
		width: min(86%, 300px);
		display: flex;
		flex-direction: column;
		align-items: center;
		gap: 6px;
		padding: 18px 16px 16px;
		text-align: center;
		border-radius: var(--radius);
		border: 1px solid color-mix(in oklab, var(--destructive) 40%, var(--border));
		background: color-mix(in oklch, var(--popover, var(--background)) 94%, transparent);
		backdrop-filter: blur(8px);
		color: var(--foreground);
		box-shadow: 0 12px 32px -12px rgb(0 0 0 / 0.55);
		animation: rise 220ms cubic-bezier(0.2, 0.7, 0.3, 1);
	}
	.link-fail :global(svg) { color: var(--destructive); }
	.fail-title { margin: 2px 0 0; font-size: 0.8125rem; font-weight: 600; }
	.fail-copy { margin: 0 0 6px; font-size: 0.75rem; line-height: 1.45; color: var(--muted-foreground); }
	.banner span { flex: 1; min-width: 0; overflow-wrap: anywhere; }
	.banner.bad { border-color: color-mix(in oklab, var(--destructive) 45%, var(--border)); }
	.banner.bad :global(svg:first-child) { color: var(--destructive); }
	.banner.ok :global(svg:first-child) { color: var(--success); }
	.banner code { font-family: var(--font-mono, monospace); font-size: 0.6875rem; }
	.banner button { display: flex; padding: 4px; border: 0; border-radius: 4px; background: transparent; color: var(--muted-foreground); cursor: pointer; }
	.banner button:hover { background: var(--hover-overlay); color: var(--foreground); }

	.empty-state {
		flex: 1;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 32px 24px;
		text-align: center;
		color: var(--muted-foreground);
	}
	.empty-title { margin: 4px 0 0; font-size: 0.8125rem; font-weight: 600; color: var(--foreground); }
	.empty-copy { margin: 0 0 6px; font-size: 0.75rem; max-width: 30ch; line-height: 1.45; color: color-mix(in oklch, var(--muted-foreground) 85%, transparent); }
	.empty-copy.wide { max-width: 44ch; overflow-wrap: anywhere; }

	/* ── Setup / host help card ─────────────────────────────────────────── */
	.card-scroll { flex: 1; min-height: 0; overflow-y: auto; display: flex; padding: 20px 16px; }
	.card {
		margin: auto;
		width: 100%;
		max-width: 440px;
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 20px;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		background: var(--background);
		animation: rise 260ms cubic-bezier(0.2, 0.7, 0.3, 1);
	}
	.card-icon {
		width: 38px;
		height: 38px;
		display: grid;
		place-items: center;
		border-radius: 10px;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		color: var(--primary);
		margin-bottom: 4px;
	}
	.card-icon.warn { background: color-mix(in oklch, var(--warning) 16%, transparent); color: var(--warning); }
	.card h2 { margin: 0; font-size: 0.9375rem; font-weight: 600; line-height: 1.3; color: var(--foreground); }
	.card h3 { margin: 8px 0 0; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted-foreground); }
	.card p { margin: 0; font-size: 0.8125rem; line-height: 1.5; color: var(--foreground); }
	.card p.fine { font-size: 0.75rem; color: var(--muted-foreground); }
	.card p.detail { font-family: var(--font-mono, monospace); font-size: 0.6875rem; overflow-wrap: anywhere; padding-top: 4px; }
	.card code { font-family: var(--font-mono, monospace); font-size: 0.75rem; padding: 1px 4px; border-radius: 4px; background: var(--muted); }
	.card > .pill-btn { align-self: flex-start; margin-top: 8px; }
	.cmd {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 6px 6px 10px;
		border-radius: calc(var(--radius) - 4px);
		background: var(--muted);
		border: 1px solid color-mix(in oklch, var(--border) 70%, transparent);
	}
	.cmd code { flex: 1; min-width: 0; padding: 0; background: none; overflow-x: auto; white-space: nowrap; }
	.cmd-copy { display: flex; padding: 5px; border: 0; border-radius: 4px; background: transparent; color: var(--muted-foreground); cursor: pointer; }
	.cmd-copy:hover { background: var(--hover-overlay); color: var(--foreground); }
	.cmd-copy:focus-visible { outline: 2px solid var(--ring); }
	.facts { list-style: none; margin: 2px 0; padding: 0; }
	.facts li { display: flex; align-items: flex-start; gap: 8px; font-size: 0.75rem; line-height: 1.5; color: var(--muted-foreground); }
	.facts li :global(svg) { margin-top: 3px; flex-shrink: 0; }
	.facts strong { color: var(--foreground); font-weight: 600; }
	.consent {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		margin-top: 6px;
		padding: 10px 12px;
		border-radius: calc(var(--radius) - 2px);
		border: 1px solid var(--border);
		font-size: 0.8125rem;
		line-height: 1.45;
		cursor: pointer;
		transition: border-color 150ms ease, background 150ms ease;
	}
	.consent:has(input:checked) { border-color: color-mix(in oklab, var(--primary) 50%, var(--border)); background: color-mix(in oklch, var(--primary) 5%, transparent); }
	.consent input { margin-top: 2px; width: 16px; height: 16px; accent-color: var(--primary); flex-shrink: 0; cursor: pointer; }
	.consent a { color: var(--primary); text-decoration: underline; text-underline-offset: 2px; }
	.consent a :global(svg) { display: inline; margin-left: 3px; vertical-align: -1px; }
	.progress { height: 8px; margin-top: 6px; border-radius: 999px; overflow: hidden; background: var(--muted); }
	.progress-fill {
		height: 100%;
		border-radius: inherit;
		background: linear-gradient(90deg, var(--primary), color-mix(in oklab, var(--primary) 70%, var(--background)), var(--primary));
		background-size: 200% 100%;
		animation: shimmer 1.6s linear infinite;
		transition: width 400ms ease;
	}
	.progress-meta { display: flex; justify-content: space-between; gap: 12px; font-size: 0.75rem; color: var(--muted-foreground); }
	.progress-meta .pct { font-family: var(--font-mono, monospace); color: var(--foreground); font-variant-numeric: tabular-nums; }

	/* ── Device stage ───────────────────────────────────────────────────── */
	.stage { flex: 1; min-height: 0; display: flex; flex-direction: column; }
	.screen-slot {
		flex: 1;
		min-height: 0;
		display: flex;
		align-items: center;
		justify-content: center;
		padding: 12px 12px 4px;
		container-type: size;
	}
	/* The largest box of the screen's aspect ratio that fits the slot. */
	.screen-frame,
	.device-ghost {
		position: relative;
		aspect-ratio: var(--aspect);
		width: min(100cqw, 100cqh * (var(--aspect)));
		max-height: 100cqh;
		display: flex;
	}
	.screen-frame {
		border-radius: 18px;
		padding: 6px;
		/* A phone's bezel is dark in either theme; the ring separates it from a dark panel. */
		background: #101014;
		box-shadow:
			0 0 0 1px color-mix(in oklch, var(--border) 90%, var(--muted-foreground)),
			inset 0 0 0 1px rgb(255 255 255 / 0.06),
			0 18px 40px -22px rgb(0 0 0 / 0.6);
	}
	.screen-frame > :global(.screen) { border-radius: 12px; background: #000; }
	.device-ghost {
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 20px;
		text-align: center;
		color: var(--muted-foreground);
		border-radius: 18px;
		border: 1.5px dashed color-mix(in oklch, var(--border) 90%, var(--muted-foreground));
		background: color-mix(in oklch, var(--background) 60%, transparent);
	}
	.device-ghost.booting { border-style: solid; animation: breathe 2.4s ease-in-out infinite; }
	.device-ghost .pill-btn { margin-top: 6px; }
	.screen-wait {
		position: absolute;
		inset: 6px;
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		border-radius: 12px;
		font-size: 0.75rem;
		color: color-mix(in oklch, #fff 70%, transparent);
		pointer-events: none;
	}
	/* Bottom of the screen, clear of the banners that drop in at the top. */
	.stream-badge {
		position: absolute;
		bottom: 22px;
		left: 50%;
		transform: translateX(-50%);
		z-index: 2;
		display: flex;
		align-items: center;
		gap: 5px;
		padding: 3px 9px;
		font-size: 0.6875rem;
		font-weight: 500;
		letter-spacing: 0.02em;
		white-space: nowrap;
		color: var(--foreground);
		background: color-mix(in oklch, var(--popover) 88%, transparent);
		border: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
		border-radius: 999px;
		backdrop-filter: blur(6px);
		pointer-events: none;
	}

	.android-nav {
		flex-shrink: 0;
		display: flex;
		justify-content: center;
		gap: clamp(20px, 12cqw, 56px);
		padding: 4px 12px 8px;
	}
	.android-nav button {
		width: 44px;
		height: 36px;
		display: grid;
		place-items: center;
		border: 0;
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background 150ms ease, color 150ms ease, transform 120ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.android-nav button:hover:not(:disabled) { background: var(--hover-overlay); color: var(--foreground); }
	.android-nav button:active:not(:disabled) { transform: scale(0.9); background: color-mix(in oklch, var(--primary) 14%, transparent); color: var(--primary); }
	.android-nav button:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.android-nav button:disabled { opacity: 0.35; cursor: default; }

	.panel-note {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 12px;
		border-top: 1px solid color-mix(in oklch, var(--border) 60%, transparent);
		font-size: 0.6875rem;
		color: color-mix(in oklch, var(--muted-foreground) 80%, transparent);
		flex-shrink: 0;
	}
	.panel-note :global(svg) { flex-shrink: 0; }
	.mobile .panel-note { padding-bottom: max(6px, env(safe-area-inset-bottom, 0px)); }

	.phone-panel :global(.spin) { animation: spin 1s linear infinite; }

	/* Narrow panels keep the essentials: the volume and power keys go. */
	@container (max-width: 400px) {
		.wide-only { display: none; }
		.device-meta { display: none; }
	}
	@container (max-width: 300px) {
		.device-chip { display: none; }
	}
	/* Touch: bigger targets. */
	@media (pointer: coarse) {
		.tool-btn { width: 36px; height: 36px; }
		.android-nav button { width: 56px; height: 44px; }
	}

	@keyframes spin { to { transform: rotate(360deg); } }
	@keyframes dot-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
	@keyframes shimmer { to { background-position: -200% 0; } }
	@keyframes breathe { 0%, 100% { border-color: color-mix(in oklab, var(--warning) 25%, var(--border)); } 50% { border-color: color-mix(in oklab, var(--warning) 70%, var(--border)); } }
	@keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
	@keyframes drop { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }

	@media (prefers-reduced-motion: reduce) {
		.dot[data-tone='warn'], .progress-fill, .device-ghost.booting, .card, .banner, .link-fail, .phone-panel :global(.spin) { animation: none; }
	}
</style>
