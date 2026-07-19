<script lang="ts">
	/**
	 * ClaudeApiKeyRow — the Anthropic API-key integration row (plan §1.1 row 2).
	 *
	 * Lifted VERBATIM (mechanics unchanged) from ConnectionsSection's Anthropic-key
	 * keycard + its self-contained state: the raw `fetch` calls to
	 * `/settings/integrations` (load) and `/settings/integrations/anthropic`
	 * (POST/DELETE) are preserved exactly (landmine L6 — a relocated raw fetch, NOT
	 * a rewrite). State stays component-local, as it was.
	 *
	 * The key is a Claude Code AUTHENTICATION method: when set, Claude Code runs
	 * all sessions with it (ANTHROPIC_API_KEY), overriding the connected account.
	 * The Claude-subscription row above needs to know which method is active to
	 * show its "overridden by API key" status, so this row reports the active
	 * method up via `onAuthMethodChange` after every load/save/remove.
	 */
	import { onMount } from 'svelte';

	interface Props {
		/** Fired with `true` when Claude Code is authenticating via this API key. */
		onAuthMethodChange?: (usingApiKey: boolean) => void;
	}
	let { onAuthMethodChange }: Props = $props();

	let anthropicKeySet = $state(false);
	let anthropicKeyMasked = $state('');
	let anthropicKeyInput = $state('');
	let anthropicSaving = $state(false);
	let anthropicError = $state('');
	let anthropicSuccess = $state('');

	// Which auth method Claude Code is actively using, from the backend:
	// 'api_key' (key overrides the account) or 'account' (connected OAuth).
	// API-key login wins whenever a key is set — no silent billing switch.
	let claudeCodeAuthMethod = $state('account');
	let usingApiKeyAuth = $derived(claudeCodeAuthMethod === 'api_key');

	function reportMethod() {
		onAuthMethodChange?.(claudeCodeAuthMethod === 'api_key');
	}

	// Load the anthropic key status from the shared /settings/integrations payload.
	async function loadKeyStatus() {
		try {
			const res = await fetch('/api/v1/settings/integrations', { credentials: 'include' });
			if (!res.ok) return;
			const data = await res.json();
			anthropicKeySet = !!data.anthropic_api_key_set;
			anthropicKeyMasked = data.anthropic_api_key_masked ?? '';
			claudeCodeAuthMethod = data.claude_code_auth_method ?? (data.anthropic_api_key_set ? 'api_key' : 'account');
			reportMethod();
		} catch (e) {
			// Silent — non-critical, user can retry by reopening settings
			console.warn('Failed to load credential status', e);
		}
	}

	async function saveAnthropicKey() {
		if (!anthropicKeyInput.trim()) return;
		anthropicSaving = true;
		anthropicError = '';
		anthropicSuccess = '';
		try {
			const res = await fetch('/api/v1/settings/integrations/anthropic', {
				method: 'POST',
				credentials: 'include',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ api_key: anthropicKeyInput.trim() }),
			});
			if (!res.ok) {
				const body = await res.json().catch(() => ({}));
				anthropicError = body.detail ?? `Save failed (${res.status})`;
				return;
			}
			const data = await res.json();
			anthropicKeySet = true;
			anthropicKeyMasked = data.masked_key ?? '';
			anthropicKeyInput = '';
			// Setting a key makes Claude Code authenticate with it, overriding
			// the connected account.
			claudeCodeAuthMethod = 'api_key';
			reportMethod();
			anthropicSuccess = 'Saved — Claude Code now uses this API key';
			setTimeout(() => (anthropicSuccess = ''), 2500);
		} catch (e) {
			anthropicError = e instanceof Error ? e.message : 'Network error';
		} finally {
			anthropicSaving = false;
		}
	}

	async function removeAnthropicKey() {
		anthropicSaving = true;
		anthropicError = '';
		anthropicSuccess = '';
		try {
			const res = await fetch('/api/v1/settings/integrations/anthropic', {
				method: 'DELETE',
				credentials: 'include',
			});
			if (!res.ok) {
				anthropicError = `Remove failed (${res.status})`;
				return;
			}
			anthropicKeySet = false;
			anthropicKeyMasked = '';
			// Clearing the key reverts Claude Code to the connected account.
			claudeCodeAuthMethod = 'account';
			reportMethod();
			anthropicSuccess = 'Removed — Claude Code reverted to the connected account';
			setTimeout(() => (anthropicSuccess = ''), 2500);
		} catch (e) {
			anthropicError = e instanceof Error ? e.message : 'Network error';
		} finally {
			anthropicSaving = false;
		}
	}

	onMount(() => {
		loadKeyStatus();
	});
</script>

<div class="keycard">
	<div class="kc-ic">
		<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
			<path d="M6.5 4h3l5.5 16h-3l-1.2-3.6H6.2L5 20H2L6.5 4zm.4 9.6h3.2L8.5 8.3 6.9 13.6zM15.5 4h3L22 20h-3L15.5 4z"/>
		</svg>
	</div>
	<div class="kc-body">
		<h4>Anthropic API key
			<span class="sb off">API key</span>
			{#if usingApiKeyAuth}<span class="sb ok">Active</span>{:else}<span class="sb off">Not set</span>{/if}
		</h4>
		{#if anthropicKeySet}
			<p>Claude Code authenticates with this key, overriding the connected account. <code class="kc-val">{anthropicKeyMasked}</code></p>
		{:else}
			<p>Authenticate Claude Code with an API key instead of your account. Setting it overrides the connected account; removing it reverts to the account.</p>
		{/if}
	</div>
	<div class="kc-acts">
		{#if anthropicKeySet}
			<button type="button" class="ed-btn danger" onclick={removeAnthropicKey} disabled={anthropicSaving}>Remove</button>
		{:else}
			<input
				type="password"
				autocomplete="off"
				value={anthropicKeyInput}
				oninput={(e) => (anthropicKeyInput = e.currentTarget.value)}
				placeholder="sk-ant-…"
				class="inp kc-in"
			/>
			<button type="button" class="ed-btn primary" onclick={saveAnthropicKey} disabled={anthropicSaving || !anthropicKeyInput.trim()}>
				{anthropicSaving ? '…' : 'Use this key'}
			</button>
		{/if}
	</div>
</div>

{#if anthropicError}<div class="kc-foot err" role="alert">{anthropicError}</div>{/if}
{#if anthropicSuccess}<div class="kc-foot ok">{anthropicSuccess}</div>{/if}

<style>
	/* Keycard styles relocated from ConnectionsSection (visuals unchanged). */
	.keycard {
		display: flex;
		gap: 14px;
		align-items: flex-start;
		padding: 12px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.kc-ic {
		flex-shrink: 0;
		width: 38px;
		height: 38px;
		border-radius: 10px;
		display: grid;
		place-items: center;
		background: var(--background);
		color: var(--foreground);
	}
	.kc-ic :global(svg) {
		width: 20px;
		height: 20px;
	}
	.kc-body {
		flex: 1;
		min-width: 0;
	}
	.kc-body h4 {
		margin: 1px 0 3px;
		font-size: 13.5px;
		font-weight: 600;
		color: var(--foreground);
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.kc-body p {
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
		line-height: 1.5;
	}
	.kc-val {
		font-family: var(--font-mono, monospace);
		font-size: 11.5px;
		color: var(--muted-foreground);
	}
	.kc-acts {
		flex-shrink: 0;
		display: flex;
		gap: 8px;
		align-items: center;
		align-self: center;
		flex-wrap: wrap;
		justify-content: flex-end;
	}
	.kc-acts .kc-in {
		width: 200px;
		min-width: 0;
	}
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.sb.off {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.kc-foot {
		padding: 8px 2px 4px;
		font-size: 12.5px;
	}
	.kc-foot.ok {
		color: var(--success, #16a34a);
	}
	.kc-foot.err {
		color: var(--destructive);
	}

	@media (max-width: 560px) {
		.keycard {
			flex-wrap: wrap;
		}
		.kc-acts {
			width: 100%;
			justify-content: flex-start;
		}
		.kc-acts .kc-in {
			width: 100%;
		}
	}
</style>
