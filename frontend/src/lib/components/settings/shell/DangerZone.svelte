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
	 * An action that throws keeps the owner here and shows why under its row,
	 * until the next confirm opens or Settings is left (it stays mounted).
	 */
	import { getContext, type Snippet } from 'svelte';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '../../layout/feature-panel-context';
	import { getErrorMessage } from '$lib/api/client';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';

	interface Props {
		items?: DangerAction[];
		title?: string;
		children?: Snippet;
	}

	let { items = [], title = 'Danger zone', children }: Props = $props();

	/** The last failure per action, keyed like the rows. */
	let errors = $state<Record<string, string>>({});
	const keyOf = (item: DangerAction) => item.id ?? item.label;

	const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	$effect(() => {
		if (feature && !feature.active) errors = {};
	});

	async function run(item: DangerAction) {
		const key = keyOf(item);
		errors = { ...errors, [key]: '' };
		try {
			await item.onConfirm();
		} catch (e) {
			errors = { ...errors, [key]: getErrorMessage(e, `${item.button} failed`) };
		}
	}
</script>

{#if items.length || children}
	<RowGroup {title} tone="danger">
		{#each items as item (keyOf(item))}
			<SettingRow id={item.id} label={item.label} hint={item.hint}>
				<InlineConfirm
					label={item.button}
					question={item.question}
					onConfirm={() => run(item)}
					onArm={() => (errors = {})}
					disabled={item.disabled}
				/>
			</SettingRow>
			{#if errors[keyOf(item)]}<p class="dz-err" role="alert">{errors[keyOf(item)]}</p>{/if}
		{/each}
		{@render children?.()}
	</RowGroup>
{/if}

<style>
	.dz-err {
		margin: 4px 14px 10px;
		font-size: 12.5px;
		color: var(--st-destructive);
	}
</style>
