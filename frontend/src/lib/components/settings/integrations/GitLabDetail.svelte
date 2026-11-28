<script lang="ts">
	/**
	 * GitLab: connects this worker to gitlab.com or a self-managed server with
	 * a personal access token, so agents can push and the Git panel can open
	 * and merge merge requests.
	 */
	import { ExternalLink, Gitlab } from 'lucide-svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { gitlabTokensUrl, type GitLabAuthFlow } from './gitlab-auth.svelte';

	interface Props {
		flow: GitLabAuthFlow;
	}

	let { flow }: Props = $props();

	const connected = $derived(flow.authenticated === true);
	const status = $derived(
		flow.authenticated === null ? 'standby' : connected ? (flow.warning ? 'warn' : 'ok') : 'off'
	);
	const statusText = $derived(
		flow.authenticated === null
			? flow.busy
				? 'Checking'
				: 'Status unknown'
			: connected
				? flow.warning
					? 'Needs attention'
					: 'Connected'
				: 'Not connected'
	);
	const tokensUrl = $derived(gitlabTokensUrl(flow.serverInput));
</script>

{#if flow.forbidden}
	<EmptyDetail icon={Gitlab} title="GitLab" message={FORBIDDEN_TEXT} />
{:else}
	<DetailPane
		name="GitLab"
		icon={Gitlab}
		{status}
		{statusText}
		badge="Account"
		subtitle={connected && flow.user ? `@${flow.user}` : 'Personal access token'}
		facts={connected
			? [
					{ label: 'Account', value: flow.user ? `@${flow.user}` : 'Connected' },
					{ label: 'Server', value: flow.host, mono: true }
				]
			: []}
		danger={connected
			? [
					{
						label: 'Disconnect GitLab',
						hint: 'Agents lose git push to GitLab',
						button: 'Disconnect',
						question: 'Forget the GitLab token?',
						onConfirm: () => flow.disconnect(),
						disabled: flow.busy
					}
				]
			: []}
	>
		{#snippet banner()}
			{#if flow.error}
				<InlineBanner tone="warn"><span role="alert">{flow.error}</span></InlineBanner>
			{:else if connected && flow.warning}
				<InlineBanner tone="warn"><span role="alert">{flow.warning}</span></InlineBanner>
			{/if}
		{/snippet}
		{#if connected}
			<RowGroup title="Access">
				<SettingRow label="Used by">
					<ValueText value="Git push, merge requests and cloning" tone="fg" />
				</SettingRow>
			</RowGroup>
		{:else if flow.authenticated === false}
			<RowGroup title="Connect">
				<SettingRow label="Server" hint="gitlab.com or your own GitLab">
					<TextField
						mono
						width={200}
						placeholder="https://gitlab.com"
						bind:value={flow.serverInput}
						disabled={flow.busy}
					/>
				</SettingRow>
				<SettingRow label="Personal access token" hint="Scopes: api, write_repository">
					<TextField
						type="password"
						mono
						width={200}
						placeholder="glpat-…"
						bind:value={flow.token}
						disabled={flow.busy}
					/>
					<button
						type="button"
						class="st-btn primary"
						disabled={flow.busy || !flow.token.trim()}
						onclick={() => flow.connect()}
					>
						{flow.busy ? 'Connecting…' : 'Connect token'}
					</button>
				</SettingRow>
				<SettingRow label="No token yet">
					<a class="gl-link" href={tokensUrl} target="_blank" rel="noopener noreferrer">
						Create a token<ExternalLink />
					</a>
				</SettingRow>
			</RowGroup>
		{/if}
	</DetailPane>
{/if}

<style>
	.gl-link {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 13px;
		color: var(--st-mfg);
		text-decoration: none;
	}
	.gl-link:hover {
		color: var(--st-fg);
	}
	.gl-link :global(svg.lucide) {
		width: 13px;
		height: 13px;
	}
</style>
