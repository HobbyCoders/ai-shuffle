# AI Hub Tools

This directory contains code-executable tools that Claude can use during conversations. Following the [MCP code execution pattern](https://www.anthropic.com/engineering/code-execution-with-mcp), these tools are TypeScript modules that wrap AI Hub's internal APIs.

## Available Tools

### Image Generation (Nano Banana)

Generate AI images using Google Gemini's image models.

**Setup:** Configure your Google AI API key in Settings > Integrations > Nano Banana

```typescript
// The import path is automatically injected by AI Hub when tools are enabled.
// It will be an absolute path like: /workspace/ai-hub/tools/dist/image-generation/generateImage.js
import { generateImage } from '/workspace/ai-hub/tools/dist/image-generation/generateImage.js';
import * as fs from 'fs';

// Generate an image
const result = await generateImage({
  prompt: 'A futuristic city at sunset, cyberpunk style, neon lights'
});

if (result.success) {
  // Save the image
  const buffer = Buffer.from(result.image_base64!, 'base64');
  fs.writeFileSync('generated-image.png', buffer);
  console.log('Image saved to generated-image.png');
} else {
  console.error('Generation failed:', result.error);
}
```

**Parameters:**
- `prompt` (required): Text description of the image to generate
- `aspect_ratio` (optional): Image dimensions - `1:1`, `16:9`, `9:16`, `4:3`, `3:4`

**Returns:**
- `success`: Whether generation succeeded
- `image_base64`: Base64-encoded PNG image data
- `mime_type`: Image MIME type (usually `image/png`)
- `error`: Error message if failed

## Architecture

```
tools/
├── client.ts              # API client for making requests
├── index.ts               # Main exports
├── README.md              # This file
└── image-generation/
    ├── index.ts           # Module exports
    └── generateImage.ts   # Image generation tool
```

## Adding New Tools

1. Create a new directory for your tool category (e.g., `voice-synthesis/`)
2. Create TypeScript files for each tool with:
   - Input/Output interfaces with JSDoc documentation
   - Async function that calls the AI Hub API
   - Error handling that returns structured responses
3. Create an `index.ts` that exports all tools
4. Update the main `tools/index.ts` to include your new tools

### Example Tool Template

```typescript
// tools/my-category/myTool.ts
import { callTool } from '../client.js';

export interface MyToolInput {
  /** Description of the parameter */
  param: string;
}

export interface MyToolResponse {
  success: boolean;
  data?: SomeType;
  error?: string;
}

export async function myTool(input: MyToolInput): Promise<MyToolResponse> {
  try {
    return await callTool<MyToolResponse>('/endpoint', input);
  } catch (error) {
    return {
      success: false,
      error: error instanceof Error ? error.message : 'Unknown error'
    };
  }
}
```

## Pricing

| Tool | Provider | Cost |
|------|----------|------|
| Image Generation | Google Gemini (Nano Banana) | ~$0.039/image |

## Future Tools

- **Voice Synthesis**: Text-to-speech using ElevenLabs or similar
- **Video Generation**: AI video creation
- **Music Generation**: AI music/audio creation
- **3D Generation**: 3D model creation
