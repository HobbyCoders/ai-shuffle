<script lang="ts">
	import { automationWebhookUrl, type TriggerType, type TriggerItem } from '$lib/api/automations';
	import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';
	import { RowGroup, SettingRow, SelectField, Segmented } from '$lib/components/layout/view';
 import TextField from '$lib/components/settings/rows/TextField.svelte';
	import {
		Clock,
		Timer,
		Radio,
		Zap,
		Webhook,
		Plus,
		Trash2,
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
	// Per trigger: whether a preset's raw expression is shown under Advanced.
	let advancedOpen: Record<number, boolean> = $state({});
	const uid = $props.id();

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
			case 'connector_trigger': return getTriggersForConnector(trigger.config.connector_id || '').find(t => t.id === trigger.config.trigger_id)?.name || (trigger.config.trigger_id ? 'Saved trigger' : 'Not configured');
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
			case 'cron': return 'Schedule';
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
{#each triggers as trigger, trigIdx}
 <section class="st-card trigger-card" aria-label={`Trigger ${trigIdx + 1}`}>
  <div class="trigger-header">
   <h4>{getTriggerSummary(trigger)}</h4>
   <div class="trigger-actions">
    <button class="st-btn icon trigger-toggle ghost" aria-label={`${expandedTriggers.has(trigIdx) ? 'Collapse' : 'Expand'} trigger`} aria-expanded={expandedTriggers.has(trigIdx)} onclick={() => onToggleTriggerExpand(trigIdx)}>{#if expandedTriggers.has(trigIdx)}<ChevronDown />{:else}<ChevronRight />{/if}</button>
    {#if triggers.length > 1}<button class="st-btn icon danger" aria-label="Remove trigger" onclick={() => removeTrigger(trigIdx)}><Trash2 size={14} /></button>{/if}
   </div>
  </div>
  {#if expandedTriggers.has(trigIdx)}
   <!-- Direct card children reuse the shared SettingRow dividers. -->
    <SettingRow label="Trigger type" stackOnPhone>
     <Segmented label="Trigger type" value={trigger.type} options={[{value:'cron',label:'Schedule'},{value:'interval',label:'Interval'},{value:'connector_event',label:'Event'},{value:'connector_trigger',label:'Connector'},{value:'webhook',label:'Webhook'}]} onchange={(value) => updateTriggerType(trigIdx,value)} />
    </SettingRow>
    {#if trigger.type === 'cron'}
     <!-- The type reads "Schedule" (stored as cron); presets or a custom timing sit under it. -->
     <SettingRow label="Timing" stackOnPhone><Segmented label="Timing" value={cronMode} options={[{value:'preset',label:'Preset'},{value:'custom',label:'Custom'}]} onchange={(value) => cronMode = value} /></SettingRow>
     {#if cronMode === 'preset'}
      <SettingRow label="Preset" hint="Timezone: {timezone}"><SelectField value={trigger.config.expression || '0 9 * * *'} options={Object.entries(CRON_PRESETS).map(([value,label]) => ({value,label}))} onchange={(value) => {updateTriggerConfig(trigIdx,{...trigger.config,expression:value});syncCronFieldsFromExpression(value);}} /></SettingRow>
      <!-- A preset is described by its name; its raw expression waits behind Advanced. -->
      <SettingRow label="Advanced" hint="The cron expression behind this preset.">
       <button type="button" class="st-btn advanced-toggle" aria-expanded={!!advancedOpen[trigIdx]} aria-controls={advancedOpen[trigIdx] ? `cron-expression-${uid}-${trigIdx}` : undefined} onclick={() => advancedOpen[trigIdx] = !advancedOpen[trigIdx]}>{advancedOpen[trigIdx] ? 'Hide expression' : 'Show expression'}<ChevronDown /></button>
      </SettingRow>
     {:else}
<SettingRow label="Minute"><SelectField value={cronMinute} options={[{value:'*',label:'Every minute'},...['5','10','15','30'].map(n => ({value:`*/${n}`,label:`Every ${n} min`})),...['0','15','30','45'].map(n => ({value:n,label:`:${n.padStart(2,'0')}`}))]} onchange={(value) => {cronMinute = value; updateTriggerConfig(trigIdx,{...trigger.config,expression:buildCronFromFields()});}} /></SettingRow>
<SettingRow label="Hour"><SelectField value={cronHour} options={[{value:'*',label:'Every hour'},...Array.from({length:24},(_,h) => ({value:String(h),label:formatHour(h)}))]} onchange={(value) => {cronHour = value; updateTriggerConfig(trigIdx,{...trigger.config,expression:buildCronFromFields()});}} /></SettingRow>
<SettingRow label="Day of month"><SelectField value={cronDayOfMonth} options={[{value:'*',label:'Every day'},...Array.from({length:31},(_,i) => ({value:String(i+1),label:String(i+1)}))]} onchange={(value) => {cronDayOfMonth = value; updateTriggerConfig(trigIdx,{...trigger.config,expression:buildCronFromFields()});}} /></SettingRow>
<SettingRow label="Month"><SelectField value={cronMonth} options={[{value:'*',label:'Every month'},...['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'].map((m,i) => ({value:String(i+1),label:m}))]} onchange={(value) => {cronMonth = value; updateTriggerConfig(trigIdx,{...trigger.config,expression:buildCronFromFields()});}} /></SettingRow>
<SettingRow label="Day of Week"><SelectField value={cronDayOfWeek} options={[{value:'*',label:'Every day'},{value:'1-5',label:'Weekdays (Mon-Fri)'},{value:'0,6',label:'Weekends'},...['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'].map((label,i) => ({value:String(i),label}))]} onchange={(value) => {cronDayOfWeek = value; updateTriggerConfig(trigIdx,{...trigger.config,expression:buildCronFromFields()});}} /></SettingRow>

     {/if}
     <!-- One Expression row for both modes, so typing a preset or a custom
          expression into it never remounts the field under the cursor. Typing
          marks the expression open first: a custom one edited into a preset
          (0 19 → 0 9) flips the mode to Preset, and the field must stay. -->
     {#if cronMode === 'custom' || advancedOpen[trigIdx]}
     <SettingRow label="Expression" hint={cronMode === 'custom' ? `Timezone: ${timezone}` : undefined}>
      <div class="cron-expression" id="cron-expression-{uid}-{trigIdx}">
       <TextField label="Cron expression" mono value={trigger.config.expression || ''} placeholder="0 9 * * *" oninput={(value) => {advancedOpen[trigIdx] = true;updateTriggerConfig(trigIdx,{...trigger.config,expression:value});syncCronFieldsFromExpression(value);}} />
       {#if cronMode === 'custom'}<p class="cron-summary-text st-note">{describeCron(trigger.config.expression || '')}</p>{/if}
      </div>
     </SettingRow>
     {/if}
    {:else if trigger.type === 'interval'}
     <SettingRow label="Interval (seconds)" hint="Minimum 60 seconds"><TextField value={String(trigger.config.seconds || '')} placeholder="3600" inputmode="numeric" oninput={(value) => updateTriggerConfig(trigIdx,{...trigger.config,seconds:parseInt(value) || 0})} /></SettingRow>
    {:else if trigger.type === 'connector_event'}
     <SettingRow label="Connector"><SelectField value={trigger.config.connector_id || ''} options={[{value:'',label:'Select a connector...'},...connectors.filter(c => c.is_active || c.status === 'connected').map(c => ({value:c.id,label:`${c.name} (${c.type})`}))]} onchange={(value) => updateTriggerConfig(trigIdx,{...trigger.config,connector_id:value})} /></SettingRow>
     <SettingRow label="Event Type"><SelectField value={trigger.config.event_type || ''} options={[{value:'',label:'Select an event...'},...['session.complete','session.error','session.started','permission.requested','input.needed'].map((value,i) => ({value,label:['Chat complete','Chat error','Chat started','Permission requested','Input needed'][i]}))]} onchange={(value) => updateTriggerConfig(trigIdx,{...trigger.config,event_type:value})} /></SettingRow>
    {:else if trigger.type === 'connector_trigger'}
     <SettingRow label="Connector"><SelectField value={trigger.config.connector_id || ''} options={[{value:'',label:'Select a connector...'},...connectorsWithTriggers.map(c => ({value:c.id,label:`${c.name} (${c.type})`}))]} onchange={(value) => updateTriggerConfig(trigIdx,{...trigger.config,connector_id:value,trigger_id:''})} /></SettingRow>
     {#if trigger.config.connector_id}
      <SettingRow label="Trigger"><SelectField value={trigger.config.trigger_id || ''} options={[{value:'',label:'Select a trigger...'},...getTriggersForConnector(trigger.config.connector_id).map(t => ({value:t.id,label:t.name}))]} onchange={(value) => updateTriggerConfig(trigIdx,{...trigger.config,trigger_id:value})} /></SettingRow>
     {/if}
    {:else if trigger.type === 'webhook'}
     {#if currentView === 'edit' && selectedAutomationId}
      <SettingRow label="Webhook URL">{#snippet expanded()}<input class="webhook-value" aria-label="Webhook URL" value={automationWebhookUrl(selectedAutomationId)} readonly />{/snippet}</SettingRow>
      {#if trigger.config.secret}<SettingRow label="Secret">{#snippet expanded()}<input class="webhook-value" aria-label="Secret" value={trigger.config.secret} readonly />{/snippet}</SettingRow>{/if}
     {:else}<p class="st-note p-4">Webhook URL and secret will be generated after creation.</p>{/if}
    {/if}
  {/if}
 </section>
{/each}
<button class="st-btn" onclick={addTrigger}><Plus size={14} />Add trigger</button>
<style>
 .trigger-card { margin-bottom: 12px; overflow: hidden; }
 .trigger-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 16px; }
 .trigger-header:has(+ :global(.st-row)) { border-bottom: 1px solid var(--st-border); }
 .trigger-header h4 { margin: 0; font-size: 14px; font-weight: 600; }
 .trigger-actions { display: flex; align-items: center; gap: 8px; }
 .trigger-card > p { margin: 0; padding: 12px 16px; }
 .trigger-card :global(.st-note) { margin: 0; }
 .trigger-actions .st-btn.trigger-toggle { width: 28px; height: 28px; min-width: 28px; }
 .advanced-toggle :global(svg.lucide) { width: 14px; height: 14px; color: var(--st-mfg); transition: transform 0.15s; }
 .advanced-toggle[aria-expanded='true'] :global(svg.lucide) { transform: rotate(180deg); }
 @media (prefers-reduced-motion: reduce) { .advanced-toggle :global(svg.lucide) { transition: none; } }
 .cron-expression { display: grid; gap: 6px; min-width: 0; }
 .cron-summary-text { font-size: 13px; color: var(--st-mfg); text-align: right; }
 .webhook-value { width: 100%; min-width: 0; background: var(--st-field); color: var(--st-fg); border: 1px solid var(--st-input); border-radius: var(--st-radius-md); padding: 8px 12px; font: inherit; font-size: 13px; }
 @media (max-width:600px) { .trigger-actions .st-btn.trigger-toggle { width:44px; height:44px; min-width:44px; } .cron-expression { width:100%; } .cron-summary-text { text-align:left; } .webhook-value { font-size:16px; } }
</style>
