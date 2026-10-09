/**
 * Connections settings: list states, per-row actions (toggle / test / logs /
 * delete with automation-dependency guard), create and edit payloads, and the
 * 15 s status poll.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { Connector, ConnectorTypeInfo } from '$lib/api/connectors';

const conn = vi.hoisted(() => ({
	getConnectors: vi.fn(),
	getConnectionDependencies: vi.fn(),
	createConnector: vi.fn(),
	updateConnector: vi.fn(),
	deleteConnector: vi.fn(),
	testConnector: vi.fn(),
	activateConnector: vi.fn(),
	deactivateConnector: vi.fn(),
	getConnectorTypes: vi.fn(),
	getConnectorEvents: vi.fn(),
	getConnectorLogs: vi.fn(),
	getConnectorSenders: vi.fn(),
	discoverDiscord: vi.fn()
}));
vi.mock('$lib/api/connectors', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/connectors')>()),
	...conn
}));
const getApiUsers = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/apiUsers', () => ({ getApiUsers }));
const apiGet = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/client', () => ({ api: { get: apiGet } }));
const listInstalls = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/installs', () => ({ listInstalls }));
const notify = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/notifications', () => ({ notify }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/components/plugins/GoogleConnection.svelte', () => ({ default: () => {} }));
vi.mock('$lib/components/plugins/PlaidConnection.svelte', () => ({ default: () => {} }));
const usesSubscription = vi.hoisted(() => vi.fn(() => false));
vi.mock('$lib/constants/model-picker', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/constants/model-picker')>()),
	selectionUsesSubscription: usesSubscription
}));

import ConnectorManager from './ConnectorManager.svelte';

const connector = (over: Partial<Connector> = {}): Connector => ({
	id: 'c1', type: 'telegram', name: 'Family bot', api_user_id: 'u1', config: { bot_token: '****abcd' },
	events: ['message'], is_active: true, status: 'connected', status_message: null,
	last_connected_at: null, last_event_at: null, failure_count: 0, runner_id: null,
	created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', ...over
});
const telegram: ConnectorTypeInfo = {
	type: 'telegram', name: 'Telegram', description: 'Chat with your agent from Telegram',
	config_fields: [{ key: 'bot_token', label: 'Bot Token', type: 'password', required: true, placeholder: '123:ABC' }],
	triggers: [], actions: [], tools: []
} as ConnectorTypeInfo;
const webhook: ConnectorTypeInfo = {
	type: 'webhook', name: 'Webhook', description: 'Send events to a URL',
	config_fields: [{ key: 'url', label: 'URL', type: 'text', required: true, placeholder: 'https://' }],
	triggers: [], actions: [], tools: []
} as ConnectorTypeInfo;
const agents = [{ id: 'a1', name: 'Helper' }, { id: 'a2', name: 'Planner' }];

beforeEach(() => {
	vi.clearAllMocks();
	conn.getConnectors.mockResolvedValue([connector()]);
	conn.getConnectorTypes.mockResolvedValue([telegram, webhook]);
	conn.getConnectorEvents.mockResolvedValue(['message', 'error']);
	conn.getConnectorLogs.mockResolvedValue([]);
	conn.getConnectionDependencies.mockResolvedValue([]);
	conn.createConnector.mockResolvedValue(connector({ id: 'c2' }));
	conn.updateConnector.mockResolvedValue(connector());
	conn.deleteConnector.mockResolvedValue(undefined);
	conn.activateConnector.mockResolvedValue(connector());
	conn.deactivateConnector.mockResolvedValue(connector({ is_active: false }));
	conn.testConnector.mockResolvedValue({ success: true, response_time_ms: 42 });
	getApiUsers.mockResolvedValue([]);
	apiGet.mockResolvedValue(agents);
	listInstalls.mockResolvedValue({ self_instance_id: 'self', installs: [] });
	usesSubscription.mockImplementation(() => false);
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

// jsdom has no Web Animations; the views fade/slide. Finish every animation at once.
const proto = Element.prototype as unknown as { animate?: unknown };
const hadAnimate = 'animate' in proto;
beforeAll(() => {
	if (hadAnimate) return;
	proto.animate = () => {
		let done: (() => void) | null = null;
		return {
			cancel() {}, finish() {}, play() {}, pause() {}, currentTime: 0, effect: null,
			get onfinish() { return done; },
			set onfinish(fn: (() => void) | null) { done = fn; if (fn) queueMicrotask(() => { if (done === fn) fn(); }); }
		};
	};
});
afterAll(() => { if (!hadAnimate) delete proto.animate; });

async function loaded() {
	render(ConnectorManager);
	await screen.findByText('Family bot');
}

describe('ConnectorManager list', () => {
	it('shows the loading state, then the empty state', async () => {
		let resolve!: (v: Connector[]) => void;
		conn.getConnectors.mockReturnValue(new Promise((r) => (resolve = r)));
		render(ConnectorManager);
		expect(screen.getByText('Loading connectors...')).toBeTruthy();
		resolve([]);
		expect(await screen.findByText('No accounts connected')).toBeTruthy();
		expect(screen.queryByText('Loading connectors...')).toBeNull();
	});

	it('shows a load failure as a dismissible banner', async () => {
		conn.getConnectors.mockRejectedValue({ detail: 'database locked' });
		render(ConnectorManager);
		expect(await screen.findByText('database locked')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: /Dismiss/i }));
		expect(screen.queryByText('database locked')).toBeNull();
	});

	it('labels a connector owned by another install with that device', async () => {
		conn.getConnectors.mockResolvedValue([connector({ runner_id: 'peer-1234567890' }), connector({ id: 'c2', name: 'Local bot', runner_id: 'self' })]);
		listInstalls.mockResolvedValue({ self_instance_id: 'self', installs: [{ id: 'peer-1234567890', device_name: 'Laptop' }] });
		await loaded();
		expect(await screen.findByText('runs on Laptop')).toBeTruthy();
		expect(screen.getAllByText(/^runs on/)).toHaveLength(1);
	});

	it('toggles a connector off and reloads, and reports a failed toggle', async () => {
		await loaded();
		await fireEvent.click(screen.getByRole('switch', { name: 'Deactivate Family bot' }));
		await waitFor(() => expect(conn.deactivateConnector).toHaveBeenCalledWith('c1'));
		await waitFor(() => expect(conn.getConnectors).toHaveBeenCalledTimes(2));
		expect(conn.activateConnector).not.toHaveBeenCalled();

		conn.deactivateConnector.mockRejectedValue({ detail: 'runner offline' });
		await fireEvent.click(screen.getByRole('switch', { name: 'Deactivate Family bot' }));
		await waitFor(() => expect(notify).toHaveBeenCalledWith('runner offline', { severity: 'error' }));
	});

	it('reports test results through notifications', async () => {
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Test Family bot' }));
		await waitFor(() => expect(notify).toHaveBeenCalledWith('Connection successful (42ms)', { severity: 'success' }));
		conn.testConnector.mockResolvedValue({ success: false, error: 'Unauthorized token' });
		await fireEvent.click(screen.getByRole('button', { name: 'Test Family bot' }));
		await waitFor(() => expect(notify).toHaveBeenCalledWith('Unauthorized token', { severity: 'error' }));
	});

	it('expands the event log for one connector', async () => {
		conn.getConnectorLogs.mockResolvedValue([
			{ id: 'l1', event_type: 'message', direction: 'inbound', status: 'success', error: null, created_at: '2026-01-01T00:00:00Z' }
		]);
		await loaded();
		const toggle = screen.getByRole('button', { name: 'Event logs for Family bot' });
		await fireEvent.click(toggle);
		expect(await screen.findByText('Success')).toBeTruthy();
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		expect(conn.getConnectorLogs).toHaveBeenCalledWith('c1');
	});

	it('blocks deletion while automations still use the connection', async () => {
		conn.getConnectionDependencies.mockResolvedValue([{ id: 'x', name: 'Morning digest', is_enabled: false, uses: ['trigger', 'action'] }]);
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete Family bot' }));
		expect(await screen.findByText('Morning digest — trigger, action (paused)')).toBeTruthy();
		const confirm = screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement;
		expect(confirm.disabled).toBe(true);
		await fireEvent.click(confirm);
		expect(conn.deleteConnector).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(screen.getByText('Family bot')).toBeTruthy();
	});

	it('blocks deletion when the dependency check fails', async () => {
		conn.getConnectionDependencies.mockRejectedValue(new Error('boom'));
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete Family bot' }));
		expect((await screen.findByRole('alert')).textContent).toMatch('Could not check automation dependencies');
		expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('deletes after confirmation and reports a failure', async () => {
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Delete Family bot' }));
		await waitFor(() => expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(conn.deleteConnector).toHaveBeenCalledWith('c1'));

		conn.deleteConnector.mockRejectedValue({ detail: 'in use' });
		await fireEvent.click(await screen.findByRole('button', { name: 'Delete Family bot' }));
		await waitFor(() => expect((screen.getByRole('button', { name: 'Delete' }) as HTMLButtonElement).disabled).toBe(false));
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(notify).toHaveBeenCalledWith('in use', { severity: 'error' }));
	});

	it('polls connector status every 15 seconds and stops on unmount', async () => {
		vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
		const view = render(ConnectorManager);
		await screen.findByText('Family bot');
		conn.getConnectors.mockResolvedValue([connector({ status: 'error', status_message: 'token revoked' })]);
		await vi.advanceTimersByTimeAsync(15_000);
		expect(await screen.findByText('token revoked')).toBeTruthy();
		view.unmount();
		const calls = conn.getConnectors.mock.calls.length;
		await vi.advanceTimersByTimeAsync(30_000);
		expect(conn.getConnectors).toHaveBeenCalledTimes(calls);
	});
});

describe('ConnectorManager create and edit', () => {
	async function openTelegramSetup() {
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect account' }));
		await fireEvent.click(screen.getByRole('button', { name: /Telegram/ }));
		expect(screen.getByText('Set Up Telegram')).toBeTruthy();
	}

	it('validates the required field and the agent before creating', async () => {
		await openTelegramSetup();
		await fireEvent.click(screen.getByRole('button', { name: 'Create Connector' }));
		expect(await screen.findByText('Bot Token is required')).toBeTruthy();
		await fireEvent.input(screen.getByLabelText(/Bot Token/), { target: { value: '123:ABC' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create Connector' }));
		await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Choose an agent'));
		expect(conn.createConnector).not.toHaveBeenCalled();
	});

	it('creates a chat connector with the chosen agent and returns to the list', async () => {
		const onChanged = vi.fn();
		render(ConnectorManager, { props: { onChanged } });
		await screen.findByText('Family bot');
		await fireEvent.click(screen.getByRole('button', { name: 'Connect account' }));
		await fireEvent.click(screen.getByRole('button', { name: /Telegram/ }));
		await fireEvent.input(screen.getByLabelText(/Connector Name/), { target: { value: 'Work bot' } });
		await fireEvent.input(screen.getByLabelText(/Bot Token/), { target: { value: '123:ABC' } });
		await fireEvent.change(screen.getByLabelText(/^Agent/), { target: { value: 'a2' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create Connector' }));

		await waitFor(() => expect(conn.createConnector).toHaveBeenCalledOnce());
		expect(conn.createConnector).toHaveBeenCalledWith({
			name: 'Work bot', type: 'telegram', config: { bot_token: '123:ABC' }, events: [], profile_ids: ['a2']
		});
		expect(onChanged).toHaveBeenCalledOnce();
		expect(await screen.findByRole('button', { name: 'Connect account' })).toBeTruthy();
	});

	it('keeps the form and shows the worker error when creation fails', async () => {
		conn.createConnector.mockRejectedValue({ detail: 'Invalid bot token' });
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect account' }));
		await fireEvent.click(screen.getByRole('button', { name: /Webhook/ }));
		await fireEvent.input(screen.getByLabelText(/^URL/), { target: { value: 'https://hooks.example.com' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create Connector' }));
		expect(await screen.findByText('Invalid bot token')).toBeTruthy();
		expect((screen.getByLabelText(/^URL/) as HTMLInputElement).value).toBe('https://hooks.example.com');
		// Outbound webhooks run no agent, so none was asked for.
		expect(conn.createConnector.mock.calls[0][0]).not.toHaveProperty('profile_ids');
	});

	it('goes straight to setup when filtered to one type', async () => {
		conn.getConnectorTypes.mockResolvedValue([telegram, webhook]);
		render(ConnectorManager, { props: { typeFilter: 'webhook' } });
		await screen.findByText('No accounts connected');
		await fireEvent.click(screen.getByRole('button', { name: 'Connect account' }));
		expect(screen.getByText('Set Up Webhook')).toBeTruthy();
	});

	it('updates an existing connector with its id, name, config and events', async () => {
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Edit Family bot' }));
		expect(screen.getByText('Editing Family bot')).toBeTruthy();
		await fireEvent.input(screen.getByLabelText(/Connector Name/), { target: { value: 'Family chat' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Update Connector' }));
		await waitFor(() => expect(conn.updateConnector).toHaveBeenCalledWith('c1', {
			name: 'Family chat', config: { bot_token: '****abcd' }, events: ['message']
		}));
		expect(await screen.findByRole('button', { name: 'Connect account' })).toBeTruthy();
	});

	it('refuses to save an empty name', async () => {
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Edit Family bot' }));
		await fireEvent.input(screen.getByLabelText(/Connector Name/), { target: { value: '  ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Update Connector' }));
		expect(await screen.findByText('Name is required')).toBeTruthy();
		expect(conn.updateConnector).not.toHaveBeenCalled();
	});
});

describe('subscription sign-ins are the owner\'s own', () => {
	it('warns on a connection whose API user is set to a subscription model', async () => {
		getApiUsers.mockResolvedValue([
			{ id: 'u1', name: 'connector:telegram', is_active: true, subscription_warning: 'refused' }
		]);
		conn.getConnectors.mockResolvedValue([connector(), connector({ id: 'c2', name: 'Other bot', api_user_id: 'u2' })]);
		await loaded();
		const warnings = await screen.findAllByTestId('connector-subscription-warning');
		expect(warnings).toHaveLength(1);
		expect(warnings[0].textContent).toContain('Incoming messages are refused');
	});

	it('shows no warning for a connection on an API-key model', async () => {
		getApiUsers.mockResolvedValue([{ id: 'u1', name: 'connector:telegram', is_active: true, subscription_warning: null }]);
		await loaded();
		expect(screen.queryByTestId('connector-subscription-warning')).toBeNull();
	});

	it('disables agents on a subscription in the Agent choice, with the reason', async () => {
		apiGet.mockResolvedValue([
			{ id: 'a1', name: 'Helper', config: { model: 'prov:codex:gpt-5' } },
			{ id: 'a2', name: 'Planner', config: { model: 'gpt-5', provider: 'oa' } }
		]);
		usesSubscription.mockImplementation((model?: string | null) => model === 'prov:codex:gpt-5');
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect account' }));
		await fireEvent.click(screen.getByRole('button', { name: /Telegram/ }));
		const helper = screen.getByRole('option', { name: /Helper/ }) as HTMLOptionElement;
		const planner = screen.getByRole('option', { name: 'Planner' }) as HTMLOptionElement;
		expect(helper.disabled).toBe(true);
		expect(helper.textContent).toContain('(your subscription)');
		expect(planner.disabled).toBe(false);
		expect(screen.getByTestId('connector-agent-subscription-reason').textContent).toContain(
			"can't run for other people"
		);
	});
	it('lets a connection that knows your account save it, with recent senders to copy', async () => {
		conn.getConnectorTypes.mockResolvedValue([{ ...telegram, owner_sender: true }, webhook]);
		getApiUsers.mockResolvedValue([
			{ id: 'u1', name: 'connector:telegram', is_active: true, subscription_warning: 'refused' }
		]);
		conn.getConnectorSenders.mockResolvedValue([
			{ platform_user_id: '111', platform_user_name: 'Sam', last_seen: null }
		]);
		await loaded();
		expect((await screen.findByTestId('connector-subscription-warning')).textContent).toContain(
			'Add your own account below'
		);
		const field = screen.getByTestId('connector-owner-account');
		expect(field.textContent).toContain(
			'Your own account on this platform. Messages from it can use your subscriptions'
		);
		await fireEvent.click(screen.getByRole('button', { name: 'Recent senders' }));
		expect(conn.getConnectorSenders).toHaveBeenCalledWith('c1');
		await fireEvent.click(await screen.findByRole('button', { name: 'Sam · 111' }));
		const input = screen.getByLabelText('Your account') as HTMLInputElement;
		expect(input.value).toBe('111');
		await fireEvent.input(input, { target: { value: '111, 222 111' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
		await waitFor(() =>
			expect(conn.updateConnector).toHaveBeenCalledWith('c1', { config: { owner_sender_ids: ['111', '222'] } })
		);
	});

	it('keeps a saved account editable once the warning is gone', async () => {
		conn.getConnectorTypes.mockResolvedValue([{ ...telegram, owner_sender: true }, webhook]);
		conn.getConnectors.mockResolvedValue([connector({ config: { owner_sender_ids: ['111'] } })]);
		await loaded();
		expect(screen.queryByTestId('connector-subscription-warning')).toBeNull();
		expect(((await screen.findByLabelText('Your account')) as HTMLInputElement).value).toBe('111');
	});

	it('offers no account field where the connection cannot recognise a sender', async () => {
		getApiUsers.mockResolvedValue([
			{ id: 'u1', name: 'connector:webhook', is_active: true, subscription_warning: 'refused' }
		]);
		conn.getConnectors.mockResolvedValue([connector({ type: 'webhook' })]);
		await loaded();
		expect((await screen.findByTestId('connector-subscription-warning')).textContent).not.toContain(
			'Add your own account'
		);
		expect(screen.queryByTestId('connector-owner-account')).toBeNull();
	});

	it('allows a subscription agent where only your own messages will use it', async () => {
		conn.getConnectorTypes.mockResolvedValue([{ ...telegram, owner_sender: true }, webhook]);
		apiGet.mockResolvedValue([{ id: 'a1', name: 'Helper', config: { model: 'prov:codex:gpt-5' } }]);
		usesSubscription.mockImplementation((model?: string | null) => model === 'prov:codex:gpt-5');
		await loaded();
		await fireEvent.click(screen.getByRole('button', { name: 'Connect account' }));
		await fireEvent.click(screen.getByRole('button', { name: /Telegram/ }));
		const helper = screen.getByRole('option', { name: /Helper/ }) as HTMLOptionElement;
		expect(helper.disabled).toBe(false);
		expect(helper.textContent).toContain('your messages only');
		expect(screen.getByTestId('connector-agent-subscription-reason').textContent).toContain(
			'answers only your own account'
		);
	});
});
