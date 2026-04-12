<script lang="ts">
	import type { TriggerType, TriggerItem } from '$lib/api/automations';
	import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';
	import { slide } from 'svelte/transition';
	import {
		Clock,
		Timer,
		Radio,
		Zap,
		Webhook,
		Plus,
		X,
		ChevronDown,
		ChevronRight
	} from 'lucide-svelte';

	interface Props {
		triggers: TriggerItem[];
		expandedTriggers: Set<number>;
		connectors: Connector[];
		connectorTypes: ConnectorTypeInfo[];
		connectorsWithTriggers: Connector[];
		currentView: 'create' | 'edit';
		selectedAutomationId: string | null;
		timezone: string;
		onUpdateTriggers: (triggers: TriggerItem[]) => void;
		onToggleTriggerExpand: (idx: number) => void;
	}

	let {
		triggers,
		expandedTriggers,
		connectors,
		connectorTypes,
		connectorsWithTriggers,
		currentView,
		selectedAutomationId,
		timezone,
		onUpdateTriggers,
		onToggleTriggerExpand
	}: Props = $props();

	// Cron builder state
	let cronMode: 'preset' | 'custom' = $state('preset');
	let cronPreset = $state('0 9 * * *');
	let cronMinute = $state('0');
	let cronHour = $state('9');
	let cronDayOfMonth = $state('*');
	let cronMonth = $state('*');
	let cronDayOfWeek = $state('*');

	// Sync cron fields from the first cron trigger on mount / when triggers change
	$effect(() => {
		const firstCron = triggers.find(t => t.type === 'cron');
		if (firstCron?.config?.expression) {
			syncCronFieldsFromExpression(firstCron.config.expression);
		}
	});

	const CRON_PRESETS: Record<string, string> = {
		'0 * * * *': 'Every hour',
		'0 9 * * *': 'Every day at 9 AM',
		'0 9 * * 1-5': 'Every weekday at 9 AM',
		'0 9 * * 1': 'Every Monday at 9 AM',
		'0 0 1 * *': 'Every month on the 1st',
		'*/15 * * * *': 'Every 15 minutes',
		'*/30 * * * *': 'Every 30 minutes'
	};

	function addTrigger() {
		onUpdateTriggers([...triggers, { type: 'cron', config: { expression: '0 9 * * *' } }]);
	}

	function removeTrigger(index: number) {
		if (triggers.length <= 1) return;
		onUpdateTriggers(triggers.filter((_, i) => i !== index));
	}

	function updateTriggerType(index: number, type: TriggerType) {
		const defaultConfig: Record<TriggerType, Record<string, any>> = {
			cron: { expression: '0 9 * * *' },
			interval: { seconds: 3600 },
			connector_event: {},
			connector_trigger: {},
			webhook: {}
		};
		const updated = [...triggers];
		updated[index] = { type, config: defaultConfig[type] };
		onUpdateTriggers(updated);
	}

	function updateTriggerConfig(index: number, config: Record<string, any>) {
		const updated = [...triggers];
		updated[index] = { ...updated[index], config };
		onUpdateTriggers(updated);
	}

	function getTriggerSummary(trigger: TriggerItem): string {
		switch (trigger.type) {
			case 'cron': return trigger.config.expression ? describeCron(trigger.config.expression) : 'Not configured';
			case 'interval': return trigger.config.seconds ? `Every ${trigger.config.seconds}s` : 'Not configured';
			case 'connector_event': return trigger.config.event_type || 'Not configured';
			case 'connector_trigger': return trigger.config.trigger_id || 'Not configured';
			case 'webhook': return 'Webhook endpoint';
			default: return 'Not configured';
		}
	}

	function getTriggerIcon(type: TriggerType) {
		switch (type) {
			case 'cron': return Clock;
			case 'interval': return Timer;
			case 'connector_event': return Radio;
			case 'connector_trigger': return Zap;
			case 'webhook': return Webhook;
			default: return Clock;
		}
	}

	function triggerTypeLabel(type: TriggerType): string {
		switch (type) {
			case 'cron': return 'Cron';
			case 'interval': return 'Interval';
			case 'connector_event': return 'Event';
			case 'connector_trigger': return 'Connector';
			case 'webhook': return 'Webhook';
		}
	}

	function getTriggersForConnector(connectorId: string) {
		const connector = connectors.find(c => c.id === connectorId);
		if (!connector) return [];
		const typeInfo = connectorTypes.find(t => t.type === connector.type);
		return typeInfo?.triggers || [];
	}

	// Cron helpers
	function parseCronExpression(expr: string): { minute: string; hour: string; dayOfMonth: string; month: string; dayOfWeek: string } | null {
		if (!expr) return null;
		const parts = expr.trim().split(/\s+/);
		if (parts.length !== 5) return null;
		return { minute: parts[0], hour: parts[1], dayOfMonth: parts[2], month: parts[3], dayOfWeek: parts[4] };
	}

	function buildCronFromFields(): string {
		return `${cronMinute} ${cronHour} ${cronDayOfMonth} ${cronMonth} ${cronDayOfWeek}`;
	}

	function syncCronFieldsFromExpression(expr: string) {
		const parsed = parseCronExpression(expr);
		if (parsed) {
			cronMinute = parsed.minute;
			cronHour = parsed.hour;
			cronDayOfMonth = parsed.dayOfMonth;
			cronMonth = parsed.month;
			cronDayOfWeek = parsed.dayOfWeek;
		}
		if (expr && CRON_PRESETS[expr.trim()]) {
			cronMode = 'preset';
			cronPreset = expr.trim();
		} else {
			cronMode = 'custom';
		}
	}

	function describeCron(expr: string): string {
		if (!expr) return '';
		const parts = expr.trim().split(/\s+/);
		if (parts.length !== 5) return 'Invalid expression';
		const [min, hour, dom, month, dow] = parts;

		if (expr.trim() === '* * * * *') return 'Every minute';
		if (CRON_PRESETS[expr.trim()]) return CRON_PRESETS[expr.trim()];

		let desc = '';
		if (min !== '*' && hour !== '*') {
			const h = parseInt(hour);
			const m = parseInt(min);
			const ampm = h >= 12 ? 'PM' : 'AM';
			const h12 = h === 0 ? 12 : h > 12 ? h - 12 : h;
			desc = `At ${h12}:${String(m).padStart(2, '0')} ${ampm}`;
		} else if (min === '0' && hour === '*') {
			desc = 'Every hour';
		} else if (min.startsWith('*/')) {
			desc = `Every ${min.slice(2)} minutes`;
		} else {
			desc = `At minute ${min}`;
		}

		if (dow === '1-5') {
			desc += ', weekdays';
		} else if (dow === '0,6') {
			desc += ', weekends';
		} else if (dow !== '*') {
			const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
			const idx = parseInt(dow);
			desc += `, on ${isNaN(idx) ? dow : (dayNames[idx] || dow)}`;
		}

		if (dom !== '*') {
			desc += `, on day ${dom}`;
		}

		if (month !== '*') {
			const monthNames = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
			const idx = parseInt(month);
			desc += ` of ${isNaN(idx) ? month : (monthNames[idx] || month)}`;
		}

		return desc;
	}

	function formatHour(h: number): string {
		if (h === 0) return '12 AM';
		if (h < 12) return `${h} AM`;
		if (h === 12) return '12 PM';
		return `${h - 12} PM`;
	}
</script>

<p class="af-hint">Any trigger firing will run this automation. At least one required.</p>

{#each triggers as trigger, trigIdx}
	<div class="af-item-card" transition:slide={{ duration: 150 }}>
		<div class="af-item-header" role="button" tabindex="0" onclick={() => onToggleTriggerExpand(trigIdx)} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') onToggleTriggerExpand(trigIdx); }}>
			<div class="af-item-header-left">
				{#if expandedTriggers.has(trigIdx)}
					<ChevronDown size={14} />
				{:else}
					<ChevronRight size={14} />
				{/if}
				<span class="af-item-type-badge">
					{#if getTriggerIcon(trigger.type)}
						{@const TriggerIcon = getTriggerIcon(trigger.type)}
						<TriggerIcon size={12} />
					{/if}
					{triggerTypeLabel(trigger.type)}
				</span>
				{#if !expandedTriggers.has(trigIdx)}
					<span class="af-item-summary">{getTriggerSummary(trigger)}</span>
				{/if}
			</div>
			<div class="af-item-header-actions">
				{#if triggers.length > 1}
					<button
						class="card-action-btn card-action-btn--destructive"
						onclick={(e) => { e.stopPropagation(); removeTrigger(trigIdx); }}
					>
						<X size={12} />
					</button>
				{/if}
			</div>
		</div>

		{#if expandedTriggers.has(trigIdx)}
			<div class="af-item-body" transition:slide={{ duration: 150 }}>
				<!-- Type selector -->
				<div class="auto-trigger-selector">
					<button class="auto-trigger-btn" class:active={trigger.type === 'cron'}
						onclick={() => updateTriggerType(trigIdx, 'cron')}>
						<Clock size={14} /> Cron
					</button>
					<button class="auto-trigger-btn" class:active={trigger.type === 'interval'}
						onclick={() => updateTriggerType(trigIdx, 'interval')}>
						<Timer size={14} /> Interval
					</button>
					<button class="auto-trigger-btn" class:active={trigger.type === 'connector_event'}
						onclick={() => updateTriggerType(trigIdx, 'connector_event')}>
						<Radio size={14} /> Event
					</button>
					<button class="auto-trigger-btn" class:active={trigger.type === 'connector_trigger'}
						onclick={() => updateTriggerType(trigIdx, 'connector_trigger')}>
						<Zap size={14} /> Connector
					</button>
					<button class="auto-trigger-btn" class:active={trigger.type === 'webhook'}
						onclick={() => updateTriggerType(trigIdx, 'webhook')}>
						<Webhook size={14} /> Webhook
					</button>
				</div>

				<!-- Type-specific config -->
				{#if trigger.type === 'cron'}
					<div class="af-trigger-config">
						<div class="cron-mode-selector">
							<button class="auto-trigger-btn" class:active={cronMode === 'preset'}
								onclick={() => cronMode = 'preset'}>Preset</button>
							<button class="auto-trigger-btn" class:active={cronMode === 'custom'}
								onclick={() => cronMode = 'custom'}>Custom</button>
						</div>
						{#if cronMode === 'preset'}
							<select class="af-input"
								value={trigger.config.expression || '0 9 * * *'}
								onchange={(e) => {
									const val = e.currentTarget.value;
									updateTriggerConfig(trigIdx, { ...trigger.config, expression: val });
									syncCronFieldsFromExpression(val);
								}}>
								<option value="*/15 * * * *">Every 15 minutes</option>
								<option value="*/30 * * * *">Every 30 minutes</option>
								<option value="0 * * * *">Every hour</option>
								<option value="0 9 * * *">Every day at 9 AM</option>
								<option value="0 9 * * 1-5">Every weekday at 9 AM</option>
								<option value="0 9 * * 1">Every Monday at 9 AM</option>
								<option value="0 0 1 * *">Every month on the 1st</option>
							</select>
						{:else}
							<div class="cron-builder-grid">
								<div class="cron-field">
									<label class="af-label" for="auto-cron-minute-{trigIdx}">Minute</label>
									<select class="af-input" id="auto-cron-minute-{trigIdx}" bind:value={cronMinute}
										onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
										<option value="*">Every minute</option>
										<option value="*/5">Every 5 min</option>
										<option value="*/10">Every 10 min</option>
										<option value="*/15">Every 15 min</option>
										<option value="*/30">Every 30 min</option>
										<option value="0">:00</option>
										<option value="15">:15</option>
										<option value="30">:30</option>
										<option value="45">:45</option>
									</select>
								</div>
								<div class="cron-field">
									<label class="af-label" for="auto-cron-hour-{trigIdx}">Hour</label>
									<select class="af-input" id="auto-cron-hour-{trigIdx}" bind:value={cronHour}
										onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
										<option value="*">Every hour</option>
										{#each Array.from({length: 24}, (_, i) => i) as h}
											<option value={String(h)}>{formatHour(h)}</option>
										{/each}
									</select>
								</div>
								<div class="cron-field">
									<label class="af-label" for="auto-cron-day-{trigIdx}">Day</label>
									<select class="af-input" id="auto-cron-day-{trigIdx}" bind:value={cronDayOfMonth}
										onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
										<option value="*">Every day</option>
										{#each Array.from({length: 31}, (_, i) => i + 1) as d}
											<option value={String(d)}>{d}</option>
										{/each}
									</select>
								</div>
								<div class="cron-field">
									<label class="af-label" for="auto-cron-month-{trigIdx}">Month</label>
									<select class="af-input" id="auto-cron-month-{trigIdx}" bind:value={cronMonth}
										onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
										<option value="*">Every month</option>
										{#each ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'] as m, mi}
											<option value={String(mi+1)}>{m}</option>
										{/each}
									</select>
								</div>
								<div class="cron-field" style="grid-column: 1 / -1">
									<label class="af-label" for="auto-cron-dow-{trigIdx}">Day of Week</label>
									<select class="af-input" id="auto-cron-dow-{trigIdx}" bind:value={cronDayOfWeek}
										onchange={() => { const expr = buildCronFromFields(); updateTriggerConfig(trigIdx, { ...trigger.config, expression: expr }); }}>
										<option value="*">Every day</option>
										<option value="1-5">Weekdays (Mon-Fri)</option>
										<option value="0,6">Weekends</option>
										<option value="1">Monday</option>
										<option value="2">Tuesday</option>
										<option value="3">Wednesday</option>
										<option value="4">Thursday</option>
										<option value="5">Friday</option>
										<option value="6">Saturday</option>
										<option value="0">Sunday</option>
									</select>
								</div>
							</div>
						{/if}
						{#if trigger.config.expression}
							<div class="cron-summary">
								<span class="cron-summary-text">{describeCron(trigger.config.expression)}</span>
								<code class="cron-summary-expr">{trigger.config.expression}</code>
							</div>
						{/if}
						<details class="cron-raw-details">
							<summary class="af-hint" style="cursor: pointer; user-select: none;">Edit raw expression</summary>
							<input type="text" class="af-input af-input--mono" style="margin-top: 6px"
								value={trigger.config.expression || ''}
								oninput={(e) => { updateTriggerConfig(trigIdx, { ...trigger.config, expression: e.currentTarget.value }); syncCronFieldsFromExpression(e.currentTarget.value); }}
								placeholder="0 9 * * *" />
						</details>
						<p class="af-hint" style="margin-top: 8px">Timezone: {timezone}</p>
					</div>
				{:else if trigger.type === 'interval'}
					<div class="af-trigger-config">
						<label class="af-label" for="auto-interval-{trigIdx}">Interval (seconds)</label>
						<input type="number" class="af-input" id="auto-interval-{trigIdx}" min="60"
							value={trigger.config.seconds || ''}
							oninput={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, seconds: parseInt(e.currentTarget.value) || 0 })}
							placeholder="3600" />
						<p class="af-hint">Minimum 60 seconds</p>
					</div>
				{:else if trigger.type === 'connector_event'}
					<div class="af-trigger-config">
						<label class="af-label" for="auto-evt-connector-{trigIdx}">Connector</label>
						<select class="af-input" id="auto-evt-connector-{trigIdx}"
							value={trigger.config.connector_id || ''}
							onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, connector_id: e.currentTarget.value })}>
							<option value="">Select a connector...</option>
							{#each connectors.filter(c => c.is_active || c.status === 'connected') as c}
								<option value={c.id}>{c.name} ({c.type})</option>
							{/each}
						</select>
						<label class="af-label" for="auto-evt-type-{trigIdx}" style="margin-top: 12px">Event Type</label>
						<select class="af-input" id="auto-evt-type-{trigIdx}"
							value={trigger.config.event_type || ''}
							onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, event_type: e.currentTarget.value })}>
							<option value="">Select an event...</option>
							<option value="session.complete">Session complete</option>
							<option value="session.error">Session error</option>
							<option value="session.started">Session started</option>
							<option value="permission.requested">Permission requested</option>
							<option value="input.needed">Input needed</option>
						</select>
					</div>
				{:else if trigger.type === 'connector_trigger'}
					<div class="af-trigger-config">
						<label class="af-label" for="auto-trig-connector-{trigIdx}">Connector</label>
						<select class="af-input" id="auto-trig-connector-{trigIdx}"
							value={trigger.config.connector_id || ''}
							onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, connector_id: e.currentTarget.value, trigger_id: '' })}>
							<option value="">Select a connector...</option>
							{#each connectorsWithTriggers as c}
								<option value={c.id}>{c.name} ({c.type})</option>
							{/each}
						</select>
						{#if trigger.config.connector_id}
							<label class="af-label" for="auto-trig-id-{trigIdx}" style="margin-top: 12px">Trigger</label>
							<select class="af-input" id="auto-trig-id-{trigIdx}"
								value={trigger.config.trigger_id || ''}
								onchange={(e) => updateTriggerConfig(trigIdx, { ...trigger.config, trigger_id: e.currentTarget.value })}>
								<option value="">Select a trigger...</option>
								{#each getTriggersForConnector(trigger.config.connector_id) as trig}
									<option value={trig.id}>{trig.name}</option>
								{/each}
							</select>
						{/if}
					</div>
				{:else if trigger.type === 'webhook'}
					<div class="af-trigger-config">
						{#if currentView === 'edit' && selectedAutomationId}
							<label class="af-label" for="auto-webhook-url-{trigIdx}">Webhook URL</label>
							<input type="text" class="af-input af-input--mono" id="auto-webhook-url-{trigIdx}"
								value={trigger.config.webhook_url || 'Generated after creation'} readonly />
							{#if trigger.config.secret}
								<label class="af-label" for="auto-webhook-secret-{trigIdx}" style="margin-top: 12px">Secret</label>
								<input type="text" class="af-input af-input--mono" id="auto-webhook-secret-{trigIdx}"
									value={trigger.config.secret} readonly />
							{/if}
						{:else}
							<p class="af-hint">Webhook URL and secret will be generated after creation.</p>
						{/if}
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/each}

<button class="af-add-btn" onclick={addTrigger}>
	<Plus size={14} />
	Add Trigger
</button>

<style>
	/* Item Cards */
	.af-item-card {
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		transition: border-color 200ms;
	}

	.af-item-card:hover {
		border-color: var(--border-emphasis, var(--border));
	}

	.af-item-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
		padding: 10px 14px;
		background: none;
		border: none;
		cursor: pointer;
		color: inherit;
		text-align: left;
		transition: background 150ms;
	}

	.af-item-header:hover {
		background: color-mix(in oklch, var(--foreground) 3%, transparent);
	}

	.af-item-header-left {
		display: flex;
		align-items: center;
		gap: 8px;
		min-width: 0;
		flex: 1;
	}

	.af-item-header-left :global(svg) {
		flex-shrink: 0;
		color: var(--text-secondary, var(--muted-foreground));
	}

	.af-item-header-actions {
		display: flex;
		gap: 4px;
		flex-shrink: 0;
	}

	.af-item-type-badge {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 2px 10px;
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 600;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
		border-radius: 6px;
		white-space: nowrap;
	}

	.af-item-summary {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary, var(--muted-foreground));
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}

	.af-item-body {
		padding: 12px 14px 14px;
		border-top: 1px solid var(--border-subtle, var(--border));
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.af-trigger-config {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.af-hint {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-tertiary, var(--muted-foreground));
		margin: 0;
	}

	.af-label {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		color: var(--text-secondary, var(--muted-foreground));
	}

	.af-input {
		width: 100%;
		padding: 10px 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary, var(--foreground));
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		transition: border-color 200ms, box-shadow 200ms;
		outline: none;
	}

	.af-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.af-input::placeholder {
		color: var(--text-tertiary, var(--muted-foreground));
	}

	.af-input--mono {
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
		font-size: var(--text-sm, 0.75rem);
	}

	/* Trigger selector */
	.auto-trigger-selector {
		display: flex;
		gap: 6px;
		flex-wrap: wrap;
	}

	.auto-trigger-btn {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 14px;
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		border-radius: var(--radius-md, calc(var(--radius, 0.75rem) * 0.833));
		background: var(--surface-1);
		border: 1px solid var(--border-subtle, var(--border));
		color: var(--text-secondary, var(--muted-foreground));
		cursor: pointer;
		transition: all 150ms ease;
	}

	.auto-trigger-btn:hover {
		border-color: var(--accent);
		color: var(--text-primary, var(--foreground));
	}

	.auto-trigger-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.auto-trigger-btn.active {
		background: var(--selection-bg-emphasis);
		color: var(--selection-text);
		border-color: var(--selection-ring);
		box-shadow: var(--selection-shadow-subtle, var(--selection-shadow));
	}

	/* Cron Builder */
	.cron-mode-selector {
		display: flex;
		gap: 6px;
		margin-bottom: 10px;
	}

	.cron-builder-grid {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 10px;
	}

	.cron-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.cron-summary {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-top: 10px;
		padding: 8px 12px;
		background: color-mix(in oklch, var(--primary) 5%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 15%, transparent);
		border-radius: var(--radius-sm, calc(var(--radius, 0.75rem) * 0.5));
		font-size: var(--text-sm, 0.75rem);
	}

	.cron-summary-text {
		color: var(--text-primary, var(--foreground));
		font-weight: 500;
	}

	.cron-summary-expr {
		font-family: var(--card-font-mono, monospace);
		color: var(--text-secondary, var(--muted-foreground));
		font-size: var(--text-xs, 0.6875rem);
		padding: 2px 6px;
		background: var(--muted);
		border-radius: 4px;
		flex-shrink: 0;
	}

	.cron-raw-details {
		margin-top: 8px;
	}

	.cron-raw-details summary {
		font-size: var(--text-sm, 0.75rem);
	}

	/* Add Button */
	.af-add-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 10px;
		background: none;
		border: 1px dashed var(--border-subtle, var(--border));
		border-radius: var(--radius-md, 10px);
		color: var(--text-secondary, var(--muted-foreground));
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		cursor: pointer;
		transition: all 200ms;
	}

	.af-add-btn:hover {
		border-color: var(--primary);
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 5%, transparent);
	}

	@media (max-width: 639px) {
		.cron-builder-grid {
			grid-template-columns: 1fr;
		}

		.cron-summary {
			flex-direction: column;
			align-items: flex-start;
			gap: 4px;
		}
	}
</style>
