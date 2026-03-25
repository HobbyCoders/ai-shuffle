/**
 * API client for Connectors - external integrations (Telegram, Discord, WhatsApp, Webhook, Slack, LINE, Signal, iMessage)
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export interface ConnectorConfig {
	[key: string]: any;
}

export interface ConnectorCreate {
	name: string;
	type: 'telegram' | 'discord' | 'whatsapp' | 'webhook' | 'slack' | 'line' | 'signal' | 'imessage' | 'msteams' | 'bluebubbles' | 'spotify' | 'hue' | 'homeassistant' | 'gmail';
	config: ConnectorConfig;
	events: string[];
	api_user_id?: string;
	project_id?: string;
	profile_ids?: string[];
}

export interface ConnectorUpdate {
	name?: string;
	config?: ConnectorConfig;
	events?: string[];
	is_active?: boolean;
}

export interface Connector {
	id: string;
	type: 'telegram' | 'discord' | 'whatsapp' | 'webhook' | 'slack' | 'line' | 'signal' | 'imessage' | 'msteams' | 'bluebubbles' | 'spotify' | 'hue' | 'homeassistant' | 'gmail';
	name: string;
	api_user_id: string | null;
	config: ConnectorConfig;
	events: string[];
	is_active: boolean;
	status: 'connected' | 'disconnected' | 'connecting' | 'error';
	status_message: string | null;
	last_connected_at: string | null;
	last_event_at: string | null;
	failure_count: number;
	created_at: string;
	updated_at: string;
}

export interface ConnectorTestResponse {
	success: boolean;
	message: string;
	response_time_ms?: number;
	error?: string;
}

export interface ConnectorTriggerInfo {
	id: string;
	name: string;
	description: string;
	payload_schema: Record<string, any>;
}

export interface ConnectorActionInfo {
	id: string;
	name: string;
	description: string;
	input_schema: Record<string, any>;
}

export interface ConnectorToolInfo {
	id: string;
	name: string;
	description: string;
	input_schema: Record<string, any>;
}

export interface ConnectorTypeInfo {
	type: string;
	name: string;
	description: string;
	config_fields: Array<{
		key: string;
		label: string;
		type: string; // 'password', 'text', 'textarea'
		required: boolean;
		placeholder?: string;
		description?: string;
	}>;
	triggers: ConnectorTriggerInfo[];
	actions: ConnectorActionInfo[];
	tools: ConnectorToolInfo[];
}

export interface ConnectorEventLog {
	id: string;
	connector_id: string;
	event_type: string;
	direction: string;
	payload: any;
	status: string;
	error: string | null;
	created_at: string;
}

// ============================================================================
// API Functions
// ============================================================================

/**
 * Get all connectors
 */
export async function getConnectors(): Promise<Connector[]> {
	return api.get<Connector[]>('/connectors');
}

/**
 * Create a new connector
 */
export async function createConnector(data: ConnectorCreate): Promise<Connector> {
	return api.post<Connector>('/connectors', data);
}

/**
 * Get a single connector by ID
 */
export async function getConnector(id: string): Promise<Connector> {
	return api.get<Connector>(`/connectors/${id}`);
}

/**
 * Update a connector
 */
export async function updateConnector(id: string, data: ConnectorUpdate): Promise<Connector> {
	return api.patch<Connector>(`/connectors/${id}`, data);
}

/**
 * Delete a connector
 */
export async function deleteConnector(id: string): Promise<void> {
	await api.delete(`/connectors/${id}`);
}

/**
 * Test a connector's connection
 */
export async function testConnector(id: string): Promise<ConnectorTestResponse> {
	return api.post<ConnectorTestResponse>(`/connectors/${id}/test`);
}

/**
 * Activate a connector
 */
export async function activateConnector(id: string): Promise<Connector> {
	return api.post<Connector>(`/connectors/${id}/activate`);
}

/**
 * Deactivate a connector
 */
export async function deactivateConnector(id: string): Promise<Connector> {
	return api.post<Connector>(`/connectors/${id}/deactivate`);
}

/**
 * Restart a connector
 * TODO: Wire up to ConnectorManager UI (restart button)
 */
export async function restartConnector(id: string): Promise<Connector> {
	return api.post<Connector>(`/connectors/${id}/restart`);
}

/**
 * Get all available connector types with config field definitions
 */
export async function getConnectorTypes(): Promise<ConnectorTypeInfo[]> {
	return api.get<ConnectorTypeInfo[]>('/connectors/types');
}

/**
 * Get all available connector event types
 */
export async function getConnectorEvents(): Promise<string[]> {
	return api.get<string[]>('/connectors/events');
}

/**
 * Get event logs for a connector
 */
export async function getConnectorLogs(id: string): Promise<ConnectorEventLog[]> {
	return api.get<ConnectorEventLog[]>(`/connectors/${id}/logs`);
}

/**
 * Get current status for a connector
 * TODO: Wire up to ConnectorManager UI (status display)
 */
export async function getConnectorStatus(id: string): Promise<any> {
	return api.get<any>(`/connectors/${id}/status`);
}

// ============================================================================
// Variant API Functions (Triggers, Actions, Tools)
// TODO: Wire up to ConnectorManager UI — backend endpoints exist and are functional
// ============================================================================

/**
 * Get available triggers for a connector
 */
export async function getConnectorTriggers(id: string): Promise<ConnectorTriggerInfo[]> {
	return api.get<ConnectorTriggerInfo[]>(`/connectors/${id}/triggers`);
}

/**
 * Get available actions for a connector
 */
export async function getConnectorActions(id: string): Promise<ConnectorActionInfo[]> {
	return api.get<ConnectorActionInfo[]>(`/connectors/${id}/actions`);
}

/**
 * Get available tools for a connector
 */
export async function getConnectorTools(id: string): Promise<ConnectorToolInfo[]> {
	return api.get<ConnectorToolInfo[]>(`/connectors/${id}/tools`);
}

/**
 * Execute a connector action directly
 * TODO: Wire up to ConnectorManager UI (action executor)
 */
export async function executeConnectorAction(
	id: string,
	actionId: string,
	params: Record<string, any> = {}
): Promise<any> {
	return api.post<any>(`/connectors/${id}/actions/${actionId}/execute`, params);
}
