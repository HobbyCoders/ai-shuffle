<script lang="ts">
	import { createEventDispatcher } from 'svelte';

	export let startDate: string;
	export let endDate: string;

	const dispatch = createEventDispatcher<{
		change: { startDate: string; endDate: string };
	}>();

	type PresetRange = '7d' | '30d' | '90d' | 'custom';
	let selectedPreset: PresetRange = '30d';
	let showCustomPicker = false;

	// Format date for input
	function formatDateForInput(date: Date): string {
		return date.toISOString().split('T')[0];
	}

	// Get preset dates
	function getPresetDates(preset: PresetRange): { start: string; end: string } {
		const end = new Date();
		const start = new Date();

		switch (preset) {
			case '7d':
				start.setDate(end.getDate() - 7);
				break;
			case '30d':
				start.setDate(end.getDate() - 30);
				break;
			case '90d':
				start.setDate(end.getDate() - 90);
				break;
			case 'custom':
				return { start: startDate, end: endDate };
		}

		return {
			start: formatDateForInput(start),
			end: formatDateForInput(end)
		};
	}

	// Handle preset selection
	function selectPreset(preset: PresetRange) {
		selectedPreset = preset;
		if (preset === 'custom') {
			showCustomPicker = true;
		} else {
			showCustomPicker = false;
			const dates = getPresetDates(preset);
			startDate = dates.start;
			endDate = dates.end;
			dispatch('change', { startDate, endDate });
		}
	}

	// Handle custom date change
	function handleCustomDateChange() {
		dispatch('change', { startDate, endDate });
	}

	// Initialize with 30d preset on mount
	$: if (selectedPreset === '30d' && !startDate) {
		const dates = getPresetDates('30d');
		startDate = dates.start;
		endDate = dates.end;
	}
</script>

<div class="flex flex-wrap items-center gap-2">
	<div class="flex items-center gap-1 bg-card border border-border rounded-lg p-1">
		<button
			class="px-3 py-1.5 text-sm rounded-md transition-all {selectedPreset === '7d' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			on:click={() => selectPreset('7d')}
		>
			7 Days
		</button>
		<button
			class="px-3 py-1.5 text-sm rounded-md transition-all {selectedPreset === '30d' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			on:click={() => selectPreset('30d')}
		>
			30 Days
		</button>
		<button
			class="px-3 py-1.5 text-sm rounded-md transition-all {selectedPreset === '90d' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			on:click={() => selectPreset('90d')}
		>
			90 Days
		</button>
		<button
			class="px-3 py-1.5 text-sm rounded-md transition-all {selectedPreset === 'custom' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground hover:bg-muted'}"
			on:click={() => selectPreset('custom')}
		>
			Custom
		</button>
	</div>

	{#if showCustomPicker}
		<div class="flex items-center gap-2">
			<input
				type="date"
				bind:value={startDate}
				on:change={handleCustomDateChange}
				class="px-3 py-1.5 text-sm bg-card border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
			/>
			<span class="text-muted-foreground">to</span>
			<input
				type="date"
				bind:value={endDate}
				on:change={handleCustomDateChange}
				class="px-3 py-1.5 text-sm bg-card border border-border rounded-lg text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
			/>
		</div>
	{/if}
</div>
