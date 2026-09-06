<script lang="ts">
	import { onDestroy } from 'svelte';
	import { api, getErrorMessage } from '$lib/api/client';
	import { auth } from '$lib/stores/auth.svelte';
	import { ExternalLink, Loader2 } from 'lucide-svelte';

	let { onAuthenticated }: { onAuthenticated: () => void | Promise<void> } = $props();
	let busy = $state(false);
	let error = $state('');
	let authorizationUrl = $state('');
	let completionSecret = $state('');
	let completionInput = $state('');
	let showCode = $state(false);
	let requestId = '';
	let hubOrigin = '';
	let popup: Window | null = null;
	let timer: ReturnType<typeof setTimeout> | undefined;
	let request: AbortController | undefined;
	let generation = 0;
	let expiresAt = 0;

	function stop() {
		generation++;
		clearTimeout(timer);
		request?.abort();
		request = undefined;
		busy = false;
	}

	onDestroy(stop);

	async function post<T>(path: string, body: unknown): Promise<T> {
		request = new AbortController();
		const controller = request;
		const timeout = setTimeout(() => controller.abort(), 15_000);
		try { return await api.post<T>(path, body, { signal: controller.signal }); }
		finally { clearTimeout(timeout); }
	}

	function receiveCompletion(event: MessageEvent) {
		if (!busy || !popup || event.source !== popup || event.origin !== hubOrigin) return;
		const data = event.data;
		if (data?.type !== 'shuffle.browser.complete' || data.request_id !== requestId) return;
		if (typeof data.completion_secret === 'string' && /^[A-Za-z0-9_-]{43}$/.test(data.completion_secret)) {
			completionSecret = data.completion_secret;
		}
	}

	function submitCode(event: SubmitEvent) {
		event.preventDefault();
		const code = completionInput.trim();
		if (!/^[A-Za-z0-9_-]{43}$/.test(code)) { error = 'Paste the complete sign-in code from the account tab.'; return; }
		error = '';
		completionSecret = code;
		completionInput = '';
	}

	async function poll(current: number) {
		if (generation !== current) return;
		if (Date.now() >= expiresAt) {
			stop();
			error = 'Sign-in expired. Please try again.';
			return;
		}
		try {
			const result = await post<{ status: 'pending' | 'authenticated' }>(
				'/auth/account/poll', completionSecret ? { completion_secret: completionSecret } : {}
			);
			if (generation !== current) return;
			if (result.status === 'authenticated') {
				await auth.checkAuth();
				if (generation !== current) return;
				if (!auth.current.status?.authenticated) throw new Error('The worker could not verify your session.');
				stop();
				popup?.close();
				await onAuthenticated();
			} else {
				timer = setTimeout(() => void poll(current), 2000);
			}
		} catch (e) {
			if (generation !== current) return;
			stop();
			error = getErrorMessage(e, 'Sign-in could not be completed. Please try again.');
		}
	}

	async function signIn() {
		stop();
		const current = generation;
		busy = true;
		error = '';
		authorizationUrl = '';
		completionSecret = '';
		completionInput = '';
		showCode = false;
		requestId = '';
		hubOrigin = '';
		// A completion proof comes only from this popup and the pinned hub origin.
		popup = window.open('about:blank', '_blank');
		try {
			const result = await post<{ request_id: string; authorization_url: string; expires_at: string }>(
				'/auth/account/start', {}
			);
			if (generation !== current) return;
			const url = new URL(result.authorization_url);
			if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Invalid sign-in address.');
			expiresAt = Math.min(Date.parse(result.expires_at), Date.now() + 10 * 60_000);
			if (!Number.isFinite(expiresAt) || expiresAt <= Date.now()) throw new Error('Sign-in expired. Please try again.');
			if (!/^[A-Za-z0-9_-]{43}$/.test(result.request_id)) throw new Error('Invalid sign-in request.');
			requestId = result.request_id;
			hubOrigin = url.origin;
			authorizationUrl = url.href;
			if (popup) popup.location.replace(authorizationUrl);
			void poll(current);
		} catch (e) {
			if (generation !== current) return;
			popup?.close();
			stop();
			error = getErrorMessage(e, 'Unable to start account sign-in. Please try again.');
		}
	}
</script>

<svelte:window onmessage={receiveCompletion} />

<div class="space-y-4">
	{#if busy}
		<p role="status" class="text-sm text-muted-foreground flex items-center gap-2">
			<Loader2 size={18} class="animate-spin" />{completionSecret ? 'Confirming your sign-in…' : 'Finish signing in in the account tab.'}
		</p>
		{#if authorizationUrl}
			<a href={authorizationUrl} target="_blank" rel="noopener noreferrer" class="btn w-full">
				<ExternalLink size={16} />Open sign-in
			</a>
		{/if}
		{#if !completionSecret}
			<button type="button" class="text-sm underline" onclick={() => showCode = !showCode}>Use a completion code</button>
			{#if showCode}
				<form onsubmit={submitCode} class="space-y-2">
					<label for="completion-code" class="text-sm">Code from the account tab</label>
					<input id="completion-code" class="input" bind:value={completionInput} autocomplete="off" spellcheck={false} />
					<button type="submit" class="btn w-full">Complete sign-in</button>
				</form>
			{/if}
		{/if}
		<button type="button" class="btn w-full" onclick={() => { stop(); popup?.close(); }}>Cancel</button>
	{:else}
		<button type="button" class="btn btn-primary w-full py-3" onclick={signIn}>Sign in to AI Shuffle</button>
	{/if}
	{#if error}<p role="alert" class="text-sm text-destructive">{error}</p>{/if}
</div>
