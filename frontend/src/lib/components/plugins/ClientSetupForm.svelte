<script lang="ts">
	/**
	 * ClientSetupForm: the manual fallback for a plugin service with no OAuth
	 * client from any source. The owner copies the return address into the
	 * provider's app settings and enters the client id (and a secret, only
	 * for providers that need one). The secret lives only in this open form:
	 * it is cleared after saving, on failure, and on unmount, and the worker
	 * never sends it back.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { saveClient, type ServiceSignIn } from '$lib/api/plugin-oauth';

	interface Props {
		plugin: string;
		service: string;
		/** This worker's return address as the current browser reaches it. */
		redirectUri: string;
		/** The owner's current client id, when replacing one. */
		currentClientId?: string;
		/**
		 * The plugin ships a client this worker can't use (its sign-in would
		 * return through the account hub): the owner registers their own app.
		 */
		ownApp?: boolean;
		disabled?: boolean;
		onSaved: (status: ServiceSignIn) => void;
	}

	let { plugin, service, redirectUri, currentClientId = '', ownApp = false, disabled = false, onSaved }: Props = $props();
	const uid = $props.id();

	let clientId = $state('');
	let clientSecret = $state('');
	let busy = $state(false);
	let error = $state('');
	let copied = $state(false);
	let secure = $state(true);
	let copyTimer: ReturnType<typeof setTimeout> | undefined;

	onMount(() => { secure = window.isSecureContext; });
	onDestroy(() => { clientSecret = ''; clearTimeout(copyTimer); });

	async function copy() {
		try {
			await navigator.clipboard.writeText(redirectUri);
			copied = true;
			clearTimeout(copyTimer);
			copyTimer = setTimeout(() => (copied = false), 2000);
		} catch {
			error = 'Copy failed. Select the address and copy it.';
		}
	}

	async function save(event: SubmitEvent) {
		event.preventDefault();
		if (busy || disabled || !secure || !clientId.trim()) return;
		busy = true;
		error = '';
		const secret = clientSecret;
		clientSecret = '';
		try {
			const status = await saveClient(plugin, service, clientId.trim(), secret);
			clientId = '';
			onSaved(status);
		} catch (e) {
			// The worker never echoes the secret; its refusals are fixed wording.
			const status = (e as { status?: number })?.status;
			error = status === 422 ? 'Check the client ID and try again.' : getErrorMessage(e, 'The client could not be saved.');
		} finally {
			busy = false;
		}
	}
</script>

<form class="client-setup" onsubmit={save} autocomplete="off" aria-label="Set up {service} manually">
	<ol class="cs-steps">
		<li>
			<span class="cs-label" id="{uid}-ret">Return address</span>
			<div class="cs-copy">
				<code aria-labelledby="{uid}-ret">{redirectUri}</code>
				<button type="button" class="cs-btn" onclick={copy} aria-label="Copy return address">{copied ? 'Copied' : 'Copy'}</button>
			</div>
			<span class="cs-help">{ownApp ? 'Add this to your own app at the provider' : 'Add this to the provider'}</span>
		</li>
		<li>
			<label class="cs-field">
				<span class="cs-label">Client ID</span>
				<input class="cs-input" required maxlength="512" spellcheck="false" autocomplete="off"
					disabled={disabled || busy || !secure} bind:value={clientId} />
			</label>
			<span class="cs-help">{currentClientId ? 'Replaces the saved client' : 'From the provider’s app settings'}</span>
		</li>
		<li>
			<label class="cs-field">
				<span class="cs-label">Client secret <em>optional</em></span>
				<input class="cs-input" type="password" maxlength="1024" spellcheck="false" autocomplete="new-password"
					disabled={disabled || busy || !secure} bind:value={clientSecret} />
			</label>
			<span class="cs-help">Leave empty for public clients</span>
		</li>
	</ol>
	{#if !secure}<p class="cs-error" role="alert">Needs HTTPS or this computer.</p>{/if}
	{#if error}<p class="cs-error" role="alert">{error}</p>{/if}
	<div class="cs-actions">
		<button type="submit" class="cs-btn cs-primary" disabled={disabled || busy || !secure || !clientId.trim()}>
			{busy ? 'Saving…' : 'Save client'}
		</button>
	</div>
</form>

<style>
	.client-setup {
		display: grid;
		gap: 12px;
		min-width: 0;
		padding: 12px;
		border: 1px dashed color-mix(in oklab, var(--border) 100%, var(--foreground) 12%);
		border-radius: 10px;
		background: color-mix(in oklab, var(--muted) 30%, transparent);
	}
	.cs-steps {
		display: grid;
		gap: 12px;
		margin: 0;
		padding: 0;
		list-style: none;
		counter-reset: cs;
	}
	.cs-steps > li {
		display: grid;
		gap: 4px;
		min-width: 0;
		padding-left: 26px;
		position: relative;
		counter-increment: cs;
	}
	.cs-steps > li::before {
		content: counter(cs);
		position: absolute;
		left: 0;
		top: 0;
		display: grid;
		place-items: center;
		width: 18px;
		height: 18px;
		border-radius: 50%;
		border: 1px solid var(--border);
		font-size: 10.5px;
		font-weight: 600;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}
	.cs-field { display: grid; gap: 4px; min-width: 0; }
	.cs-label { font-size: 12.5px; font-weight: 550; color: var(--foreground); }
	.cs-label em { font-style: normal; font-weight: 400; color: var(--muted-foreground); }
	.cs-help { font-size: 11.5px; color: var(--muted-foreground); }
	.cs-copy {
		display: flex;
		align-items: stretch;
		gap: 6px;
		min-width: 0;
	}
	.cs-copy code {
		flex: 1;
		min-width: 0;
		padding: 7px 9px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: var(--background);
		font-family: var(--font-mono, ui-monospace, monospace);
		font-size: 11.5px;
		line-height: 1.45;
		overflow-wrap: anywhere;
		user-select: all;
	}
	.cs-input {
		box-sizing: border-box;
		width: 100%;
		min-width: 0;
		min-height: 36px;
		padding: 6px 10px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		font-family: var(--font-mono, ui-monospace, monospace);
		transition: border-color .15s ease-out, box-shadow .15s ease-out;
	}
	.cs-input:focus-visible {
		outline: none;
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklab, var(--primary) 22%, transparent);
	}
	.cs-input:disabled { opacity: .55; cursor: not-allowed; }
	.cs-actions { display: flex; justify-content: flex-end; }
	.cs-btn {
		min-height: 32px;
		padding: 0 12px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 500;
		cursor: pointer;
		white-space: nowrap;
		transition: background .15s ease-out, border-color .15s ease-out, transform .1s ease-out;
	}
	.cs-btn:hover:not(:disabled) { background: color-mix(in oklab, var(--foreground) 5%, var(--card)); }
	.cs-btn:active:not(:disabled) { transform: translateY(1px); }
	.cs-btn:focus-visible { outline: 2px solid var(--ring, var(--primary)); outline-offset: 2px; }
	.cs-btn:disabled { opacity: .5; cursor: not-allowed; }
	.cs-primary { background: var(--primary); border-color: var(--primary); color: var(--primary-foreground); }
	.cs-primary:hover:not(:disabled) { background: color-mix(in oklab, var(--primary) 88%, var(--foreground)); }
	.cs-error { margin: 0; font-size: 12px; color: var(--destructive); }
	@media (pointer: coarse) {
		.cs-btn, .cs-input { min-height: 44px; }
		.cs-input { font-size: 16px; }
	}
	@media (prefers-reduced-motion: reduce) {
		.cs-btn, .cs-input { transition: none; }
	}
</style>
