<script lang="ts">
	/**
	 * A pairing code from "Add device": kind, the address it carries, the
	 * code with its QR, and the countdown. It works once.
	 */
	import { Check, Copy, QrCode } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import {
		KIND_LABEL,
		KIND_NAME,
		endpointLabel,
		enrollmentCommand,
		type PairingCodes,
		type PairingItem,
		type PairingKind
	} from './pairing-codes.svelte';

	interface Props {
		pairing: PairingCodes;
		item: PairingItem;
	}

	let { pairing, item }: Props = $props();

	const HELP: Record<PairingKind, string> = {
		worker:
			'Another computer running Shuffle joins this worker’s network and gets your settings, agents and provider keys. Use it for a worker on your home network or a VPN such as Tailscale; workers on the same Shuffle account find each other on their own.',
		client: 'Scan with the Shuffle app, or open the link in the browser you want to pair. It connects right away.',
		runner: 'Run the command on the worker set up for this shared database. No address is embedded.'
	};

	const grant = $derived(item.grant);
	const link = $derived(pairing.link(item));
	const kindOptions = $derived(pairing.kinds.map((k) => ({ value: k, label: KIND_LABEL[k] })));
	const addressOptions = $derived(
		pairing.endpoints.length
			? pairing.endpoints.map((e) => ({ value: e.url, label: endpointLabel(e) }))
			: [{ value: '', label: 'No address detected' }]
	);
	const blocker = $derived(pairing.blocker(item));
	const busy = $derived(item.creating || item.cancelling);
	const statusText = $derived(
		grant ? 'Works once' : item.creating ? 'Creating' : item.expired ? 'Code expired' : 'No code yet'
	);
	const copyLinkLabel = $derived(item.kind === 'worker' ? 'Copy invite link' : 'Copy pairing link');

	async function cancel() {
		if (await pairing.cancel(item)) settingsNav.clearSelection('devices');
	}
</script>

<DetailPane
	name={KIND_NAME[item.kind]}
	icon={QrCode}
	status={grant || item.creating ? 'busy' : 'off'}
	{statusText}
	badge={KIND_LABEL[item.kind]}
	subtitle={grant ? 'Waiting for the device' : undefined}
	category="devices"
	item={item.id}
	facts={grant
		? [
				{ label: 'Uses left', value: '1' },
				{
					label: 'Address',
					value: item.kind === 'runner' ? null : grant.grant.endpoint_url || item.endpointUrl || null,
					mono: true
				},
				{ label: 'Expires in', value: pairing.countdown(item), mono: true }
			]
		: []}
	danger={[
		{
			label: 'Cancel this code',
			hint: 'The code stops working',
			button: 'Cancel code',
			question: 'Cancel the code?',
			onConfirm: cancel,
			disabled: item.cancelling
		}
	]}
>
	{#snippet primary()}
		{#if !grant}
			<button type="button" class="st-btn primary" disabled={busy || !!blocker} onclick={() => pairing.create(item)}>
				{item.creating ? 'Creating…' : item.expired ? 'Create new code' : 'Create code'}
			</button>
		{/if}
	{/snippet}
	{#snippet banner()}
		{#if item.expired && !grant}
			<InlineBanner tone="info"><span role="status">The code expired. Create a new one.</span></InlineBanner>
		{/if}
		{#if item.error}
			<InlineBanner tone="warn"><span role="alert">{item.error}</span></InlineBanner>
		{/if}
		{#if blocker}
			<InlineBanner tone="warn"><span role="alert">{blocker}</span></InlineBanner>
		{/if}
		{#if pairing.endpointError}
			<InlineBanner tone="warn">
				<span role="alert">{pairing.endpointError}</span>
				{#snippet actions()}
					<button
						type="button"
						class="st-btn sm"
						disabled={pairing.loadingEndpoints}
						onclick={() => pairing.loadEndpoints()}
					>
						Retry addresses
					</button>
				{/snippet}
			</InlineBanner>
		{/if}
	{/snippet}

	<RowGroup title="Code">
		<SettingRow label="Kind" help={HELP[item.kind]}>
			<Segmented
				options={kindOptions}
				value={item.kind}
				disabled={busy}
				onchange={(kind) => pairing.change(item, { kind })}
			/>
		</SettingRow>
		{#if item.kind !== 'runner'}
			<SettingRow label="Address the device uses">
				<SelectField
					value={item.endpointUrl}
					options={addressOptions}
					width={280}
					disabled={busy || !pairing.endpoints.length}
					onchange={(endpointUrl) => pairing.change(item, { endpointUrl })}
				/>
			</SettingRow>
		{/if}
		{#if grant}
			<div class="pairbox">
				{#if grant.qr_svg}
					<div class="qr" aria-label={item.kind === 'worker' ? 'Worker invite QR code' : 'Pairing QR code'}>
						{@html grant.qr_svg}
					</div>
				{/if}
				<div class="pair-main">
					<span class="bigcode" class:long={grant.display_code.length > 12}>{grant.display_code}</span>
					<div class="acts">
						<button
							type="button"
							class="st-btn sm"
							aria-label="Copy code"
							onclick={() => pairing.copy(item, 'code', grant.display_code)}
						>
							{#if item.copied === 'code'}<Check />Copied{:else}<Copy />Copy code{/if}
						</button>
						{#if item.kind === 'runner'}
							<button
								type="button"
								class="st-btn sm"
								aria-label="Copy enrollment command"
								onclick={() => pairing.copy(item, 'link', enrollmentCommand(grant.display_code))}
							>
								{#if item.copied === 'link'}<Check />Copied{:else}<Copy />Copy command{/if}
							</button>
						{:else if link}
							<button
								type="button"
								class="st-btn sm"
								aria-label={copyLinkLabel}
								onclick={() => pairing.copy(item, 'link', link)}
							>
								{#if item.copied === 'link'}<Check />Copied{:else}<Copy />{copyLinkLabel}{/if}
							</button>
						{/if}
					</div>
					{#if item.kind === 'runner'}<code class="cmd">{enrollmentCommand(grant.display_code)}</code>{/if}
				</div>
			</div>
		{/if}
	</RowGroup>
</DetailPane>

<style>
	.pairbox {
		display: flex;
		gap: 18px;
		align-items: center;
		flex-wrap: wrap;
		padding: 16px;
		border-top: 1px solid var(--st-border);
	}
	.qr {
		width: 156px;
		padding: 8px;
		background: white;
		border-radius: var(--st-radius-md);
		flex: none;
	}
	.qr :global(svg) {
		display: block;
		width: 100%;
		height: auto;
	}
	.pair-main {
		display: grid;
		gap: 10px;
		justify-items: start;
		min-width: 0;
		flex: 1 1 200px;
	}
	.bigcode {
		font-family: var(--st-mono);
		font-size: 24px;
		letter-spacing: 0.05em;
		font-weight: 500;
		user-select: all;
		overflow-wrap: anywhere;
	}
	.bigcode.long {
		font-size: 17px;
	}
	.acts {
		display: flex;
		gap: 8px;
		flex-wrap: wrap;
	}
	.cmd {
		font-family: var(--st-mono);
		font-size: 11.5px;
		color: var(--st-mfg);
		overflow-wrap: anywhere;
	}
</style>
