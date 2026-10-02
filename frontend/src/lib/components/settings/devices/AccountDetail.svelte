<script lang="ts">
	/**
	 * Shuffle account: sign in (browser tab or phone code), username, remote
	 * access with the connection-limit refusal, and sign-outs.
	 */
	import { Check, CircleUser, Copy, ExternalLink, Pencil, RefreshCw, Smartphone } from 'lucide-svelte';
	import { USERNAME_HINT } from '$lib/api/account';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { groupCode, type AccountFlow } from './account-flow.svelte';
	import { accountStatus, remoteShort } from './device-status';

	interface Props {
		flow: AccountFlow;
	}

	let { flow }: Props = $props();

	const status = $derived(accountStatus(flow));
	const c = $derived(flow.connection);
	const profile = $derived(flow.profile);
	const refusal = $derived(flow.refusal);
	const editing = $derived(flow.needsUsername || flow.editingName);

	async function saveName() {
		if (await flow.saveUsername()) flashSaved();
	}

	function openLicense() {
		settingsNav.go({ category: 'system', anchor: 'system.license' });
	}
</script>

<DetailPane
	name="Shuffle account"
	icon={CircleUser}
	status={status.tone}
	statusText={status.text}
	badge="Account"
	subtitle="Remote access and sync"
	category="devices"
	item="account"
	facts={flow.signedIn
		? [
				{ label: 'User', value: profile ? `@${profile.account.username}` : null },
				{ label: 'Remote access', value: remoteShort(flow) },
				{ label: 'Connections', value: refusal ? `${refusal.used} of ${refusal.limit}` : null },
				{ label: 'Workers', value: profile ? profile.servers.length : null }
			]
		: []}
	danger={flow.signedIn
		? [
				{
					label: 'Sign this worker out',
					hint: 'Remote access stops',
					button: 'Sign out',
					question: 'Sign this worker out?',
					onConfirm: () => flow.signOut(),
					disabled: flow.busy
				},
				...(flow.browserViaAccount
					? [
							{
								label: 'Sign this browser out',
								hint: 'Sign in again after',
								button: 'Sign out browser',
								question: 'Sign this browser out?',
								onConfirm: () => flow.signOutBrowser()
							}
						]
					: [])
			]
		: []}
>
	{#snippet primary()}
		{#if flow.signedIn}
			<button
				type="button"
				class="st-btn"
				class:spin={flow.loadingProfile}
				disabled={flow.loadingProfile || flow.busy || flow.savingUsername || c?.profile_ready === false}
				onclick={() => flow.loadProfile()}
			>
				<RefreshCw />Refresh
			</button>
		{/if}
	{/snippet}
	{#snippet banner()}
		{#if flow.statusError}
			<InlineBanner tone="warn">
				<span role="alert">{flow.statusError}</span>
				{#snippet actions()}
					<button type="button" class="st-btn sm" disabled={flow.busy} onclick={() => flow.refresh()}>Retry</button>
				{/snippet}
			</InlineBanner>
		{/if}
		{#if refusal}
			<InlineBanner tone="warn">
				<span role="alert">
					{refusal.message}
					{refusal.used} of {refusal.limit} remote {refusal.limit === 1 ? 'connection' : 'connections'} in use. This
					worker still works here and on your network.
				</span>
				{#snippet actions()}
					<button type="button" class="st-btn sm" onclick={openLicense}>Open License</button>
				{/snippet}
			</InlineBanner>
		{/if}
		{#if flow.errorText}
			<InlineBanner tone="warn"><span role="alert">{flow.errorText}</span></InlineBanner>
		{/if}
	{/snippet}

	{#if !c}
		<p class="st-empty" role="status">{flow.checking ? 'Checking account…' : 'Account status unavailable'}</p>
	{:else if !flow.signedIn}
		<RowGroup title="Sign in">
			<SettingRow id="devices.accountSignIn">
				{#if !flow.offerOpen}
					<button
						type="button"
						class="st-btn primary"
						disabled={flow.busy || !flow.canStart}
						onclick={() => flow.begin('signin')}
					>
						{flow.busy && flow.intent === 'signin' ? 'Preparing…' : 'Sign in'}
					</button>
				{:else}
					<button type="button" class="st-btn ghost" disabled={flow.busy} onclick={() => flow.signOut()}>
						{flow.busy ? 'Cancelling…' : 'Cancel'}
					</button>
				{/if}
			</SettingRow>
			{#if flow.offerOpen && c.code}
				{#if c.authorization_url}
					<SettingRow label="Finish in the new tab" hint="This worker joins right after">
						<a class="st-btn" href={c.authorization_url} target="_blank" rel="noopener noreferrer">
							<ExternalLink />Sign in in browser
						</a>
						<button type="button" class="st-btn" aria-expanded={flow.showPhone} onclick={() => flow.togglePhone()}>
							<Smartphone />{flow.showPhone ? 'Hide phone code' : 'Use my phone instead'}
						</button>
					</SettingRow>
				{/if}
				{#if flow.showPhone || !c.authorization_url}
					<div class="pairbox">
						{#if c.qr_svg}<div class="qr" aria-label="Worker connection QR code">{@html c.qr_svg}</div>{/if}
						<div class="pair-main">
							<span class="bigcode">{groupCode(c.code)}</span>
							<span class="rh">Links one worker, 10 min</span>
							<button type="button" class="st-btn sm" aria-label="Copy connection code" onclick={() => flow.copyCode()}>
								{#if flow.copied}<Check />Copied{:else}<Copy />Copy{/if}
							</button>
						</div>
					</div>
				{/if}
			{/if}
		</RowGroup>
	{:else}
		<RowGroup title="Profile">
			<SettingRow id="devices.username" label={flow.needsUsername ? 'Choose your username' : undefined} hint={editing ? 'Others see this name' : undefined}>
				{#if editing}
					<TextField bind:value={flow.usernameInput} maxlength={64} width={180} disabled={flow.savingUsername} />
					<button
						type="button"
						class="st-btn primary"
						disabled={flow.savingUsername || !flow.usernameInput.trim()}
						onclick={saveName}
					>
						{flow.savingUsername ? 'Saving…' : 'Save'}
					</button>
					{#if !flow.needsUsername}
						<button type="button" class="st-btn ghost" disabled={flow.savingUsername} onclick={() => flow.cancelEdit()}>
							Cancel
						</button>
					{/if}
				{:else if profile}
					<ValueText value="@{profile.account.username}" tone="fg" />
					<button type="button" class="st-btn icon ghost sm" aria-label="Edit username" title="Edit username" onclick={() => flow.startEdit()}>
						<Pencil />
					</button>
				{:else}
					<ValueText value={flow.loadingProfile ? 'Loading…' : c.profile_ready === false ? 'Finishing connection…' : ''} />
				{/if}
			</SettingRow>
			{#if flow.profileError}
				<SettingRow label={flow.profileError === USERNAME_HINT ? 'Username rules' : 'Profile did not load'}>
					<span class="err" role="alert">{flow.profileError}</span>
					{#if flow.profileError !== USERNAME_HINT}
						<button type="button" class="st-btn sm" onclick={() => flow.loadProfile()}>Retry profile</button>
					{/if}
				</SettingRow>
			{/if}
		</RowGroup>
		<RowGroup title="Remote access">
			<SettingRow id="devices.remoteAccess" hint={refusal ? 'Over the limit: LAN only' : undefined}>
				<ValueText status={c.status === 'connected' ? 'ok' : refusal ? 'warn' : 'off'} value={remoteShort(flow)} tone="fg" />
			</SettingRow>
			{#if flow.browserViaAccount}
				<SettingRow label="This browser">
					<ValueText value="Signed in through the account" />
				</SettingRow>
			{/if}
		</RowGroup>
	{/if}
</DetailPane>

<style>
	a.st-btn {
		text-decoration: none;
		color: inherit;
	}
	.pairbox {
		display: flex;
		gap: 16px;
		align-items: center;
		flex-wrap: wrap;
		padding: 14px 16px;
		border-top: 1px solid var(--st-border);
	}
	.qr {
		width: 148px;
		padding: 8px;
		background: white;
		border-radius: var(--st-radius-md);
	}
	.qr :global(svg) {
		display: block;
		width: 100%;
		height: auto;
	}
	.pair-main {
		display: grid;
		gap: 8px;
		justify-items: start;
		min-width: 0;
	}
	.bigcode {
		font-family: var(--st-mono);
		font-size: 22px;
		letter-spacing: 0.06em;
		font-weight: 500;
		user-select: all;
		overflow-wrap: anywhere;
	}
	.rh {
		font-size: 12.5px;
		color: var(--st-mfg);
	}
	.err {
		font-size: 12.5px;
		color: var(--st-destructive);
		white-space: normal;
		max-width: 320px;
	}
	.spin :global(svg.lucide) {
		animation: st-acct-spin 0.9s linear infinite;
	}
	@keyframes st-acct-spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.spin :global(svg.lucide) {
			animation: none;
		}
	}
</style>
