# Windows Local Mode Debug Context

## Problem Summary

AI Hub's local mode on Windows has two issues:

1. **Database folder not being created** - The logs claim the database is initialized at `C:\Users\Maste\AppData\Roaming\ai-hub\db.sqlite` but the folder doesn't exist when checked manually
2. **Workspace folder UI not appearing during setup** - The setup page should show a "Workspace Folder" selection step in local mode, but it's not appearing

## Current Behavior

When running `run_local.bat` on Windows:

```
INFO:     Started server process [21784]
INFO:     Waiting for application startup.
2025-12-06 22:31:40,707 - app.main - INFO - ============================================================
2025-12-06 22:31:40,707 - app.main - INFO - Starting AI Hub v4.0.0
2025-12-06 22:31:40,707 - app.main - INFO - ============================================================
2025-12-06 22:31:40,707 - app.db.database - INFO - Initializing database at C:\Users\Maste\AppData\Roaming\ai-hub\db.sqlite
2025-12-06 22:31:40,709 - app.db.database - INFO - Database initialized successfully
2025-12-06 22:31:40,711 - app.main - WARNING - Claude CLI: Not authenticated - run 'claude login' in container
2025-12-06 22:31:40,713 - app.main - INFO - Admin user: admin
2025-12-06 22:31:40,713 - app.main - INFO - API docs: http://localhost:8000/docs
```

However, when checking the filesystem:
```powershell
PS D:\Development\ai-hub> dir "C:\Users\Maste\AppData\Roaming\ai-hub"
dir : Cannot find path 'C:\Users\Maste\AppData\Roaming\ai-hub' because it does not exist.
```

The admin user "admin" exists, meaning a database IS somewhere - just not where the logs say.

## Expected Behavior

1. Database should be created at `%APPDATA%\ai-hub\db.sqlite` (e.g., `C:\Users\Maste\AppData\Roaming\ai-hub\db.sqlite`)
2. On first run, setup page should show workspace folder selection before admin account creation
3. Settings page should show editable workspace path (not Docker mode message)

## Key Files to Investigate

### 1. Platform Detection (`app/core/platform.py`)

This module detects if running in Docker or local mode:

```python
def detect_deployment_mode() -> DeploymentMode:
    # Explicit override via environment variable
    local_mode_env = os.environ.get("LOCAL_MODE", "").lower()
    if local_mode_env in ("true", "1", "yes"):
        return DeploymentMode.LOCAL

    # Windows and macOS are always local mode
    if sys.platform in ("win32", "darwin"):
        return DeploymentMode.LOCAL
    # ... Docker checks for Linux
```

```python
def get_app_data_dir() -> Path:
    if detect_deployment_mode() == DeploymentMode.DOCKER:
        return Path("/data")

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "ai-hub"
        return Path.home() / "AppData" / "Roaming" / "ai-hub"
    # ...
```

**Debug this**: Add print statements to verify:
- What `os.environ.get("APPDATA")` returns
- What `Path.home()` returns
- What `detect_deployment_mode()` returns
- What `get_app_data_dir()` returns

### 2. Config Settings (`app/core/config.py`)

The Settings class uses platform.py to determine paths:

```python
def _get_default_data_dir() -> Path:
    from app.core.platform import get_app_data_dir
    return get_app_data_dir()

class Settings(BaseSettings):
    data_dir: Optional[Path] = None

    def model_post_init(self, __context) -> None:
        if self.data_dir is None:
            object.__setattr__(self, 'data_dir', _get_default_data_dir())

    @property
    def db_path(self) -> Path:
        return self.effective_data_dir / "db.sqlite"
```

### 3. Database Initialization (`app/db/database.py`)

```python
def init_database():
    logger.info(f"Initializing database at {settings.db_path}")

    # Ensure data directory exists
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    with get_db() as conn:
        # ... creates tables

    logger.info("Database initialized successfully")
```

**Key question**: If `mkdir()` was called and succeeded, why doesn't the folder exist?

### 4. Workspace API (`app/api/system.py`)

```python
@router.get("/api/v1/workspace/config")
async def get_workspace_config() -> WorkspaceConfigResponse:
    from app.core.platform import get_default_workspace_dir, is_local_mode

    return WorkspaceConfigResponse(
        workspace_path=current_path,
        is_configured=is_configured,
        is_local_mode=is_local_mode(),  # <-- This must return True on Windows
        default_path=default_path,
        exists=Path(current_path).exists()
    )
```

**Debug this**: Visit `http://127.0.0.1:8000/api/v1/workspace/config` in browser and check what `is_local_mode` returns.

### 5. Frontend Setup Page (`frontend/src/routes/setup/+page.svelte`)

The workspace step only shows if `workspaceConfig?.is_local_mode` is true:

```svelte
$: isLocalMode = workspaceConfig?.is_local_mode ?? false;
$: showWorkspaceStep = isLocalMode && currentStep === 1;

{#if showWorkspaceStep}
    <!-- Workspace folder selection UI -->
{:else}
    <!-- Admin account form -->
{/if}
```

## Debug Steps

### Step 1: Check API Response

Open browser to `http://127.0.0.1:8000/api/v1/workspace/config` and verify:
- `is_local_mode` should be `true`
- `workspace_path` and `default_path` should be Windows paths

### Step 2: Add Debug Logging

In `app/core/platform.py`, add at the top of `get_app_data_dir()`:

```python
def get_app_data_dir() -> Path:
    mode = detect_deployment_mode()
    print(f"DEBUG: deployment_mode = {mode}")
    print(f"DEBUG: sys.platform = {sys.platform}")
    print(f"DEBUG: APPDATA = {os.environ.get('APPDATA')}")
    print(f"DEBUG: Path.home() = {Path.home()}")

    if mode == DeploymentMode.DOCKER:
        return Path("/data")
    # ... rest of function
```

### Step 3: Find the Actual Database

Search for where the database actually is:

```powershell
# Search entire D: drive
Get-ChildItem -Path D:\ -Recurse -Filter "db.sqlite" -ErrorAction SilentlyContinue

# Search C: drive user folder
Get-ChildItem -Path C:\Users\Maste -Recurse -Filter "db.sqlite" -ErrorAction SilentlyContinue

# Check if it's in the project directory
dir "D:\Development\ai-hub\*.sqlite"
dir "D:\Development\ai-hub\data"
```

### Step 4: Check Browser Console

Open browser DevTools (F12) on the setup page and check:
- Network tab: Is `/api/v1/workspace/config` being called? What does it return?
- Console tab: Any JavaScript errors?

### Step 5: Verify Environment Variables

In `run_local.bat`, these are set:
```batch
set LOCAL_MODE=true
set HOST=127.0.0.1
set PORT=8000
set COOKIE_SECURE=false
```

Verify they're being passed to Python by adding to `app/main.py` lifespan:
```python
logger.info(f"LOCAL_MODE env: {os.environ.get('LOCAL_MODE')}")
```

## Potential Root Causes

1. **Path resolution issue**: `Path()` might be resolving relative to CWD instead of creating absolute path
2. **Environment variable not propagating**: `LOCAL_MODE=true` might not be reaching the Python process
3. **Pydantic Settings loading before env vars**: Settings might be initialized before environment is set up
4. **Virtual environment isolation**: The venv might have different environment than the batch script
5. **mkdir silent failure**: `mkdir()` might be failing silently due to permissions or path issues

## Files Changed Recently

These files were modified to add local mode support - compare with the repo to ensure changes are present:

1. `app/core/platform.py` - Platform detection (should detect Windows as local)
2. `app/core/config.py` - Dynamic path configuration
3. `app/main.py` - Added deployment logging at startup
4. `app/api/system.py` - Workspace config endpoints
5. `app/db/database.py` - Added system_settings table
6. `frontend/src/routes/setup/+page.svelte` - Workspace selection UI
7. `frontend/src/routes/settings/+page.svelte` - Workspace settings UI
8. `frontend/src/lib/api/auth.ts` - Workspace API functions
9. `frontend/src/lib/api/client.ts` - TypeScript types

## Quick Fix Attempt

If the database is in the wrong location, you can:

1. Find it: `Get-ChildItem -Path D:\ -Recurse -Filter "*.sqlite" -ErrorAction SilentlyContinue`
2. Delete it to start fresh
3. Ensure latest code is deployed
4. Rebuild frontend: `cd frontend && npm run build && xcopy /e /i /y build ..\app\static`
5. Run again with `run_local.bat`

## Contact

This document was created to provide context for debugging AI Hub local mode on Windows. The Docker version works correctly - the issue is specific to Windows local mode execution.
