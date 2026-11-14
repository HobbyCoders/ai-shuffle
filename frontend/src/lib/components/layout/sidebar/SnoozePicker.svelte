<script lang="ts">
	/**
	 * SnoozePicker — when a snoozed chat comes back to Active.
	 *
	 * The content of the sidebar's snooze sheet (SidebarSheet supplies the
	 * surface: a popover on desktop, a bottom sheet on the phone). Quick choices
	 * first, each with the exact time it means; then a custom date and time in
	 * the viewer's own timezone. A time that is not ahead of now is refused in
	 * words, next to the field.
	 */
	import { AlarmClock } from 'lucide-svelte';
	import { parseCustomSnooze, snoozePresets, toLocalInputValue, snoozeLabel } from '../session-card-data';

	interface Props {
		/** The clock when the picker opened: presets and the field's minimum use it. */
		now: number;
		/** How many chats this snoozes (the selection bar can pick several). */
		count?: number;
		/** True on the phone shell: larger targets. */
		phone?: boolean;
		onPick: (at: Date) => void;
	}

	let { now, count = 1, phone = false, onPick }: Props = $props();

	const presets = $derived(snoozePresets(new Date(now)));
	const heading = $derived(count > 1 ? `Snooze ${count} chats until` : 'Snooze until');
	// The field starts on the furthest preset's time, so a small change is quick.
	let custom = $state('');
	let error = $state<string | null>(null);
	const uid = $props.id();
	const fieldId = `snooze-custom-${uid}`;
	const errorId = `${fieldId}-error`;
	const minValue = $derived(toLocalInputValue(new Date(now + 60_000)));

	$effect(() => {
		// A fresh open resets the custom field and its message.
		void now;
		custom = toLocalInputValue(presets[presets.length - 1]?.at ?? new Date(now + 3_600_000));
		error = null;
	});

	function presetTime(at: Date): string {
		return snoozeLabel(at.getTime(), now).replace(/^until /, '');
	}

	function submitCustom(event: SubmitEvent) {
		event.preventDefault();
		const result = parseCustomSnooze(custom, Date.now());
		if ('error' in result) {
			error = result.error;
			return;
		}
		error = null;
		onPick(result.at);
	}
</script>

<div class="snooze" class:phone>
	<h4><AlarmClock size={14} strokeWidth={1.75} aria-hidden="true" />{heading}</h4>
	<div class="presets" role="group" aria-label="Quick choices">
		{#each presets as preset (preset.id)}
			<button
				type="button"
				class="preset"
				aria-label={`${preset.label}, ${presetTime(preset.at)}`}
				onclick={() => onPick(preset.at)}
			>
				<span class="name">{preset.label}</span>
				<span class="when">{presetTime(preset.at)}</span>
			</button>
		{/each}
	</div>
	<form class="custom" onsubmit={submitCustom} novalidate>
		<label for={fieldId}>Pick a date and time</label>
		<div class="row">
			<input
				id={fieldId}
				type="datetime-local"
				bind:value={custom}
				min={minValue}
				aria-invalid={error ? 'true' : undefined}
				aria-describedby={error ? errorId : undefined}
				oninput={() => (error = null)}
			/>
			<button type="submit" class="go">Snooze</button>
		</div>
		<p id={errorId} class="err" aria-live="polite">{error ?? ''}</p>
	</form>
</div>

<style>
	.snooze { display: flex; flex-direction: column; gap: 6px; }
	h4 {
		display: flex;
		align-items: center;
		gap: 6px;
		margin: 0 4px 2px;
		font-size: 13px;
		font-weight: 600;
		color: var(--muted-foreground);
	}
	.presets { display: flex; flex-direction: column; gap: 1px; }
	.preset {
		display: flex;
		align-items: center;
		gap: 10px;
		height: 32px;
		padding: 0 8px;
		border: 0;
		border-radius: var(--radius-control);
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		text-align: left;
		cursor: pointer;
	}
	.phone .preset { height: 44px; font-size: 14.5px; }
	.preset:hover { background: var(--row-hover); }
	.preset:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.preset .when {
		margin-left: auto;
		color: var(--muted-foreground);
		font-variant-numeric: tabular-nums;
	}
	.custom {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 4px;
		padding: 8px 4px 0;
		border-top: 1px solid var(--border);
	}
	label { font-size: 12px; color: var(--muted-foreground); }
	/* The native date-time field needs the full width, so Snooze sits under it. */
	.row { display: flex; flex-direction: column; gap: 6px; }
	input {
		width: 100%;
		min-width: 0;
		box-sizing: border-box;
		height: 30px;
		padding: 0 8px;
		border: 1px solid var(--border);
		border-radius: var(--radius-control);
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		color-scheme: light dark;
	}
	.phone input { height: 40px; font-size: 16px; }
	input:focus-visible { outline: 2px solid var(--ring); outline-offset: -1px; }
	input[aria-invalid='true'] { border-color: var(--destructive); }
	.go {
		align-self: flex-end;
		height: 30px;
		padding: 0 12px;
		border: 0;
		border-radius: var(--radius-control);
		background: var(--action);
		color: var(--action-foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		cursor: pointer;
	}
	.phone .go { height: 40px; font-size: 14px; }
	.go:hover { background: var(--action-hover); }
	.go:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.err { min-height: 16px; margin: 0; font-size: 12px; color: var(--destructive); }
</style>
