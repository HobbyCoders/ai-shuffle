# AI Hub Tools

This directory contains code-executable tools that Claude can use during conversations. Following the [MCP code execution pattern](https://www.anthropic.com/engineering/code-execution-with-mcp), these tools are TypeScript modules that wrap AI Hub's internal APIs.

## Available Tools

### Image Generation (Nano Banana)

Generate AI images using Google Gemini's image models.

**Setup:** Configure your Google AI API key in Settings > Integrations > Nano Banana

```typescript
// IMPORTANT: Save as .mjs and run with: node yourscript.mjs
import { generateImage } from '/opt/ai-tools/dist/image-generation/generateImage.js';

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
import { editImage } from '/opt/ai-tools/dist/image-generation/editImage.js';

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
import { generateVideo } from '/opt/ai-tools/dist/video-generation/generateVideo.js';

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

---

### 3D Model Generation (Meshy)

Generate AI 3D models using Meshy's API. Supports text-to-3D, image-to-3D, retexturing, rigging, and animation.

**Setup:** Configure your Meshy API key in Settings > Integrations > Meshy (get key from https://app.meshy.ai/settings/api)

#### Text to 3D

```typescript
// IMPORTANT: Save as .mjs and run with: node yourscript.mjs
import { textTo3D } from '/opt/ai-tools/dist/model3d-generation/textTo3D.js';

// Generate a 3D model from text
const result = await textTo3D({
  prompt: 'A medieval sword with ornate handle',
  art_style: 'realistic',  // 'realistic', 'cartoon', 'low-poly', 'sculpture'
  topology: 'quad'  // 'quad' or 'triangle'
});

// Generation takes 2-5 minutes. Poll for completion using getTask3D
console.log(JSON.stringify(result));
```

**Parameters:**
- `prompt` (required): Text description of the 3D model to generate
- `art_style` (optional): Style of the model - `realistic`, `cartoon`, `low-poly`, `sculpture`
- `topology` (optional): Mesh topology - `quad` or `triangle`
- `target_polycount` (optional): Target polygon count for the mesh

**Returns:**
- `task_id`: ID for polling task status
- `status`: Task status (PENDING, IN_PROGRESS, SUCCEEDED, FAILED)
- `model_urls`: Download URLs for GLB, FBX, OBJ, USDZ formats (when complete)
- `thumbnail_url`: Preview thumbnail URL
- `video_url`: 360° preview video URL
- `error`: Error message if failed

#### Image to 3D

```typescript
import { imageTo3D } from '/opt/ai-tools/dist/model3d-generation/imageTo3D.js';

// Convert an image to a 3D model
const result = await imageTo3D({
  image_path: '/path/to/reference.png',
  topology: 'quad'
});

console.log(JSON.stringify(result));
```

**Parameters:**
- `image_path` (required): Path to the reference image
- `topology` (optional): Mesh topology - `quad` or `triangle`

#### Retexture 3D

```typescript
import { retexture3D } from '/opt/ai-tools/dist/model3d-generation/retexture3D.js';

// Apply new AI textures to an existing model
const result = await retexture3D({
  model_path: '/path/to/model.glb',  // or URL
  prompt: 'Worn leather texture with scratches',
  art_style: 'realistic'
});

console.log(JSON.stringify(result));
```

#### Rig 3D (Auto-Rigging)

```typescript
import { rig3D } from '/opt/ai-tools/dist/model3d-generation/rig3D.js';

// Add animation skeleton to a humanoid model
const result = await rig3D({
  model_path: '/path/to/humanoid.glb'
});

console.log(JSON.stringify(result));
```

**Note:** Only works with humanoid models with proper proportions.

#### Animate 3D

```typescript
import { animate3D } from '/opt/ai-tools/dist/model3d-generation/animate3D.js';

// Apply preset animation to a rigged model
const result = await animate3D({
  model_path: '/path/to/rigged_model.glb',
  animation: 'walk'  // 'walk', 'run', 'jump', 'idle', 'wave', etc.
});

console.log(JSON.stringify(result));
```

**Note:** Model must be rigged first using rig3D.

#### Check Task Status

```typescript
import { getTask3D } from '/opt/ai-tools/dist/model3d-generation/getTask3D.js';

// Poll for task completion
const result = await getTask3D({
  task_id: 'your-task-id'
});

console.log(JSON.stringify(result));
```

**Returns:** Status, model URLs, thumbnail URL, video URL when complete.

---

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
| Video Generation | Google Veo | ~$0.15-$0.40/sec |
| Text to 3D | Meshy | ~5-20 credits |
| Image to 3D | Meshy | ~5-20 credits |
| Retexture 3D | Meshy | ~5-10 credits |
| Rig 3D | Meshy | ~5 credits |
| Animate 3D | Meshy | ~5-10 credits |

## Future Tools

- **Voice Synthesis**: Text-to-speech using ElevenLabs or similar
- **Music Generation**: AI music/audio creation
