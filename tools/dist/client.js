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
// API base URL - injected by the execution environment or use default
const API_BASE_URL = process.env.AI_HUB_API_URL || 'http://localhost:8080';
// Auth token - injected by the execution environment
const AUTH_TOKEN = process.env.AI_HUB_AUTH_TOKEN || '';
export function getApiBaseUrl() {
    return API_BASE_URL;
}
/**
 * Call an AI Hub API endpoint
 *
 * @param endpoint - The API endpoint path (e.g., '/settings/generate-image')
 * @param payload - The request payload
 * @returns The API response
 */
export async function callTool(endpoint, payload) {
    const url = `${API_BASE_URL}/api/v1${endpoint}`;
    // Build headers - only include Authorization if we have a token
    const headers = {
        'Content-Type': 'application/json',
    };
    if (AUTH_TOKEN) {
        headers['Authorization'] = `Bearer ${AUTH_TOKEN}`;
    }
    const response = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
        credentials: 'include',
    });
    if (!response.ok) {
        const errorText = await response.text();
        let errorMessage;
        try {
            const errorJson = JSON.parse(errorText);
            errorMessage = errorJson.detail || errorJson.message || errorText;
        }
        catch {
            errorMessage = errorText;
        }
        throw new Error(`API call failed (${response.status}): ${errorMessage}`);
    }
    return response.json();
}
/**
 * Call an AI Hub API endpoint with GET method
 *
 * @param endpoint - The API endpoint path
 * @returns The API response
 */
export async function getTool(endpoint) {
    const url = `${API_BASE_URL}/api/v1${endpoint}`;
    // Build headers - only include Authorization if we have a token
    const headers = {};
    if (AUTH_TOKEN) {
        headers['Authorization'] = `Bearer ${AUTH_TOKEN}`;
    }
    const response = await fetch(url, {
        method: 'GET',
        headers,
        credentials: 'include',
    });
    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`API call failed (${response.status}): ${errorText}`);
    }
    return response.json();
}
/**
 * Call an AI Hub API endpoint with multipart form data (for file uploads)
 *
 * @param endpoint - The API endpoint path
 * @param formData - FormData object containing files and fields
 * @returns The API response
 */
export async function uploadTool(endpoint, formData) {
    const url = `${API_BASE_URL}/api/v1${endpoint}`;
    // Build headers - only include Authorization if we have a token
    // Don't set Content-Type for FormData - browser will set it with boundary
    const headers = {};
    if (AUTH_TOKEN) {
        headers['Authorization'] = `Bearer ${AUTH_TOKEN}`;
    }
    const response = await fetch(url, {
        method: 'POST',
        headers,
        body: formData,
        credentials: 'include',
    });
    if (!response.ok) {
        const errorText = await response.text();
        let errorMessage;
        try {
            const errorJson = JSON.parse(errorText);
            errorMessage = errorJson.detail || errorJson.message || errorText;
        }
        catch {
            errorMessage = errorText;
        }
        throw new Error(`API call failed (${response.status}): ${errorMessage}`);
    }
    return response.json();
}
//# sourceMappingURL=client.js.map