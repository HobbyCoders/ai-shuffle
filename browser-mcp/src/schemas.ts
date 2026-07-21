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
] as const;

export const navigateSchema: JsonSchema = {
  type: "object",
  properties: {
    url: {
      type: "string",
      description: 'URL to open, or "back" / "forward" to move through history. Bare domains get https://.',
    },
  },
  required: ["url"],
  additionalProperties: false,
};

export const snapshotSchema: JsonSchema = {
  type: "object",
  properties: {
    filter: {
      type: "string",
      enum: ["interactive", "all"],
      description: 'interactive = controls + headings/landmarks (default all).',
    },
    scope_ref: {
      type: "string",
      description: "Re-snapshot only the subtree under this ref (e.g. e12).",
    },
    max_chars: {
      type: "integer",
      description: "Truncation budget (default 40000).",
      minimum: 500,
    },
  },
  additionalProperties: false,
};

export const actSchema: JsonSchema = {
  type: "object",
  properties: {
    kind: {
      type: "string",
      enum: [...ACT_KINDS],
      description: "Action to perform.",
    },
    ref: {
      type: "string",
      description: "Target element ref (e.g. e12). Provide exactly one of ref or coordinate.",
    },
    coordinate: {
      type: "array",
      items: { type: "number" },
      description: "Target [x,y] in the last screenshot's pixel space. Exactly one of ref or coordinate.",
    },
    value: {
      type: "string",
      description: "Text for type/fill, or option value for select.",
    },
    key: {
      type: "string",
      description: 'Key name for kind=key (e.g. "Enter", "ArrowDown").',
    },
    modifiers: {
      type: "array",
      items: { type: "string" },
      description: 'Modifier keys for kind=key (e.g. ["Control","Shift"]).',
    },
    direction: {
      type: "string",
      enum: ["up", "down", "left", "right"],
      description: "Scroll direction for kind=scroll.",
    },
    amount: {
      type: "integer",
      description: "Scroll distance in pixels for kind=scroll.",
    },
    to_coordinate: {
      type: "array",
      items: { type: "number" },
      description: "Drop point [x,y] for kind=drag.",
    },
  },
  required: ["kind"],
  additionalProperties: false,
};

export const screenshotSchema: JsonSchema = {
  type: "object",
  properties: {
    region: {
      type: "array",
      items: { type: "number" },
      description: "Crop [x,y,width,height] in page pixels. Omit for the viewport.",
    },
    full_page: {
      type: "boolean",
      description: "Capture the full scrollable page instead of the viewport.",
    },
  },
  additionalProperties: false,
};

export const TOOL_SCHEMAS = {
  browser_navigate: navigateSchema,
  browser_snapshot: snapshotSchema,
  browser_act: actSchema,
  browser_screenshot: screenshotSchema,
} as const;

export interface ValidationResult {
  ok: boolean;
  error?: string;
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
