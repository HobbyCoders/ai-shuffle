<!--
title: Notes
description: Global notes with tags, search, chat autocomplete and file attachments
order: 16
-->

# Notes API

Endpoints for notes: free-form markdown notes for ideas, bugs and planning, with tags, pinning and file attachments. Notes are global, not tied to a project or session.

Every endpoint on this page needs authentication. A note belongs to whoever wrote it. An API key sees, edits and deletes only its own notes; any other note, with its attachments, returns `404` as if it did not exist, and the list, tags and autocomplete leave it out. The owner sees every note, including those API keys wrote. A note reference in a prompt (`@Notes/note-<id>`) is expanded the same way: from an API key's session only the key's own notes are filled in, and other references stay as typed. Workers in a [network](/docs/features/worker-network) keep the same owner on every device.

Each note is also saved on the server as a markdown file. If that file is edited directly on disk, the change is picked up the next time the note is read with `GET /api/v1/notes/{note_id}`.

## Notes

### GET /api/v1/notes

List notes, pinned notes first, then most recently updated.

**Auth:** Required

| Query | Default | Notes |
|-------|---------|-------|
| `search` | none | Case-insensitive substring match on title or content |
| `tag` | none | Only notes carrying this exact tag |
| `limit` | `100` | 1 to 500 |
| `offset` | `0` | 0 or more |

```bash
curl "http://localhost:8000/api/v1/notes?tag=bug&limit=20" \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "note-3f9a1c2b7d4e",
    "title": "Login bug",
    "content": "Session cookie is dropped after refresh.",
    "tags": ["bug"],
    "is_pinned": true,
    "file_path": "notes/login-bug.md",
    "created_at": "2026-09-20T10:15:00Z",
    "updated_at": "2026-09-21T08:02:00Z"
  }
]
```

### POST /api/v1/notes

Create a note. Returns `201` with the new note.

**Auth:** Required

JSON body:

| Field | Required | Notes |
|-------|----------|-------|
| `title` | Yes | 1 to 255 characters |
| `content` | No | Markdown text. Default `""` |
| `tags` | No | List of tag names. Default `[]` |
| `is_pinned` | No | Default `false` |

```bash
curl -X POST http://localhost:8000/api/v1/notes \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"title": "Release checklist", "content": "- bump version", "tags": ["planning"]}'
```

### GET /api/v1/notes/tags

List every tag used on any note, with how many notes carry it, most used first.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/notes/tags \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  { "name": "bug", "color": null, "count": 4 },
  { "name": "planning", "color": null, "count": 2 }
]
```

### GET /api/v1/notes/autocomplete

A short list of notes for referencing a note from chat, pinned first, then most recently updated.

**Auth:** Required

| Query | Default | Notes |
|-------|---------|-------|
| `search` | none | Case-insensitive substring match on title or content |
| `limit` | `20` | 1 to 50 |

```bash
curl "http://localhost:8000/api/v1/notes/autocomplete?search=release" \
  -H "Authorization: Bearer aih_your_key"
```

Each item has `id`, `title`, `preview` (the first 100 characters of the content, with `...` when longer), `tags`, `file_path` and `absolute_path` (the note file's full path on the server).

### GET /api/v1/notes/{note_id}

Get one note. Returns `404` if it does not exist.

**Auth:** Required

```bash
curl http://localhost:8000/api/v1/notes/note-3f9a1c2b7d4e \
  -H "Authorization: Bearer aih_your_key"
```

### PATCH /api/v1/notes/{note_id}

Update a note. Only the fields you send change; `tags` replaces the whole list. Returns the updated note, or `404` if it does not exist.

**Auth:** Required

JSON body, all optional: `title` (1 to 255 characters), `content`, `tags`, `is_pinned`.

```bash
curl -X PATCH http://localhost:8000/api/v1/notes/note-3f9a1c2b7d4e \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"is_pinned": false, "tags": ["bug", "fixed"]}'
```

### DELETE /api/v1/notes/{note_id}

Delete a note, its markdown file and all its attachments. Returns `204`, or `404` if it does not exist.

**Auth:** Required

```bash
curl -X DELETE http://localhost:8000/api/v1/notes/note-3f9a1c2b7d4e \
  -H "Authorization: Bearer aih_your_key"
```

## Attachments

Files uploaded to a note, which the note body can reference. All attachment endpoints need **Auth:** Required.

### GET /api/v1/notes/{note_id}/attachments

List a note's attachments, oldest first. Returns `404` if the note does not exist.

```bash
curl http://localhost:8000/api/v1/notes/note-3f9a1c2b7d4e/attachments \
  -H "Authorization: Bearer aih_your_key"
```

**Response:**
```json
[
  {
    "id": "natt-8c21e0f4a9b3",
    "note_id": "note-3f9a1c2b7d4e",
    "filename": "screenshot.png",
    "content_type": "image/png",
    "size_bytes": 48213,
    "created_at": "2026-09-21T08:05:00Z"
  }
]
```

### POST /api/v1/notes/{note_id}/attachments

Upload one file to a note. The request is `multipart/form-data` with the file in a required field named `file`. Returns `201` with the attachment (same fields as the list above).

- Maximum size is 25 MB; a larger file returns `413`.
- Any file type is accepted. `content_type` is taken from the uploaded part.
- Only the base name of the uploaded filename is kept. An empty name, `.` or `..` returns `400`.
- If the note already has a file with that name, `_1`, `_2`, ... is added before the extension.
- Returns `404` if the note does not exist.

```bash
curl -X POST http://localhost:8000/api/v1/notes/note-3f9a1c2b7d4e/attachments \
  -H "Authorization: Bearer aih_your_key" \
  -F "file=@screenshot.png"
```

### GET /api/v1/notes/{note_id}/attachments/{attachment_id}/download

Download an attachment's bytes. The response uses the stored content type (or `application/octet-stream`) and a `Content-Disposition: attachment` header with the filename. Returns `404` if the attachment does not exist or belongs to another note.

```bash
curl -OJ http://localhost:8000/api/v1/notes/note-3f9a1c2b7d4e/attachments/natt-8c21e0f4a9b3/download \
  -H "Authorization: Bearer aih_your_key"
```

### DELETE /api/v1/notes/{note_id}/attachments/{attachment_id}

Delete an attachment. Returns `204`, or `404` if the attachment does not exist or belongs to another note.

```bash
curl -X DELETE http://localhost:8000/api/v1/notes/note-3f9a1c2b7d4e/attachments/natt-8c21e0f4a9b3 \
  -H "Authorization: Bearer aih_your_key"
```
