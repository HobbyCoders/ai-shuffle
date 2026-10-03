// Mounts the real automation create form. `?agent=legacy` gives the selected
// agent a model saved by an older version; otherwise it is a plain agent.
// verify-automation-form.mjs answers the API (the model registry and the
// Claude auth summary), so the real api client and components run unchanged.
import { mount } from 'svelte';
import '@fontsource-variable/inter';
import '../src/app.css';

const params = new URLSearchParams(location.search);
// The app stores import each other in a cycle; load them in the app's order
// (active-session pulls in app-data first) before the form.
await import('../src/lib/stores/active-session');
const [{ hydrateModelRegistry }, { default: AutomationForm }] = await Promise.all([
	import('../src/lib/constants/models'),
	import('../src/lib/components/deck/cards/automations/AutomationForm.svelte')
]);
await hydrateModelRegistry();

const agentConfig = params.get('agent') === 'legacy' ? { model: 'gpt-5.6', provider: 'openai-row' } : {};
const noop = () => {};
const props = $state({
	currentView: 'create' as const,
	selectedAutomationId: null,
	formError: '',
	formName: 'Morning digest',
	formDescription: '',
	formTriggers: [{ type: 'cron' as const, config: { expression: '0 9 * * *' } }],
	formPrompt: 'Summarize yesterday.',
	formProfileId: 'agent-1',
	formProjectId: '',
	formOutputType: 'session' as const,
	formOutputConfig: {},
	formMaxCostPerRun: '',
	formMaxCostPerDay: '',
	formMaxCostTotal: '',
	formMaxRunsPerDay: '',
	formMaxTurns: '1',
	formTimeoutSeconds: '300',
	formRequiresApproval: false,
	formModel: '',
	formActionSteps: [],
	profiles: [{ id: 'agent-1', name: 'Research agent', config: agentConfig }],
	projects: [],
	connectors: [],
	connectorTypes: [],
	connectorsWithTriggers: [],
	connectorsWithActions: [],
	expandedTriggers: new Set<number>(),
	expandedActions: new Set<number>(),
	collapsedSections: new Set<string>(['triggers', 'actions', 'budget']),
	timezone: 'UTC',
	onUpdateFormError: noop,
	onUpdateFormName: noop,
	onUpdateFormDescription: noop,
	onUpdateFormTriggers: noop,
	onUpdateFormPrompt: noop,
	onUpdateFormProfileId: noop,
	onUpdateFormProjectId: noop,
	onUpdateFormOutputType: noop,
	onUpdateFormOutputConfig: noop,
	onUpdateFormMaxCostPerRun: noop,
	onUpdateFormMaxCostPerDay: noop,
	onUpdateFormMaxCostTotal: noop,
	onUpdateFormMaxRunsPerDay: noop,
	onUpdateFormMaxTurns: noop,
	onUpdateFormTimeoutSeconds: noop,
	onUpdateFormRequiresApproval: noop,
	onUpdateFormModel: (value: string) => {
		props.formModel = value;
	},
	onUpdateFormActionSteps: noop,
	onToggleSection: noop,
	onToggleTriggerExpand: noop,
	onToggleActionExpand: noop
});
// eslint-disable-next-line @typescript-eslint/no-explicit-any
mount(AutomationForm, { target: document.getElementById('app')!, props: props as any });
