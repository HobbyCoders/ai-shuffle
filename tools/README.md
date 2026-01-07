# AI Hub Tools

This directory contains code-executable tools that Claude can use during conversations. Following the [MCP code execution pattern](https://www.anthropic.com/engineering/code-execution-with-mcp), these tools are TypeScript modules that wrap AI Hub's internal APIs.

## Available Tools

### Image Generation (Nano Banana)

Generate AI images using Google Gemini's image models.

**Setup:** Configure your Google AI API key in Settings > Integrations > Nano Banana

```typescript
// IMPORTANT: Save as .mjs and run with: node yourscript.mjs
import { generateImage } from '/workspace/ai-hub/tools/dist/image-generation/generateImage.js';

// Generate an image
const result = await generateImage({
  prompt: 'A futuristic city at sunset, cyberpunk style, neon lights'
});

// IMPORTANT: Output the result as JSON - the chat UI will display the image with download button
// Do NOT save to file or try to read the base64 - just output the JSON result
console.log(JSON.stringify(result));
```

**Displaying in Chat:** Images use base64 data URLs. The chat UI automatically renders base64 image markdown. After generation succeeds, display using markdown image syntax with the base64 data:
```
![Description](data:image/png;base64,...)
```

**Parameters:**
- `prompt` (required): Text description of the image to generate
- `aspect_ratio` (optional): Image dimensions - `1:1`, `16:9`, `9:16`, `4:3`, `3:4`

**Returns:**
- `success`: Whether generation succeeded
- `image_url`: URL to access the generated image (for display in chat)
- `file_path`: Local file path where the image was saved
- `filename`: Filename of the generated image
- `mime_type`: Image MIME type (usually `image/png`)
- `error`: Error message if failed

---

### Image Editing (Nano Banana)

Edit existing images using AI. Add elements, remove objects, change styles, and more.

**Setup:** Configure your Google AI API key in Settings > Integrations > Nano Banana

```typescript
// IMPORTANT: Save as .mjs and run with: node yourscript.mjs
import { editImage } from '/workspace/ai-hub/tools/dist/image-generation/editImage.js';

// Edit an existing image
const result = await editImage({
  prompt: 'Add a rainbow in the sky',
  image_path: '/path/to/existing/image.png'
});

// IMPORTANT: Output the result as JSON - the chat UI will display the image with download button
// Do NOT save to file or try to read the base64 - just output the JSON result
console.log(JSON.stringify(result));
```

**Parameters:**
- `prompt` (required): Editing instruction describing what changes to make
- `image_path` (optional): Path to the image file to edit
- `image_base64` (optional): Base64-encoded image data (use this OR image_path)
- `image_mime_type` (optional): MIME type when using base64 - `image/png`, `image/jpeg`, `image/gif`, `image/webp`
- `aspect_ratio` (optional): Output dimensions - `1:1`, `16:9`, `9:16`, `4:3`, `3:4`, `2:3`, `3:2`, `4:5`, `5:4`, `21:9`

**Returns:**
- `success`: Whether editing succeeded
- `image_url`: URL to access the edited image (for display in chat)
- `file_path`: Local file path where the edited image was saved
- `filename`: Filename of the edited image
- `mime_type`: Image MIME type (usually `image/png`)
- `error`: Error message if failed

**Example Use Cases:**
- Add or remove objects: "Add a cat sitting on the couch", "Remove the person on the left"
- Change styles: "Make it look like a watercolor painting", "Convert to black and white"
- Modify backgrounds: "Change the background to a beach sunset"
- Enhance images: "Make the colors more vibrant", "Add dramatic lighting"

---

### Video Generation (Veo)

Generate AI videos using Google Veo models.

**Setup:** Configure your Google AI API key in Settings > Integrations > Nano Banana (same key is used)

```typescript
// IMPORTANT: Save as .mjs and run with: node yourscript.mjs
import { generateVideo } from '/workspace/ai-hub/tools/dist/video-generation/generateVideo.js';

// Generate a video
const result = await generateVideo({
  prompt: 'A cat playing with a ball of yarn, slow motion',
  duration: 8,
  aspect_ratio: '16:9'
});

// IMPORTANT: Output the result as JSON
// Video generation takes 1-6 minutes, please be patient
console.log(JSON.stringify(result));
```

**Displaying in Chat:** Videos are displayed via markdown links to the API endpoint. After generation succeeds, display the video using a markdown link with the `video_url` from the result:
```
[Video Description](result.video_url)
```
Example: `[My Generated Video](/api/generated-videos/by-path?path=...)`

The chat UI automatically converts these links into embedded video players with download buttons.

**Parameters:**
- `prompt` (required): Text description of the video to generate (max ~1,024 tokens)
- `duration` (optional): Duration in seconds - `4`, `6`, or `8` (default: 8)
- `aspect_ratio` (optional): `16:9` (landscape) or `9:16` (vertical) (default: 16:9)
- `resolution` (optional): `720p` or `1080p` (1080p only for 8-sec videos) (default: 720p)
- `negative_prompt` (optional): Elements to exclude from the video

**Returns:**
- `success`: Whether generation succeeded
- `video_url`: URL to access the generated video (for display in chat)
- `file_path`: Local file path where the video was saved
- `filename`: Filename of the generated video
- `mime_type`: Video MIME type (usually `video/mp4`)
- `duration_seconds`: Duration of the video
- `error`: Error message if failed

**Available Models:**
- `veo-3.1-fast-generate-preview` - Fast generation, lower latency (~$0.15/sec)
- `veo-3.1-generate-preview` - High quality generation (~$0.40/sec)

## Architecture

```
tools/
├── client.ts              # API client for making requests
├── index.ts               # Main exports
├── README.md              # This file
└── image-generation/
    ├── index.ts           # Module exports
    ├── generateImage.ts   # Image generation tool
    └── editImage.ts       # Image editing tool
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
| Image Editing | Google Gemini (Nano Banana) | ~$0.039/image |

## Future Tools

- **Voice Synthesis**: Text-to-speech using ElevenLabs or similar
- **Video Generation**: AI video creation
- **Music Generation**: AI music/audio creation
- **3D Generation**: 3D model creation
