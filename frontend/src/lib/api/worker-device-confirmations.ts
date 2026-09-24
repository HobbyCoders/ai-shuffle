import { api } from './client';

/** Worker-local decisions, never inferred from account-hub registration. */
export interface WorkerDeviceConfirmation {
	device_id: string;
	hub_device_id: string;
	paired_client_id: string;
	mobile_record_id: string;
	device_name: string;
	platform: string;
	confirmation: 'pending' | 'approved' | 'denied';
	revoked_at: string | null;
	api_key_expires_at: string | null;
}

export function listWorkerDeviceConfirmations(): Promise<WorkerDeviceConfirmation[]> {
	return api.get('/mobile/hub/devices/confirmations', { cache: 'no-store' });
}

export function confirmWorkerDevice(device: WorkerDeviceConfirmation, approved: boolean) {
	return api.post<{ confirmation: 'approved' | 'denied' }>(
		`/mobile/hub/devices/${encodeURIComponent(device.device_id)}/confirmation`,
		{ paired_client_id: device.paired_client_id, mobile_record_id: device.mobile_record_id, approved }
	);
}
