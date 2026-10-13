<script lang="ts">
	/** Claude subscription > Policy: the loop guard and the turns it holds. */
	import type { ClaudeAuthMode } from '$lib/api/claude-auth';
	import { FORBIDDEN_TEXT, getSettingsAccess } from '$lib/settings/access.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import Switch from '../../rows/Switch.svelte';
	import NumberField from '../../rows/NumberField.svelte';
	import ValueText from '../../rows/ValueText.svelte';
	import { heldTitle, promptHint, type LoopGuard } from '../loop-guard.svelte';

	interface Props {
		guard: LoopGuard;
		mode: ClaudeAuthMode;
	}

	let { guard, mode }: Props = $props();
	// Only owners can confirm or dismiss a held turn; others see the list only (main 2a5e2c2).
	const access = getSettingsAccess();
	const s = $derived(guard.status);
</script>

{#if guard.forbidden}
	<p class="pv-msg">{FORBIDDEN_TEXT}</p>
{:else if !s}
	<p class="pv-msg" role="status">{guard.loadError || 'Loading the loop guard'}</p>
{:else if mode !== 'subscription'}
	<RowGroup title="Loop guard">
		<SettingRow
			id="providers.loopGuard"
			label="Loop guard"
			hint={mode === 'off' ? undefined : 'Console spend limit applies'}
		>
			<ValueText value={mode === 'off' ? 'Off while Claude is off' : 'Off in API key mode'} />
		</SettingRow>
	</RowGroup>
{:else}
	<RowGroup title="Loop guard">
		<SettingRow
			id="providers.loopGuard"
			hint={s.enabled ? `This hour: ${s.usage.install_last_hour} of ${s.install_turns_per_hour}` : undefined}
			value={s.enabled}
			defaultValue={s.defaults.enabled}
			onReset={() => guard.save({ enabled: s.defaults.enabled })}
		>
			<Switch checked={s.enabled} disabled={guard.busy} onchange={(v) => guard.save({ enabled: v })} />
		</SettingRow>
		{#if s.enabled}
			<SettingRow
				id="providers.loopSession"
				hint={s.session_turns_per_hour === s.defaults.session_turns_per_hour
					? undefined
					: `Default ${s.defaults.session_turns_per_hour}`}
				value={s.session_turns_per_hour}
				defaultValue={s.defaults.session_turns_per_hour}
				onReset={() => guard.save({ session_turns_per_hour: s.defaults.session_turns_per_hour })}
			>
				<NumberField
					value={s.session_turns_per_hour}
					min={1}
					max={1000}
					unit="per hour"
					disabled={guard.busy}
					onchange={(v) => guard.save({ session_turns_per_hour: v })}
				/>
			</SettingRow>
			<SettingRow
				id="providers.loopWorker"
				hint={s.install_turns_per_hour === s.defaults.install_turns_per_hour
					? undefined
					: `Default ${s.defaults.install_turns_per_hour}`}
				value={s.install_turns_per_hour}
				defaultValue={s.defaults.install_turns_per_hour}
				onReset={() => guard.save({ install_turns_per_hour: s.defaults.install_turns_per_hour })}
			>
				<NumberField
					value={s.install_turns_per_hour}
					min={1}
					max={10000}
					unit="per hour"
					disabled={guard.busy}
					onchange={(v) => guard.save({ install_turns_per_hour: v })}
				/>
			</SettingRow>
			<SettingRow
				id="providers.loopBackground"
				value={s.worker_max_minutes}
				defaultValue={s.defaults.worker_max_minutes}
				onReset={() => guard.save({ worker_max_minutes: s.defaults.worker_max_minutes })}
			>
				<NumberField
					value={s.worker_max_minutes}
					min={0}
					unit="min"
					disabled={guard.busy}
					onchange={(v) => guard.save({ worker_max_minutes: v })}
				/>
			</SettingRow>
		{/if}
	</RowGroup>
{/if}

{#if s}
	<RowGroup title="Waiting for you" count={s.held.length} id="providers-held">
		{#each s.held as turn (turn.id)}
			<SettingRow label={heldTitle(turn.source, turn.held_at)} hint={promptHint(turn.prompt)}>
				{#if !access.readOnly}
					<button
						type="button"
						class="st-btn primary sm"
						disabled={guard.busy}
						onclick={() => guard.act(turn.id, 'confirm')}>Confirm</button
					>
					<button
						type="button"
						class="st-btn ghost sm"
						disabled={guard.busy}
						onclick={() => guard.act(turn.id, 'dismiss')}>Dismiss</button
					>
				{/if}
			</SettingRow>
		{:else}
			<SettingRow label="No held turns" />
		{/each}
	</RowGroup>
{/if}
{#if guard.saveError}<p class="pv-msg err" role="alert">{guard.saveError}</p>{/if}
