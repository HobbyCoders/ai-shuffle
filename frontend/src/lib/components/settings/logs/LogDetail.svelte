<script lang="ts">
	/** One log line: time, level and logger as facts, the message below. */
	import { ListFilter, ScrollText } from 'lucide-svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import CodeBlock from '../rows/CodeBlock.svelte';
	import { formatLogTime, levelName, levelTone, type LogEntry } from './logs-model.svelte';

	interface Props {
		entry: LogEntry;
		onSameLogger: (logger: string) => void;
	}

	let { entry, onSameLogger }: Props = $props();
</script>

<DetailPane
	name={levelName(entry.level)}
	icon={ScrollText}
	status={levelTone(entry.level)}
	statusText={entry.logger_name}
	badge="Log entry"
	category="logs"
	item={entry.id}
	facts={[
		{ label: 'Time', value: formatLogTime(entry.timestamp), mono: true },
		{ label: 'Level', value: entry.level },
		{ label: 'Logger', value: entry.logger_name, mono: true }
	]}
>
	<RowGroup title="Message" bare>
		<CodeBlock code={entry.message} label="Copy message" />
		<div class="acts">
			<button type="button" class="st-btn sm" onclick={() => onSameLogger(entry.logger_name)}>
				<ListFilter />Same logger
			</button>
		</div>
	</RowGroup>
</DetailPane>

<style>
	.acts {
		display: flex;
		gap: 8px;
		margin-top: 10px;
	}
</style>
