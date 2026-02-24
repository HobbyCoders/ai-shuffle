/**
 * Persistent Node.js Worker - Handles AI tool execution via stdin/stdout JSON-RPC.
 *
 * Replaces the fragile "write temp .mjs file, spawn node" pattern.
 * Started once per session, receives tool calls via stdin, returns results via stdout.
 *
 * Benefits over the old pattern:
 *   - No temp file I/O per tool call
 *   - No cold Node.js startup per call (~200-500ms saved)
 *   - All modules pre-imported at startup
 *   - Concurrent request handling via request IDs
 *
 * Protocol (newline-delimited JSON):
 *   Ready:    {"ready":true}
 *   Request:  {"id":"<uuid>","tool":"<name>","args":{...}}
 *   Response: {"id":"<uuid>","result":{...}}
 *   Error:    {"id":"<uuid>","error":"<message>"}
 */

import { createInterface } from 'readline';

// Import all tool functions
import { generateImage } from './image-generation/generateImage.js';
import { editImage } from './image-generation/editImage.js';
import { generateWithReference } from './image-generation/generateWithReference.js';
import { generateVideo } from './video-generation/generateVideo.js';
import { imageToVideo } from './video-generation/imageToVideo.js';
import { extendVideo } from './video-generation/extendVideo.js';
import { textTo3D } from './model3d-generation/textTo3D.js';
import { imageTo3D } from './model3d-generation/imageTo3D.js';
import { executeFalModel } from './providers/fal-generic.js';

// Tool dispatch table
const tools: Record<string, (args: any) => Promise<any>> = {
  generateImage,
  editImage,
  generateWithReference,
  generateVideo,
  imageToVideo,
  extendVideo,
  textTo3D,
  imageTo3D,
  runFalModel: executeFalModel,
};

// Redirect console.log to stderr so stdout is reserved for protocol messages.
// Tool implementations use console.log for progress messages (e.g., "[textTo3D] Task started...").
// These are debug info and belong on stderr.
console.log = (...args: any[]) => {
  process.stderr.write(
    args.map(a => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ') + '\n'
  );
};

// Write a protocol response to stdout (one JSON line per response)
function sendResponse(response: object): void {
  process.stdout.write(JSON.stringify(response) + '\n');
}

// Process a single request (async — multiple can be in-flight concurrently)
async function handleRequest(line: string): Promise<void> {
  let id: string | undefined;
  try {
    const request = JSON.parse(line);
    id = request.id;
    const toolName = request.tool;
    const toolFn = tools[toolName];

    if (!toolFn) {
      sendResponse({ id, error: `Unknown tool: ${toolName}` });
      return;
    }

    const result = await toolFn(request.args || {});
    sendResponse({ id, result });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    sendResponse({ id, error: message });
  }
}

// Main: read stdin line by line
const rl = createInterface({ input: process.stdin });

// Signal ready — Python side waits for this before sending requests
sendResponse({ ready: true });

rl.on('line', (line) => {
  const trimmed = line.trim();
  if (trimmed) {
    // Fire-and-forget: allows concurrent tool execution
    // (e.g., image gen + 3D gen running simultaneously)
    handleRequest(trimmed);
  }
});

rl.on('close', () => {
  process.exit(0);
});
