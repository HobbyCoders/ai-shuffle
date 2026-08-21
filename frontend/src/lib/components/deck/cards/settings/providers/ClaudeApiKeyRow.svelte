<script lang="ts">
	/**
	 * ClaudeApiKeyRow — the Anthropic API-key REPLACE-KEY panel (plan §1.1 row 2).
	 *
	 * Reduced to just the inline panel that the unified `ProviderRow` for the
	 * Anthropic-key row expands on "Edit". The row chrome (title / badge / status /
	 * masked meta / Remove) now lives in `ProviderList`, and the raw `fetch` calls
	 * moved into the `providers` store (`setAnthropicKey` / `clearAnthropicKey` /
	 * `loadIntegrations`) so there is one source of truth and `refreshAll` can
	 * refetch the status on every open.
	 *
	 * This panel only takes a new key and saves it via `providers.setAnthropicKey`,
	 * which throws with the server's message on failure — surfaced inline here.
	 * Renders inside `.prov-sub` (within the `.ed-content` shell) so the shell
	 * globals (`.inp` / `.ed-btn`) theme its controls, matching the other inline
	 * panels (`ProviderPanel`).
	 */
	import { Loader2 } from 'lucide-svelte';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		/** Close the panel (after a successful save, or on cancel). */
		onClose: () => void;
	}
	let { onClose }: Props = $props();

	let keyInput = $state('');
	let saving = $state(false);
	let error = $state('');

	async function save() {
		const value = keyInput.trim();
		if (!value || saving) return;
		saving = true;
		error = '';
		try {
			await providers.setAnthropicKey(value);
			keyInput = '';
			onClose();
		} catch (e) {
			error = e instanceof Error ? e.message : 'Save failed';
		} finally {
			saving = false;
		}
	}
</script>

<div class="acct-addpanel key-panel">
	<div class="key-field">
		<label class="key-label" for="anthropic-key">Replace API key</label>
		<input
			id="anthropic-key"
			type="password"
			class="inp"
			autocomplete="off"
			spellcheck="false"
			bind:value={keyInput}
			placeholder="sk-ant-…"
			disabled={saving}
		/>
		<p class="key-hint">
			Setting a new key keeps Claude Code authenticating with the key, overriding the connected
			account. Remove it from the row to revert to the account.
		</p>
	</div>

	{#if error}<div class="kc-foot err key-err" role="alert">{error}</div>{/if}

	<div class="acct-addrow">
		<button type="button" class="ed-btn primary" onclick={save} disabled={saving || !keyInput.trim()}>
			{#if saving}<Loader2 class="spin" />Saving…{:else}Save{/if}
		</button>
		<button type="button" class="ed-btn" onclick={onClose} disabled={saving}>Cancel</button>
	</div>
</div>

<style>
	/* Inline panel styles: a dashed-bordered `.acct-addpanel` (rendered
	   inside `.ed-content`, so `.inp` / `.ed-btn` globals theme the controls). */
	.acct-addpanel {
		margin-top: 10px;
		padding: 12px;
		border: 1px dashed var(--border);
		border-radius: 10px;
		background: var(--muted);
	}
	.key-panel {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}
	.key-field {
		display: flex;
		flex-direction: column;
		gap: 5px;
		min-width: 0;
	}
	.key-field .inp {
		width: 100%;
	}
	.key-label {
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.key-hint {
		margin: 0;
		font-size: 12px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}
	.acct-addrow {
		display: flex;
		gap: 8px;
	}
	.kc-foot {
		font-size: 12.5px;
	}
	.kc-foot.err {
		color: var(--destructive);
	}
	.key-err {
		padding: 0;
	}
	/* Saving spinner inside the primary button. */
	:global(.key-panel .ed-btn .spin) {
		width: 14px;
		height: 14px;
		animation: key-spin 0.8s linear infinite;
	}
	@keyframes key-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		:global(.key-panel .ed-btn .spin) {
			animation: none;
		}
	}
</style>
