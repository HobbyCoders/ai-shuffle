<script lang="ts">
	/**
	 * What Claude Code runs on (Subscription, Cloud, Off), on Claude's detail
	 * page whether or not anything is connected, so it stays reachable in every
	 * state. Separate from choosing among saved keys and sign-ins: "Use" on a saved
	 * account never changes this. The saved API key is not a mode: it is the
	 * "Claude API" provider beside the subscription. "Cloud" opens its page (the
	 * warning is there) and the control stays on the current mode until that
	 * finishes.
	 */
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { providers } from '$lib/stores/providers.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import { pickClaudeMode } from './provider-actions';
	import { MODE_OPTIONS, type ProviderView } from './providers-model';

	let { provider }: { provider: ProviderView } = $props();

	const access = getSettingsAccess();
	const claude = $derived(providers.current.claude);
	const mode = $derived<ClaudeAuthMode>(claude?.mode ?? 'subscription');
</script>

<RowGroup title="Claude Code mode">
	<SettingRow id="providers.claudeMode" hint="The Subscription choice syncs. Update all workers before relying on a switch: older workers may keep using their previous account. Cloud and Off apply only here. A saved API key runs beside any of these as Claude · API models.">
		<Segmented options={MODE_OPTIONS} value={mode} disabled={!claude || access.readOnly} onchange={(v: ClaudeAuthMode) => pickClaudeMode(provider, v)} />
	</SettingRow>
</RowGroup>
