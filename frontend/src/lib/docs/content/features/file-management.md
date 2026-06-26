<!--
title: File Management
description: Browse, upload, download, and manage files within projects
order: 9
-->

# File Management

AI Shuffle provides file management capabilities for browsing project directories, uploading files, and managing generated media.

## File Browser

The Files page provides a visual file browser for your workspace:

- **Navigate** directories with breadcrumb navigation
- **Show/hide** hidden files (dotfiles)
- **Upload** files via drag-and-drop or file picker
- **Download** individual files
- **Create** new folders
- **Rename** files and folders
- **Delete** files and folders

### Accessing the File Browser

1. Navigate to **Files** in the sidebar
2. Browse the workspace directory tree starting from the workspace root

## File Uploads

Upload files to any project directory:

### Via the UI
- Drag and drop files onto the file browser
- Or click the upload button and select files

### Via the API

```bash
curl -X POST http://localhost:8000/api/v1/projects/proj_abc/upload \
  -H "Authorization: Bearer aih_your_key" \
  -F "file=@document.pdf" \
  -F "path=uploads/"
```

Files can be associated with specific sessions or worktrees.

## File Downloads

### Via the API

```bash
curl "http://localhost:8000/api/v1/projects/proj_abc/files/download?path=src/app.py" \
  -H "Authorization: Bearer aih_your_key" \
  -o app.py
```

## Shared Files

The agent can make files in the workspace available for download. These appear in the chat UI as download cards and are served by filename:

```bash
# Download a shared file by name
curl http://localhost:8000/api/files/report.pdf \
  -H "Authorization: Bearer aih_your_key" \
  -o report.pdf
```

## Generated Media

AI Shuffle can generate images and videos using integrated AI tools. Generated media appears inline in chat messages as download cards rather than in a dedicated gallery. The files are served from the workspace and can also be listed via the API:

```bash
# List generated images
curl http://localhost:8000/api/generated-images/ \
  -H "Authorization: Bearer aih_your_key"

# List generated videos
curl http://localhost:8000/api/generated-videos/ \
  -H "Authorization: Bearer aih_your_key"
```
