<script lang="ts">
	/**
	 * One connected credential under its provider, on the connector line. The
	 * line segment into a credential that is on is teal (`--primary`); every
	 * row but Claude's (and drafts) has its own switch.
	 */
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { moveListFocus } from '../rows/ListItem.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import Switch from '../rows/Switch.svelte';
	import type { CredentialView } from './providers-model';
	import { setCredentialEnabled } from './provider-actions';

	interface Props {
		credential: CredentialView;
		on: boolean;
		nextOn: boolean;
		last: boolean;
	}

	let { credential, on, nextOn, last }: Props = $props();

	const selected = $derived(settingsNav.selectedItem('providers') === credential.item);

	function select() {
		settingsNav.selectItem(credential.item, 'providers');
	}
</script>

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div
	class="cred"
	class:on
	class:nexton={nextOn}
	class:last
	aria-current={selected ? 'true' : undefined}
	data-settings-item={credential.item}
	onclick={select}
>
	<span class="tick" aria-hidden="true"></span>
	<span class="cdot"><StatusDot tone={credential.status} /></span>
	<button
		type="button"
		class="main"
		aria-label="{credential.name}, {credential.statusText}"
		data-settings-nav-item
		onclick={(e) => {
			e.stopPropagation();
			select();
		}}
		onkeydown={moveListFocus}
	>
		<span class="nm">{credential.name}</span>
		<span class="sm">{credential.summary}</span>
	</button>
	{#if credential.switchable}
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<span class="sw" onclick={(e) => e.stopPropagation()}>
			<Switch
				size="sm"
				checked={credential.enabled}
				label="{credential.name} on"
				onchange={(v) => setCredentialEnabled(credential, v)}
			/>
		</span>
	{:else}
		<span></span>
	{/if}
</div>

<style>
	.cred {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		min-height: 50px;
		padding: 0 10px 0 48px;
		border-radius: var(--st-radius-md);
		cursor: pointer;
		--rt: var(--st-border-strong);
		--rb: var(--st-border-strong);
		--tk: var(--st-border-strong);
	}
	.cred:hover {
		background: var(--st-hover);
	}
	.cred[aria-current='true'] {
		background: var(--st-accent);
	}
	/* The vertical connector: top half leads into this row, bottom half into the next. */
	.cred::before {
		content: '';
		position: absolute;
		left: 25px;
		top: 0;
		bottom: 0;
		width: var(--st-line);
		margin-left: calc(var(--st-line) / -2);
		background: linear-gradient(var(--rt) 50%, var(--rb) 50%);
		transition: background 0.25s;
	}
	.cred.last::before {
		bottom: 50%;
		background: var(--rt);
	}
	.tick {
		position: absolute;
		left: 25px;
		top: 50%;
		width: 10px;
		height: var(--st-line);
		margin-top: calc(var(--st-line) / -2);
		background: var(--tk);
		transition: background 0.25s;
	}
	.cdot {
		position: absolute;
		left: 35px;
		top: 50%;
		transform: translateY(-50%);
		display: flex;
	}
	.cred.on {
		--rt: var(--st-primary);
		--tk: var(--st-primary);
	}
	.cred.nexton {
		--rb: var(--st-primary);
	}
	.main {
		display: grid;
		min-width: 0;
		border: 0;
		background: none;
		padding: 4px 0;
		text-align: left;
		color: inherit;
		border-radius: var(--st-radius-sm);
	}
	.main:focus-visible {
		outline-offset: 2px;
	}
	.nm {
		font-size: 13.5px;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.sm {
		font-size: 12px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.sw {
		display: flex;
	}
	@media (prefers-reduced-motion: reduce) {
		.cred::before,
		.tick {
			transition: none;
		}
	}
</style>
