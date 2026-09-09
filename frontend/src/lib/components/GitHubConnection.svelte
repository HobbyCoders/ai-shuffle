<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	import { onMount, onDestroy } from 'svelte';
	import { Github, ExternalLink, Copy, Loader2 } from 'lucide-svelte';
	import { getGithubAuthStatus, startGithubWebLogin, loginGithubWithToken, cancelGithubWebLogin, logoutGithub } from '$lib/api/auth';
	import { getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	let { embedded = false }: { embedded?: boolean } = $props();

	let method = $state<'web' | 'token'>('web');
	let authenticated = $state(false);
	let user = $state<string | null>(null);
	let token = $state('');
	let code = $state('');
	let busy = $state(true);
	let error = $state('');
	let copied = $state(false);
	let timer: ReturnType<typeof setTimeout> | undefined;
	let deadline = 0;
	let disposed = false;
	let generation = 0;

	function stopPolling() {
		generation += 1;
		clearTimeout(timer);
		code = '';
	}

	async function readStatus() {
		const status = await getGithubAuthStatus();
		if (!disposed) {
			authenticated = status.authenticated;
			user = status.user;
		}
		return status;
	}

	onMount(() => {
		readStatus().catch((e) => { error = getErrorMessage(e, 'Could not load GitHub status.'); })
			.finally(() => { busy = false; });
	});
	onDestroy(() => { disposed = true; stopPolling(); });

	async function poll(id: number) {
		if (disposed || id !== generation) return;
		if (Date.now() >= deadline) {
			stopPolling();
			error = 'GitHub web sign-in expired. Please try again.';
			await cancelGithubWebLogin().catch(() => {});
			return;
		}
		try {
			const status = await getGithubAuthStatus();
			if (disposed || id !== generation) return;
			if (status.authenticated) {
				authenticated = true;
				user = status.user;
				error = status.web_login_error || '';
				stopPolling();
				await auth.checkAuth();
				return;
			}
			if (status.web_login_error) {
				error = status.web_login_error;
				stopPolling();
				return;
			}
			error = '';
		} catch (e) {
			if (disposed || id !== generation) return;
			error = getErrorMessage(e, 'Could not check GitHub sign-in. Retrying…');
		}
		if (!disposed && id === generation) timer = setTimeout(() => poll(id), 3000);
	}

	async function startWeb() {
		busy = true;
		error = '';
		copied = false;
		stopPolling();
		try {
			const result = await startGithubWebLogin();
			if (disposed) return;
			if (!result.success || !result.user_code) {
				error = result.error || 'Could not start GitHub web sign-in.';
				return;
			}
			code = result.user_code;
			deadline = Date.now() + 15 * 60 * 1000;
			timer = setTimeout(() => poll(generation), 3000);
		} catch (e) {
			error = getErrorMessage(e, 'Could not start GitHub web sign-in.');
		} finally { busy = false; }
	}

	async function cancelWeb() {
		busy = true;
		error = '';
		stopPolling();
		try { await cancelGithubWebLogin(); }
		catch (e) { error = getErrorMessage(e, 'Could not cancel GitHub sign-in.'); }
		finally { busy = false; }
	}

	async function connectToken() {
		if (!token.trim()) return;
		busy = true;
		error = '';
		try {
			const result = await loginGithubWithToken(token.trim());
			if (!result.success) {
				error = result.error || result.message || 'GitHub rejected this token.';
				return;
			}
			const status = await readStatus();
			if (!status.authenticated) {
				error = 'GitHub sign-in could not be confirmed. Please retry.';
				return;
			}
			token = '';
			await auth.checkAuth();
		} catch (e) {
			error = getErrorMessage(e, 'Could not connect to GitHub.');
		} finally { busy = false; }
	}

	async function disconnect() {
		busy = true;
		error = '';
		try {
			const result = await logoutGithub();
			if (!result.success) {
				error = result.error || 'Could not disconnect GitHub.';
				return;
			}
			const status = await readStatus();
			if (status.authenticated) error = 'GitHub is still connected. Check the server’s GitHub environment settings.';
			await auth.checkAuth();
		} catch (e) { error = getErrorMessage(e, 'Could not disconnect GitHub.'); }
		finally { busy = false; }
	}

	async function copyCode() {
		try { await copyText(code); copied = true; }
		catch { copied = false; error = COPY_ERROR_MESSAGE; }
	}
</script>

<section class="github-connection" class:embedded class:linked={authenticated} aria-label="GitHub connection">
	<h4><Github size={18} /> GitHub</h4>
	{#if authenticated}
		<p class="connected" role="status">{embedded ? (user ? `@${user}` : 'Connected') : `GitHub is connected${user ? ` as @${user}` : ''}.`}</p>
		<button type="button" onclick={disconnect} disabled={busy} aria-label="Disconnect GitHub">{embedded ? 'Disconnect' : 'Disconnect GitHub'}</button>
	{:else}
		<div class="actions" aria-label="GitHub sign-in method">
			<button type="button" aria-pressed={method === 'web'} disabled={busy || !!code} onclick={() => { method = 'web'; error = ''; }}>Web sign-in</button>
			<button type="button" aria-pressed={method === 'token'} disabled={busy || !!code} onclick={() => { method = 'token'; error = ''; }}>Personal access token</button>
		</div>
		{#if method === 'web'}
			{#if code}
				<div class="device-flow">
					<p>Enter this one-time code on GitHub to approve sign-in:</p>
					<div class="actions"><code>{code}</code><button type="button" onclick={copyCode}><Copy size={14} /> {copied ? 'Copied' : 'Copy code'}</button></div>
					<a href="https://github.com/login/device" target="_blank" rel="noopener noreferrer">Open GitHub to sign in <ExternalLink size={14} /></a>
					<p role="status"><Loader2 size={14} class="animate-spin" /> Waiting for approval on GitHub…</p>
					<button type="button" onclick={cancelWeb} disabled={busy}>Cancel sign-in</button>
				</div>
			{:else}
				<button class="primary" type="button" onclick={startWeb} disabled={busy}>{busy ? 'Loading…' : 'Sign in with GitHub'}</button>
			{/if}
		{:else}
			<form onsubmit={(event) => { event.preventDefault(); connectToken(); }}>
				<label for="github-personal-token">GitHub personal access token</label>
				<div class="actions">
					<input id="github-personal-token" type="password" autocomplete="off" spellcheck="false" bind:value={token} placeholder="ghp_… or github_pat_…" disabled={busy} />
					<button class="primary" type="submit" disabled={busy || !token.trim()}>{busy ? 'Connecting…' : 'Connect token'}</button>
				</div>
			</form>
			<a href="https://github.com/settings/tokens" target="_blank" rel="noopener noreferrer">Create a token <ExternalLink size={12} /></a>
		{/if}
	{/if}
	{#if error}<p class="error" role="alert">{error}</p>{/if}
</section>

<style>
	.github-connection { padding: 16px; border: 1px solid var(--border); border-radius: 10px; min-width: 0; color: var(--foreground); }
	h4 { display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; margin: 0 0 8px; }
	p { font-size: 13px; line-height: 1.5; color: var(--muted-foreground); margin: 8px 0 12px; overflow-wrap: anywhere; }
	.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
	button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px 12px; min-height: 36px; border: 1px solid var(--border); border-radius: 6px; background: var(--background); font-size: 13px; cursor: pointer; }
	button[aria-pressed="true"] { border-color: var(--primary); color: var(--primary); }
	button.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	button:disabled { opacity: .55; cursor: not-allowed; }
	button:focus-visible, input:focus-visible, a:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	label { display: block; font-size: 13px; margin-top: 12px; }
	input { flex: 1 1 180px; min-width: 0; width: 100%; padding: 9px; border: 1px solid var(--border); border-radius: 6px; background: var(--background); color: var(--foreground); }
	a { color: var(--primary); text-decoration: underline; display: inline-flex; align-items: center; gap: 4px; }
	code { font-size: 24px; letter-spacing: 2px; user-select: all; }
	.device-flow { margin-top: 12px; }
	.connected { color: var(--success, #16a34a); }
	.error { color: var(--destructive); margin-bottom: 0; }
	.embedded { padding: 10px 14px; border: 0; border-bottom: 1px solid var(--border); border-radius: 0; }
	.embedded.linked { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 10px; }
	.embedded.linked h4 { margin: 0; font-size: 13px; gap: 12px; }
	.embedded.linked h4 :global(svg) { color: var(--muted-foreground); }
	.embedded.linked .connected { flex: 1; margin: 0; font-size: 12px; }
	.embedded.linked > button { margin-left: auto; min-width: 104px; font-size: 12px; }
	.embedded.linked .error { flex-basis: 100%; }
	@media (max-width: 560px) { .embedded { padding-left: 12px; padding-right: 12px; } }
</style>
