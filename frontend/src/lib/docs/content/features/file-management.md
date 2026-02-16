<!--
title: File Management
description: Browse, upload, download, and manage files within projects
order: 9
-->

# File Management

AI Shuffle provides file management capabilities for browsing project directories, uploading files, and managing generated media.

## File Browser

The Files page provides a visual file browser for all your projects:

- **Navigate** directories with breadcrumb navigation
- **Show/hide** hidden files (dotfiles)
- **Upload** files via drag-and-drop or file picker
- **Download** individual files
- **Create** new folders
- **Rename** files and folders
- **Delete** files and folders

### Accessing the File Browser

1. Navigate to **Files** in the sidebar
2. Select a project from the dropdown
3. Browse the directory tree

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

Share files across sessions with shareable links:

```bash
# Create a shared file link
curl -X POST http://localhost:8000/api/v1/shared-files \
  -H "Authorization: Bearer aih_your_key" \
  -H "Content-Type: application/json" \
  -d '{"path": "/project/output/report.pdf"}'

# Access shared file
curl http://localhost:8000/api/v1/shared-files/{share_id}
```

## Generated Media

AI Shuffle can generate images and videos using integrated AI tools. Generated media is accessible via:

- **Images gallery** — View and manage AI-generated images
- **Videos gallery** — View and manage AI-generated videos
- Download links provided in chat messages

```bash
# List generated images
curl http://localhost:8000/api/v1/images \
  -H "Authorization: Bearer aih_your_key"

# List generated videos
curl http://localhost:8000/api/v1/videos \
  -H "Authorization: Bearer aih_your_key"
```
