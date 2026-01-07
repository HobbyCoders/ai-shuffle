import { api } from './client';

export interface ProviderModel {
	id: string;
	name: string;
	default?: boolean;
	has_audio?: boolean;
}

export interface ImageProvider {
	id: string;
	name: string;
	description: string;
	models: ProviderModel[];
	aspect_ratios: string[];
	resolutions: string[];
	supports_edit: boolean;
	supports_reference: boolean;
}

export interface VideoProvider {
	id: string;
	name: string;
	description: string;
	models: ProviderModel[];
	aspect_ratios: string[];
	durations: number[];
	max_duration: number;
	supports_extend: boolean;
	supports_image_to_video: boolean;
}

export interface ProvidersResponse {
	image_providers: ImageProvider[];
	video_providers: VideoProvider[];
}

let providersCache: ProvidersResponse | null = null;

export async function getProviders(): Promise<ProvidersResponse> {
	if (providersCache) return providersCache;

	const response = await api.get<ProvidersResponse>('/canvas/providers');
	providersCache = response;
	return response;
}
