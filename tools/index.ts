/**
 * AI Hub Tools
 *
 * This directory contains tools that Claude can use during code execution.
 * Each tool is a TypeScript module that wraps AI Hub's internal APIs.
 *
 * ## Available Tools
 *
 * ### Image Generation (Nano Banana)
 * Generate images using Google Gemini's image models.
 * ```typescript
 * import * as imageGen from './tools/image-generation';
 * const result = await imageGen.generateImage({ prompt: 'A sunset over mountains' });
 * ```
 *
 * ## How to Use
 *
 * 1. Import the tool module you need
 * 2. Call the function with appropriate parameters
 * 3. Handle the response (check success/error)
 *
 * ## Adding New Tools
 *
 * Create a new directory under /tools with:
 * - Individual tool files (e.g., toolName.ts)
 * - An index.ts that exports all tools
 * - Clear documentation and TypeScript interfaces
 *
 * Tools should follow the pattern:
 * - Input interface with JSDoc descriptions
 * - Output interface with success/error handling
 * - Async function that calls the API and handles errors
 */

// Re-export all tools for convenience
export * as imageGeneration from './image-generation/index.js';

// Export the client utilities
export { callTool, getTool, getApiBaseUrl } from './client.js';
