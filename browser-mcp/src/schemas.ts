/**
 * Tool input schemas.
 *
 * SCHEMA FLOOR (hard requirement for OpenAI strict mode + Gemini compat, so the
 * translator gateway never has to get clever): flat objects only — property
 * types restricted to string / number / boolean / enum / array-of-primitives.
 * NO anyOf / oneOf / allOf / $ref / nested object properties.
 *
 * We hand-author raw JSON Schema (rather than deriving it from zod) precisely so
 * none of those banned constructs can leak in via a code generator. A tiny
 * built-in validator enforces the schemas at call time.
 */

export type JsonSchema = {
  type: "object";
  properties: Record<string, PropSchema>;
  required?: string[];
  additionalProperties: false;
};

export type PropSchema = {
  type: "string" | "number" | "integer" | "boolean" | "array";
  description?: string;
  enum?: string[];
  items?: { type: "string" | "number" | "integer" | "boolean" };
  minimum?: number;
  maximum?: number;
};

export const ACT_KINDS = [
  "click",
  "dblclick",
  "type",
  "fill",
  "key",
  "hover",
  "scroll",
  "select",
  "drag",
  "check",
  "uncheck",
  "accept_dialog",
  "dismiss_dialog",
] as const;

export const statusSchema: JsonSchema = {
  type: "object", additionalProperties: false,
  properties: {
    upload_paths: { type: "array", items: { type: "string" }, description: "Optional: 1-10 absolute paths to check against the runtime upload root. Nothing is uploaded." },
  },
};
export const waitSchema: JsonSchema = {
  type: "object", additionalProperties: false, required: ["condition"],
  properties: {
    condition: { type: "string", enum: ["url", "text", "element"], description: "url, text or element. No selector condition: use element with selector or ref." },
    url: { type: "string", description: "URL to match." },
    text: { type: "string", description: "Text to wait for." },
    ref: { type: "string", description: "Element ref (condition=element)." },
    selector: { type: "string", description: "CSS or Playwright selector (condition=element)." },
    state: { type: "string", enum: ["visible", "hidden", "attached", "detached"] },
    match: { type: "string", enum: ["contains", "exact"] },
    timeout_ms: { type: "integer", minimum: 0, maximum: 15000 },
  },
};

export const navigateSchema: JsonSchema = {
  type: "object",
  properties: {
    url: { type: "string", description: 'URL, or "back" / "forward". Bare domains get https://.' },
  },
  required: ["url"],
  additionalProperties: false,
};

export const snapshotSchema: JsonSchema = {
  type: "object",
  properties: {
    filter: { type: "string", enum: ["interactive", "all"], description: "interactive: controls, headings and landmarks only. Default all." },
    scope_ref: { type: "string", description: "Snapshot only this ref's subtree." },
    max_chars: { type: "integer", description: "Output budget, default 40000.", minimum: 500 },
  },
  additionalProperties: false,
};

export const actSchema: JsonSchema = {
  type: "object",
  properties: {
    kind: { type: "string", enum: [...ACT_KINDS], description: "select, check and uncheck need a ref. accept_dialog and dismiss_dialog answer a pending dialog and take no target." },
    ref: { type: "string", description: "Element ref from the latest snapshot (e12, or f1e3 inside an iframe). Exactly one of ref or coordinate." },
    coordinate: { type: "array", items: { type: "number" }, description: "[x,y] in the pixels of the latest browser_screenshot. Exactly one of ref or coordinate." },
    screenshot_id: { type: "string", description: "Screenshot the coordinates come from (s3). Rejected if a newer screenshot exists." },
    value: { type: "string", description: "Text for type/fill, option value or label for select, answer for a prompt dialog." },
    key: { type: "string", description: 'kind=key: a key or chord, e.g. "Enter", "ArrowDown", "Control+A".' },
    modifiers: { type: "array", items: { type: "string" }, description: 'Held during key, e.g. ["Shift"].' },
    direction: { type: "string", enum: ["up", "down", "left", "right"], description: "kind=scroll, default down. Scroll still needs a ref or coordinate." },
    amount: { type: "integer", description: "kind=scroll pixels, default 400." },
    to_coordinate: { type: "array", items: { type: "number" }, description: "kind=drag drop point [x,y], screenshot pixels." },
  },
  required: ["kind"],
  additionalProperties: false,
};

export const screenshotSchema: JsonSchema = {
  type: "object",
  properties: {
    region: { type: "array", items: { type: "number" }, description: "[x,y,width,height] in page CSS pixels. Omit for the viewport." },
    full_page: { type: "boolean", description: "Capture the whole scrollable page." },
  },
  additionalProperties: false,
};

export const findSchema: JsonSchema = {
  type: "object",
  properties: {
    query: { type: "string", description: 'What to find, e.g. "the checkout button".' },
  },
  required: ["query"],
  additionalProperties: false,
};

export const readTextSchema: JsonSchema = {
  type: "object",
  properties: {
    offset: { type: "integer", minimum: 0, description: "Start character; use next_offset from a cut read." },
  },
  additionalProperties: false,
};

export const evalSchema: JsonSchema = {
  type: "object",
  properties: {
    code: { type: "string", description: "JavaScript; top-level await works. A single expression returns its value; otherwise use return." },
  },
  required: ["code"],
  additionalProperties: false,
};

export const consoleSchema: JsonSchema = {
  type: "object",
  properties: {
    pattern: { type: "string", description: "Required substring or regex." },
    only_errors: { type: "boolean" },
    limit: { type: "integer", description: "Default 20.", minimum: 1 },
    clear: { type: "boolean", description: "Empty the buffer after reading." },
  },
  required: ["pattern"],
  additionalProperties: false,
};

export const networkSchema: JsonSchema = {
  type: "object",
  properties: {
    url_pattern: { type: "string", description: "Substring or regex filter for the list." },
    request_id: { type: "string", description: "Return this request's response body." },
    offset: { type: "integer", minimum: 0, description: "Body start character; use next_offset." },
    limit: { type: "integer", description: "Requests listed, default 20.", minimum: 1 },
    clear: { type: "boolean", description: "Empty the buffer after reading." },
  },
  additionalProperties: false,
};

export const TABS_OPS = ["list", "create", "close", "select"] as const;

export const tabsSchema: JsonSchema = {
  type: "object",
  properties: {
    op: { type: "string", enum: [...TABS_OPS] },
    tab_id: { type: "string", description: "For close and select, e.g. tab2." },
  },
  required: ["op"],
  additionalProperties: false,
};

export const RESIZE_PRESETS = ["mobile", "tablet", "desktop"] as const;

export const resizeSchema: JsonSchema = {
  type: "object",
  properties: {
    preset: { type: "string", enum: [...RESIZE_PRESETS], description: "mobile 375x812, tablet 768x1024, desktop 1280x800." },
    width: { type: "integer", description: "With height; overrides preset.", minimum: 1, maximum: 4096 },
    height: { type: "integer", description: "With width.", minimum: 1, maximum: 4096 },
  },
  additionalProperties: false,
};

export const uploadSchema: JsonSchema = {
  type: "object",
  properties: {
    ref: { type: "string", description: "Ref of a file input." },
    paths: { type: "array", items: { type: "string" }, description: "Absolute paths under the upload root." },
  },
  required: ["ref", "paths"],
  additionalProperties: false,
};

export const batchSchema: JsonSchema = {
  type: "object",
  properties: {
    actions: { type: "array", items: { type: "string" }, description: 'JSON strings {"tool":"browser_...","input":{...}}, max 20, no nested batch.' },
  },
  required: ["actions"],
  additionalProperties: false,
};

export const TOOL_SCHEMAS = {
  browser_status: statusSchema,
  browser_wait: waitSchema,
  browser_navigate: navigateSchema,
  browser_snapshot: snapshotSchema,
  browser_act: actSchema,
  browser_screenshot: screenshotSchema,
  browser_find: findSchema,
  browser_read_text: readTextSchema,
  browser_eval: evalSchema,
  browser_console: consoleSchema,
  browser_network: networkSchema,
  browser_tabs: tabsSchema,
  browser_resize: resizeSchema,
  browser_upload: uploadSchema,
  browser_batch: batchSchema,
} as const;

export interface ValidationResult {
  ok: boolean;
  error?: string;
}

/**
 * Translated Responses tool schemas are strict and require every property, so
 * callers fill optional strings and arrays with empty placeholders. Remove only
 * those placeholders before validation and dispatch. `value` is exempt because
 * an empty value intentionally clears an input in browser_act.
 */
export function normalizeOptionalInput(
  schema: JsonSchema,
  input: Record<string, unknown>,
): Record<string, unknown> {
  const required = new Set(schema.required ?? []);
  const normalized: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(input)) {
    const isOptional = key in schema.properties && !required.has(key);
    const isEmptyPlaceholder =
      (value === "" && key !== "value") || (Array.isArray(value) && value.length === 0);
    if (isOptional && isEmptyPlaceholder) continue;
    normalized[key] = value;
  }

  return normalized;
}

/**
 * Minimal validator for the flat schema subset above. Enforces: unknown keys,
 * required keys, primitive types, enum membership, array item primitives, and
 * numeric bounds. Intentionally does not implement JSON Schema at large.
 */
export function validateInput(schema: JsonSchema, input: unknown): ValidationResult {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    return { ok: false, error: "input must be an object" };
  }
  const obj = input as Record<string, unknown>;

  for (const key of Object.keys(obj)) {
    if (!(key in schema.properties)) {
      return { ok: false, error: `unknown property: ${key}` };
    }
  }
  for (const req of schema.required ?? []) {
    if (!(req in obj) || obj[req] === undefined) {
      return { ok: false, error: `missing required property: ${req}` };
    }
  }
  for (const [key, prop] of Object.entries(schema.properties)) {
    if (!(key in obj) || obj[key] === undefined) continue;
    const v = obj[key];
    const err = validateProp(key, prop, v);
    if (err) return { ok: false, error: err };
  }
  return { ok: true };
}

function validateProp(key: string, prop: PropSchema, v: unknown): string | null {
  switch (prop.type) {
    case "string":
      if (typeof v !== "string") return `${key} must be a string`;
      if (prop.enum && !prop.enum.includes(v)) {
        return `${key} must be one of: ${prop.enum.join(", ")}`;
      }
      return null;
    case "number":
    case "integer":
      if (typeof v !== "number" || Number.isNaN(v)) return `${key} must be a number`;
      if (prop.type === "integer" && !Number.isInteger(v)) return `${key} must be an integer`;
      if (prop.minimum !== undefined && v < prop.minimum) return `${key} must be >= ${prop.minimum}`;
      if (prop.maximum !== undefined && v > prop.maximum) return `${key} must be <= ${prop.maximum}`;
      return null;
    case "boolean":
      if (typeof v !== "boolean") return `${key} must be a boolean`;
      return null;
    case "array":
      if (!Array.isArray(v)) return `${key} must be an array`;
      if (prop.items) {
        for (const el of v) {
          if (prop.items.type === "number" || prop.items.type === "integer") {
            if (typeof el !== "number" || !Number.isFinite(el)) {
              return `${key} items must be finite numbers`;
            }
          } else if (typeof el !== prop.items.type) {
            return `${key} items must be ${prop.items.type}`;
          }
        }
      }
      return null;
    default:
      return `${key} has unsupported type`;
  }
}
