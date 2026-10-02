<script lang="ts">
	/**
	 * GitHub: signs the worker's `gh` CLI in (web device code or a personal
	 * access token) so agents can push, open PRs and read CI.
	 */
	import { Copy, ExternalLink, Github } from 'lucide-svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import TextField from '../rows/TextField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { GITHUB_DEVICE_URL, GITHUB_TOKENS_URL, type GitHubAuthFlow, type GitHubMethod } from './github-auth.svelte';

	interface Props {
		flow: GitHubAuthFlow;
	}

	let { flow }: Props = $props();

	const METHODS: { value: GitHubMethod; label: string }[] = [
		{ value: 'web', label: 'Web sign-in' },
		{ value: 'token', label: 'Access token' }
	];

	const connected = $derived(flow.authenticated === true);
	const status = $derived(flow.authenticated === null ? 'standby' : connected ? 'ok' : 'off');
	const statusText = $derived(
		flow.authenticated === null ? (flow.busy ? 'Checking' : 'Status unknown') : connected ? 'Connected' : 'Not connected'
	);
</script>

{#if flow.forbidden}
	<EmptyDetail icon={Github} title="GitHub" message={FORBIDDEN_TEXT} />
{:else}
	<DetailPane
		name="GitHub"
		icon={Github}
		{status}
		{statusText}
		badge="Account"
		subtitle={connected && flow.user ? `@${flow.user}` : 'Signs in gh here'}
		facts={connected
			? [
					{ label: 'Account', value: flow.user ? `@${flow.user}` : 'Connected' },
					{ label: 'Config folder', value: flow.configDir, mono: true }
				]
			: []}
		danger={connected
			? [
					{
						id: 'integrations.githubDisconnect',
						label: 'Disconnect GitHub',
						hint: 'Agents lose git push',
						button: 'Disconnect',
						question: 'Sign gh out?',
						onConfirm: () => flow.disconnect(),
						disabled: flow.busy
					}
				]
			: []}
	>
		{#snippet banner()}
			{#if flow.error}
				<InlineBanner tone="warn"><span role="alert">{flow.error}</span></InlineBanner>
			{/if}
		{/snippet}
		{#if connected}
			<RowGroup title="Access">
				<SettingRow label="Used by">
					<ValueText value="Git push, PRs and CI" tone="fg" />
				</SettingRow>
			</RowGroup>
		{:else if flow.authenticated === false}
			<RowGroup title="Connect">
				<SettingRow id="integrations.githubMethod">
					<Segmented
						options={METHODS}
						value={flow.method}
						disabled={flow.busy || !!flow.code}
						onchange={(m) => flow.setMethod(m)}
					/>
				</SettingRow>
				{#if flow.method === 'web'}
					{#if flow.code}
						<SettingRow label="One-time code" hint="Enter it on GitHub">
							<span class="gh-code" aria-label="One-time code">{flow.code}</span>
							<button type="button" class="st-btn sm" onclick={() => flow.copyCode()}>
								<Copy />{flow.copied ? 'Copied' : 'Copy code'}
							</button>
						</SettingRow>
						<SettingRow label="Waiting for GitHub" hint="Expires in 15 min">
							<a class="st-btn sm" href={GITHUB_DEVICE_URL} target="_blank" rel="noopener noreferrer">
								Open GitHub<ExternalLink />
							</a>
							<button type="button" class="st-btn ghost sm" disabled={flow.busy} onclick={() => flow.cancelWeb()}>
								Cancel sign-in
							</button>
						</SettingRow>
					{:else}
						<SettingRow label="GitHub account">
							<button type="button" class="st-btn primary" disabled={flow.busy} onclick={() => flow.startWeb()}>
								{flow.busy ? 'Loading…' : 'Sign in with GitHub'}
							</button>
						</SettingRow>
					{/if}
				{:else}
					<SettingRow label="Personal access token">
						<TextField
							type="password"
							mono
							width={200}
							placeholder="ghp_… or github_pat_…"
							bind:value={flow.token}
							disabled={flow.busy}
						/>
						<button
							type="button"
							class="st-btn primary"
							disabled={flow.busy || !flow.token.trim()}
							onclick={() => flow.connectToken()}
						>
							{flow.busy ? 'Connecting…' : 'Connect token'}
						</button>
					</SettingRow>
					<SettingRow label="No token yet">
						<a class="gh-link" href={GITHUB_TOKENS_URL} target="_blank" rel="noopener noreferrer">
							Create a token<ExternalLink />
						</a>
					</SettingRow>
				{/if}
			</RowGroup>
		{/if}
	</DetailPane>
{/if}

<style>
	.gh-code {
		font-family: var(--st-mono);
		font-size: 20px;
		letter-spacing: 0.08em;
		font-weight: 500;
		user-select: all;
	}
	a.st-btn {
		text-decoration: none;
		color: inherit;
	}
	.gh-link {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		font-size: 13px;
		color: var(--st-mfg);
		text-decoration: none;
	}
	.gh-link:hover {
		color: var(--st-fg);
	}
	.gh-link :global(svg.lucide) {
		width: 13px;
		height: 13px;
	}
</style>
