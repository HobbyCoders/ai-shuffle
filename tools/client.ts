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

export function getApiBaseUrl(): string {
  return API_BASE_URL;
}

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
export async function callTool<T>(endpoint: string, payload: Record<string, unknown>): Promise<T> {
  const url = `${API_BASE_URL}/api/v1${endpoint}`;

  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': AUTH_TOKEN ? `Bearer ${AUTH_TOKEN}` : '',
      'Cookie': '', // Session cookie will be included automatically in browser context
    },
    body: JSON.stringify(payload),
    credentials: 'include',
  });

  if (!response.ok) {
    const errorText = await response.text();
    let errorMessage: string;
    try {
      const errorJson = JSON.parse(errorText);
      errorMessage = errorJson.detail || errorJson.message || errorText;
    } catch {
      errorMessage = errorText;
    }
    throw new Error(`API call failed (${response.status}): ${errorMessage}`);
  }

  return response.json() as Promise<T>;
}

/**
 * Call an AI Hub API endpoint with GET method
 *
 * @param endpoint - The API endpoint path
 * @returns The API response
 */
export async function getTool<T>(endpoint: string): Promise<T> {
  const url = `${API_BASE_URL}/api/v1${endpoint}`;

  const response = await fetch(url, {
    method: 'GET',
    headers: {
      'Authorization': AUTH_TOKEN ? `Bearer ${AUTH_TOKEN}` : '',
    },
    credentials: 'include',
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`API call failed (${response.status}): ${errorText}`);
  }

  return response.json() as Promise<T>;
}
