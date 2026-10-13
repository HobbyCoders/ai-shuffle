<script lang="ts" module>
	export interface DangerAction {
		/** Registry id when search should reach it. */
		id?: string;
		label: string;
		hint?: string;
		/** Trigger and confirm button text, e.g. "Remove". */
		button: string;
		question: string;
		onConfirm: () => void | Promise<void>;
		disabled?: boolean;
	}
</script>

<script lang="ts">
	/**
	 * DangerZone: the last section of a detail. Each action is a row whose
	 * button arms an InlineConfirm. Pass `items`, or `children` for custom rows.
	 */
	import type { Snippet } from 'svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';

	interface Props {
		items?: DangerAction[];
		title?: string;
		children?: Snippet;
	}

	let { items = [], title = 'Danger zone', children }: Props = $props();
</script>

{#if items.length || children}
	<RowGroup {title} tone="danger">
		{#each items as item (item.id ?? item.label)}
			<SettingRow id={item.id} label={item.label} hint={item.hint}>
				<InlineConfirm
					label={item.button}
					question={item.question}
					onConfirm={item.onConfirm}
					disabled={item.disabled}
				/>
			</SettingRow>
		{/each}
		{@render children?.()}
	</RowGroup>
{/if}
