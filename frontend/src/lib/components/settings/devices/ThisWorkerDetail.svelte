<script lang="ts">
	/**
	 * This worker: its name, platform and version, and the Network tab
	 * (current network, joining another one with an invite, keeping its own
	 * settings, and the account-network situations).
	 */
	import { LogIn, Server } from 'lucide-svelte';
	import { NETWORK_STATE_LABELS } from '$lib/api/network-status';
	import {
		abandonHandoffTransfer,
		getLanSettings,
		updateLanSettings,
		getAdvertisedAddresses,
		updateAdvertisedAddresses,
		type AdvertisedAddresses,
		type HandoffTransferRecovery,
		type LanSettings
	} from '$lib/api/network';
	import { getErrorMessage } from '$lib/api/client';
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
	import Switch from '../rows/Switch.svelte';
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
	let releaseWakes = $state<Record<string, boolean>>({});
	let recoveryError = $state('');

	async function abandon(transfer: HandoffTransferRecovery) {
		recoveryError = '';
		try {
			await abandonHandoffTransfer(transfer.token, releaseWakes[transfer.token] ?? false);
			await model.loadStatus();
		} catch (e) {
			recoveryError = getErrorMessage(e, 'Could not abandon the transfer');
		}
	}

	async function rename(name: string) {
		if (await model.rename('self', name)) flashSaved();
		else rev += 1;
	}

	let aliases = $state<AdvertisedAddresses | null>(null);
	let aliasesError = $state('');
	let aliasesRev = $state(0);
	$effect(() => {
		if (model.enrolled) void loadAliases();
	});
	async function loadAliases() {
		try {
			aliases = await getAdvertisedAddresses();
		} catch (e) {
			aliasesError = getErrorMessage(e, 'Could not load advertised addresses');
		}
	}
	async function saveAliases(text: string) {
		aliasesError = '';
		try {
			aliases = await updateAdvertisedAddresses(text.split(/[\s,]+/).filter(Boolean));
			flashSaved();
		} catch (e) {
			aliasesError = getErrorMessage(e, 'Could not save advertised addresses');
		}
		aliasesRev += 1;
	}

	const LAN_HELP =
		'Workers on the same local network sync directly instead of through the tunnel. The connection is authenticated and encrypted by the workers themselves; if it fails, sync uses the tunnel.';
	let lan = $state<LanSettings | null>(null);
	let lanAddresses = $state('');
	let lanError = $state('');
	let lanRev = $state(0);
	const lanAdvertised = $derived((lan?.advertised ?? []).filter((url) => url.startsWith('http://')));

	$effect(() => {
		if (model.enrolled && lan === null) void loadLan();
	});

	async function loadLan() {
		try {
			lan = await getLanSettings();
			lanAddresses = lan.addresses.join(', ');
		} catch (e) {
			lanError = getErrorMessage(e, 'Could not load the local network settings');
		}
	}

	async function saveLan(body: { enabled?: boolean; addresses?: string[] }): Promise<boolean> {
		lanError = '';
		try {
			lan = await updateLanSettings(body);
			lanAddresses = lan.addresses.join(', ');
			flashSaved();
			return true;
		} catch (e) {
			lanError = getErrorMessage(e, 'Could not save the local network settings');
			lanRev += 1;
			return false;
		}
	}

	function saveLanAddresses(text: string) {
		void saveLan({ addresses: text.split(/[\s,]+/).filter(Boolean) });
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
		{ label: 'Revision', value: status.revision ?? 'Unknown', mono: true },
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
				<SettingRow label="Revision"><ValueText value={status.revision ?? 'Unknown'} mono tone="fg" /></SettingRow>
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
		{#if aliases}
			<RowGroup title="Advertised addresses">
				{#key aliasesRev}
					<SettingRow label="Additional URLs" hint="At most two; HTTP or HTTPS"
						help="URLs peers use to reach this worker, including reverse proxies and mapped ports. These are shared with your network and accepted for endpoint binding, even with a managed tunnel. Use ASCII (punycode) hostnames, no passwords or query strings. HTTPS is recommended.">
						<TextField value={aliases.addresses.join(', ')} mono width={220}
							label="Advertised addresses" placeholder="https://worker.example.com"
							onchange={saveAliases} />
					</SettingRow>
				{/key}
			</RowGroup>
		{/if}
		{#if aliasesError}<InlineBanner tone="warn"><span role="alert">{aliasesError}</span></InlineBanner>{/if}
		{#if lan}
			<RowGroup title="Local network">
				<SettingRow label="Sync directly on the LAN" hint="Tunnel is the fallback" help={LAN_HELP}>
					<Switch checked={lan.enabled} onchange={(enabled) => saveLan({ enabled })} />
				</SettingRow>
				{#key lanRev}
					<SettingRow label="Extra LAN addresses" hint="Docker: the host's IP and port">
						<TextField
							value={lanAddresses}
							mono
							width={220}
							placeholder="10.0.0.20:8001"
							label="Extra LAN addresses"
							disabled={!lan.enabled}
							onchange={saveLanAddresses}
						/>
					</SettingRow>
				{/key}
				<SettingRow label="Peers on this network dial">
					<ValueText value={lanAdvertised.length ? lanAdvertised.join(', ') : 'No LAN address'} mono />
				</SettingRow>
			</RowGroup>
		{/if}
		{#if lanError}<InlineBanner tone="warn"><span role="alert">{lanError}</span></InlineBanner>{/if}
		{#if status.handoff_transfers?.pending.length}
			<RowGroup title="Unfinished chat transfers">
				<InlineBanner tone="warn">Messages held on the source worker may be missing. Wake-ups stay paused until delivery finishes or you abandon the transfer.</InlineBanner>
				{#each status.handoff_transfers.pending as transfer (transfer.token)}
					<SettingRow label={transfer.title || 'Chat'} hint={`Waiting for ${transfer.source}`}>
						{#if transfer.can_abandon}
							<label><input type="checkbox" bind:checked={releaseWakes[transfer.token]} /> Resume remaining wake-ups</label>
							<InlineConfirm label="Abandon transfer"
								question={releaseWakes[transfer.token] ? 'Earlier messages may be missing. Resume remaining wake-ups anyway?' : 'Keep this chat here and discard its paused wake-ups?'}
								onConfirm={() => abandon(transfer)} />
						{:else}
							<ValueText value={transfer.reason === 'delivering' ? 'Delivering messages' : 'Recovery available after five minutes offline'} />
						{/if}
					</SettingRow>
				{/each}
				{#if recoveryError}<InlineBanner tone="warn"><span role="alert">{recoveryError}</span></InlineBanner>{/if}
			</RowGroup>
		{/if}
		{#if status.handoff_transfers?.history.length}
			<RowGroup title="Recovered chat transfers">
				{#each status.handoff_transfers.history as transfer}
					<SettingRow label={transfer.title || 'Chat'} hint={transfer.reason === 'source_revoked' ? 'Source worker revoked; paused wake-ups discarded' : transfer.reason === 'owner_resumed_wakes' ? 'Transfer abandoned; remaining wake-ups resumed' : 'Transfer abandoned; paused wake-ups discarded'} />
				{/each}
			</RowGroup>
		{/if}
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
					width={184}
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
