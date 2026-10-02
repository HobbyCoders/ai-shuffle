<script lang="ts">
	/**
	 * This worker: its name, platform and version, and the Network tab
	 * (current network, joining another one with an invite, keeping its own
	 * settings, and the account-network situations).
	 */
	import { LogIn, Server } from 'lucide-svelte';
	import { NETWORK_STATE_LABELS } from '$lib/api/network-status';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import type { StatusTone } from '$lib/settings/types';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import TextField from '../rows/TextField.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import { JoinFlow } from './join-flow.svelte';
	import type { DevicesModel } from './devices-model.svelte';

	interface Props {
		model: DevicesModel;
	}

	let { model }: Props = $props();

	const SYNC_HELP =
		'Your data stays on your workers. Workers sync while they run; one that was offline catches up when it returns.';
	const TONE: Record<string, StatusTone> = {
		ready: 'ok',
		syncing: 'busy',
		waiting: 'busy',
		'needs-setup': 'warn',
		offline: 'err'
	};

	const join = new JoinFlow({
		status: () => model.status,
		replace: () => model.enrolled,
		onChanged: () => void model.loadStatus()
	});

	const status = $derived(model.status!);
	const network = $derived(status.network);
	const accountWorkerNames = $derived(model.accountWorkers.map((d) => d.name || 'a worker').join(', '));
	let rev = $state(0);

	async function rename(name: string) {
		if (await model.rename('self', name)) flashSaved();
		else rev += 1;
	}
</script>

<DetailPane
	name={model.workerName}
	icon={Server}
	status={TONE[model.uiState]}
	statusText={NETWORK_STATE_LABELS[model.uiState]}
	badge="This worker"
	subtitle={status.platform ?? undefined}
	category="devices"
	item="self"
	facts={[
		{ label: 'Platform', value: status.platform },
		{ label: 'Version', value: status.version, mono: true },
		{ label: 'Network', value: model.enrolled ? network?.name || 'Your network' : 'None' },
		{ label: 'Workers', value: model.enrolled && network ? network.member_count : null }
	]}
	tabs={['Overview', 'Network']}
>
	{#snippet children(tab)}
		{#if tab === 'Overview'}
			<RowGroup title="Device">
				{#key rev}
					<SettingRow label="Name" hint={model.enrolled && model.self ? undefined : 'Join a network to rename'}>
						<TextField
							value={model.workerName}
							maxlength={100}
							disabled={!model.enrolled || !model.self || model.saving !== null}
							onchange={rename}
						/>
					</SettingRow>
				{/key}
				<SettingRow label="Platform"><ValueText value={status.platform ?? 'Unknown'} tone="fg" /></SettingRow>
				<SettingRow label="Version"><ValueText value={status.version ?? 'Unknown'} mono tone="fg" /></SettingRow>
			</RowGroup>
		{:else}
			{@render networkTab()}
		{/if}
	{/snippet}
</DetailPane>

{#snippet networkTab()}
	{#if !model.enrolled}
		{#if model.enrollmentPending && join.enrollment}
			<RowGroup title="Network">
				<SettingRow
					label="{join.enrollment.status === 'syncing' ? 'Copying settings from' : 'Joining'} {join.enrollment
						.endpoint_url || 'the inviting worker'}"
					hint="Finishes when it is online"
				>
					<StatusDot tone="busy" />
					<button type="button" class="st-btn ghost" disabled={join.busy} onclick={() => join.cancel()}>Cancel</button>
				</SettingRow>
			</RowGroup>
		{:else if model.waitingOnAccount}
			<RowGroup title="Network">
				<SettingRow label="Joining your account's network" hint="Admitted when another is online">
					<StatusDot tone="busy" />
				</SettingRow>
				{#if accountWorkerNames}
					<SettingRow label="Account workers"><ValueText value={accountWorkerNames} /></SettingRow>
				{/if}
			</RowGroup>
		{:else}
			{@render joinFields()}
			<RowGroup title="Or keep this worker on its own">
				<SettingRow label="Keep separate settings" hint="Start its own network">
					<button type="button" class="st-btn" disabled={model.startingFresh} onclick={() => model.startFresh()}>
						{model.startingFresh ? 'Setting up…' : 'Keep separate settings'}
					</button>
				</SettingRow>
			</RowGroup>
		{/if}
	{:else}
		{#if model.switchingToAccount}
			<InlineBanner tone="info">
				<span role="status">
					Switching to your account's settings. Your account already syncs {accountWorkerNames || 'your other workers'}.
				</span>
				{#snippet actions()}
					{#if status.hub?.last_error}
						<button type="button" class="st-btn sm" disabled={model.adopting} onclick={() => model.adopt()}>
							{model.adopting ? 'Retrying…' : 'Retry'}
						</button>
					{/if}
				{/snippet}
			</InlineBanner>
		{/if}
		{#if status.hub?.last_error}
			<InlineBanner tone="warn"><span role="alert">{status.hub.last_error}</span></InlineBanner>
		{/if}
		<RowGroup title="Current network">
			<SettingRow label={network?.name || 'Your network'} help={SYNC_HELP}>
				<ValueText
					status="ok"
					tone="fg"
					value="{network?.member_count ?? 1} {network?.member_count === 1 ? 'worker' : 'workers'}, direct sync"
				/>
			</SettingRow>
		</RowGroup>
		{@render joinFields()}
	{/if}
{/snippet}

{#snippet joinFields()}
	<RowGroup title={model.enrolled ? 'Join another network' : 'Join a network'} id="join">
		{#if join.failed && join.enrollment}
			<SettingRow label="The last join {join.enrollment.status}">
				{#if join.enrollment.error}<span class="err" role="alert">{join.enrollment.error}</span>{/if}
				<button type="button" class="st-btn ghost" disabled={join.busy} onclick={() => join.cancel()}>Dismiss</button>
			</SettingRow>
		{/if}
		<SettingRow label="Invite link or code" hint="Home network or Tailscale">
			<TextField
				bind:value={join.input}
				mono
				width={240}
				placeholder="shuffle://network/join?…"
				oninput={() => join.inputChanged()}
			/>
		</SettingRow>
		{#if join.pairingLinkPasted}
			<SettingRow label="That is a pairing link" hint="Use a Worker invite instead">
				<ValueText value="For phones and browsers" />
			</SettingRow>
		{:else if join.parsed}
			<SettingRow label="Worker"><ValueText value={join.parsed.endpoint || '—'} mono tone="fg" /></SettingRow>
			<SettingRow label="Code"><ValueText value={join.parsed.code || '—'} mono tone="fg" /></SettingRow>
		{:else}
			<SettingRow label="Or worker address">
				<TextField
					bind:value={join.endpoint}
					mono
					width={160}
					placeholder="http://10.0.0.5:8000"
					label="Worker address"
				/>
				<TextField bind:value={join.code} mono width={130} placeholder="ABCD-EFGH-JKLM" label="Code" />
			</SettingRow>
		{/if}
		<SettingRow
			label={model.enrolled ? 'Leave this network and join' : 'Join with this invite'}
			hint={model.enrolled ? 'Invited network wins' : undefined}
		>
			{#if model.enrolled}
				<InlineConfirm
					question="Leave {network?.name || 'this network'}?"
					confirmLabel="Leave and join"
					tone="primary"
					disabled={join.busy || !join.ready || join.pairingLinkPasted}
					onConfirm={() => join.join(true)}
				>
					{#snippet trigger(arm)}
						<button
							type="button"
							class="st-btn primary"
							disabled={join.busy || !join.ready || join.pairingLinkPasted}
							onclick={arm}
						>
							<LogIn />Join
						</button>
					{/snippet}
				</InlineConfirm>
			{:else}
				<button
					type="button"
					class="st-btn primary"
					disabled={join.busy || !join.ready || join.pairingLinkPasted}
					onclick={() => join.join()}
				>
					<LogIn />{join.busy ? 'Joining…' : 'Join'}
				</button>
			{/if}
		</SettingRow>
		{#if join.error}
			<SettingRow label="Could not join"><span class="err" role="alert">{join.error}</span></SettingRow>
		{/if}
	</RowGroup>
{/snippet}

<style>
	.err {
		font-size: 12.5px;
		color: var(--st-destructive);
		white-space: normal;
		max-width: 320px;
	}
</style>
