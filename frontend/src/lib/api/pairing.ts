import { api } from './client';

export interface PairingEndpoint {
	url: string;
	kind: 'loopback' | 'lan' | 'tailscale' | 'https' | 'custom';
	label: string;
	secure: boolean;
	loopback: boolean;
	qr_eligible: boolean;
	source: 'current' | 'advertised';
}

export interface PairingGrant {
	id: string;
	purpose: 'controller' | 'runner';
	scopes: string[];
	target_install_id: string | null;
	endpoint_url: string | null;
	device_label: string | null;
	created_by: string;
	created_at: string;
	expires_at: string;
	consumed_at: string | null;
	revoked_at: string | null;
	attempt_count: number;
	max_attempts: number;
}

export interface CreatedPairingGrant {
	grant: PairingGrant;
	code: string;
	display_code: string;
	pairing_url: string | null;
	qr_svg: string | null;
}

export interface PairedClient {
	id: string;
	device_id: string;
	device_name: string;
	platform: string;
	client_type: 'browser' | 'pwa' | 'mobile' | string;
	scopes: string[];
	session_expires_at: string | null;
	paired_at: string;
	last_seen: string | null;
	revoked_at: string | null;
	created_at: string;
	updated_at: string;
}

export async function listPairingEndpoints(): Promise<PairingEndpoint[]> {
	const response = await api.get<{ endpoints: PairingEndpoint[] }>('/pairing/endpoints');
	return response.endpoints;
}

export async function createPairingGrant(
	endpointUrl: string | null,
	deviceLabel?: string,
	purpose: 'controller' | 'runner' = 'controller'
): Promise<CreatedPairingGrant> {
	return api.post<CreatedPairingGrant>('/pairing/grants', {
		endpoint_url: endpointUrl,
		device_label: deviceLabel?.trim() || null,
		purpose
	});
}

export async function revokePairingGrant(grantId: string): Promise<void> {
	await api.delete(`/pairing/grants/${encodeURIComponent(grantId)}`);
}

export async function exchangePairingGrant(input: {
	code: string;
	deviceId: string;
	deviceName: string;
	platform: string;
	clientType?: 'browser' | 'pwa';
}): Promise<{ status: 'ok'; client: PairedClient; scopes: string[]; expires_at: string }> {
	return api.post('/pairing/exchange', {
		code: input.code,
		device_id: input.deviceId,
		device_name: input.deviceName,
		platform: input.platform,
		client_type: input.clientType ?? 'browser'
	});
}

export async function listPairedClients(): Promise<PairedClient[]> {
	const response = await api.get<{ clients: PairedClient[] }>('/pairing/clients');
	return response.clients;
}

export async function renamePairedClient(clientId: string, deviceName: string): Promise<PairedClient> {
	return api.patch(`/pairing/clients/${encodeURIComponent(clientId)}`, {
		device_name: deviceName
	});
}

export async function revokePairedClient(clientId: string): Promise<void> {
	await api.delete(`/pairing/clients/${encodeURIComponent(clientId)}`);
}
