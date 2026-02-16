<!--
title: Notes
description: Global notes for ideas, bugs, and planning
order: 8
-->

# Notes

Notes provide a simple, global space for capturing ideas, tracking bugs, and planning work. They support markdown formatting, tags, and search.

## Features

- **Markdown** — Write notes in markdown with live preview
- **Tags** — Organize notes with custom tags
- **Pinning** — Pin important notes to the top
- **Search** — Find notes by title or content
- **Auto-save** — Changes save automatically with debouncing

## Using Notes

### Creating a Note

1. Navigate to **Notes** in the sidebar
2. Click **New Note** (+ button)
3. Enter a title and content
4. Add tags for organization
5. Notes auto-save as you type

### Organizing with Tags

- Create tags to categorize notes (e.g., "bug", "idea", "todo")
- Filter the notes list by tag
- A note can have multiple tags

### Markdown Preview

Toggle between edit and preview mode to see your markdown rendered.

## API Access

```bash
# List notes
curl http://localhost:8000/api/v1/notes \
  -H "Authorization: Bearer aih_your_key"

# Create a note
curl -X POST http://localhost:8000/api/v1/notes \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "API Redesign Ideas",
    "content": "## Goals\n- Simplify auth flow\n- Add pagination everywhere",
    "tags": ["idea", "api"],
    "pinned": true
  }'
```
