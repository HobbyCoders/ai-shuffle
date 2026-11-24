<script lang="ts">
	/**
	 * Shared on/off switch for one credential. Built-in slots, gateways and
	 * fal.ai flip their own flag; Claude's saved keys and sign-ins are one-on-
	 * at-a-time per kind, so turning one on moves the selection to it. The
	 * subscription and Claude API switch independently (setCredentialEnabled).
	 */
	import { providers } from '$lib/stores/providers.svelte';
	import Switch from '../rows/Switch.svelte';
	import type { CredentialView } from './providers-model';
	import { setCredentialEnabled } from './provider-actions';

	let { credential, size = 'md', showState = false }: {
		credential: CredentialView;
		size?: 'sm' | 'md';
		showState?: boolean;
	} = $props();
	/**
	 * The Claude subscription and cloud switches change the Claude Code mode,
	 * which has no store flag, so the switch that started it holds one. Other
	 * switches use the store's own flags.
	 */
	let pending = $state(false);
	const modeSwitch = $derived(credential.providerId === 'claude' && (credential.flavor === 'cli' || credential.flavor === 'cloud'));
	const busy = $derived(pending || !!providers.current.togglingProviderId || !!providers.current.claudeAccountBusy);
	const saved = $derived(credential.account ?? credential.subscription);
	const saving = $derived(pending || providers.current.togglingProviderId === credential.row?.id ||
		(!!saved && providers.current.claudeAccountBusy === saved.id));
	/**
	 * A Claude row that is on but cannot run ("Values missing", "Not signed
	 * in", "Sign in again") says so beside its switch instead of "Active".
	 */
	const claudeProblem = $derived(credential.providerId === 'claude' && credential.attention);
	/** A failed switch says why right here, not only in the bell. */
	let error = $state('');
	async function setEnabled(enabled: boolean) {
		error = '';
		pending = modeSwitch;
		try {
			error = (await setCredentialEnabled(credential, enabled)) ?? '';
		} finally {
			pending = false;
		}
	}
</script>

<span class="wrap">
<span class="control" aria-busy={saving}>
	{#if showState || saving}<span class="state">{saving ? 'Saving…' : claudeProblem ? credential.statusText : credential.enabled ? (credential.active || saved ? 'Active' : 'On') : 'Off'}</span>{/if}
	<Switch {size} checked={credential.enabled} disabled={busy}
		label={credential.enabled ? `${credential.name} on` : `Use ${credential.name}`}
		onchange={(v) => setEnabled(v)} />
</span>
{#if error}<span class="err" role="alert">{error}</span>{/if}
</span>

<style>
	/* A column: a failed switch's message goes under the controls without widening them. */
	.wrap { display: inline-flex; flex-direction: column; align-items: flex-end; gap: 4px; flex: none; max-width: 100%; }
	.control { display: inline-flex; align-items: center; gap: 10px; flex: none; }
	/* Capped: on phone the control sits in the More menu, which sizes to its content. */
	.err { max-width: min(18rem, 70vw); text-align: right; font-size: 12px; color: var(--st-destructive); overflow-wrap: anywhere; }
	.state { font-size: 12px; color: var(--st-mfg); white-space: nowrap; }
</style>
