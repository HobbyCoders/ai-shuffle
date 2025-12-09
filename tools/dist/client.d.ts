/**
 * AI Hub Tools Client
 *
 * This module provides a client for calling AI Hub's internal APIs from within
 * the code execution environment. Tools can use this to interact with configured
 * AI services like image generation, voice synthesis, etc.
 *
 * Usage:
 *   import { callTool, getApiBaseUrl } from '../client.js';
 *   const result = await callTool<ResponseType>('endpoint', payload);
 */
export declare function getApiBaseUrl(): string;
export interface ApiResponse<T> {
    success: boolean;
    data?: T;
    error?: string;
}
/**
 * Call an AI Hub API endpoint
 *
 * @param endpoint - The API endpoint path (e.g., '/settings/generate-image')
 * @param payload - The request payload
 * @returns The API response
 */
export declare function callTool<T>(endpoint: string, payload: Record<string, unknown>): Promise<T>;
/**
 * Call an AI Hub API endpoint with GET method
 *
 * @param endpoint - The API endpoint path
 * @returns The API response
 */
export declare function getTool<T>(endpoint: string): Promise<T>;
/**
 * Call an AI Hub API endpoint with multipart form data (for file uploads)
 *
 * @param endpoint - The API endpoint path
 * @param formData - FormData object containing files and fields
 * @returns The API response
 */
export declare function uploadTool<T>(endpoint: string, formData: FormData): Promise<T>;
//# sourceMappingURL=client.d.ts.map