"""
PostgreSQL DDL schema — creates all 50 tables, indexes, and FTS infrastructure.

This defines the PostgreSQL schema (all tables, indexes, and FTS
infrastructure) corresponding to ``database.py._create_schema()``
plus all column migrations from ``_run_column_migrations()``.

Usage::

    from app.db.schema_postgres import create_postgres_schema
    with get_db() as conn:
        cursor = conn.cursor()
        create_postgres_schema(cursor)
"""

import logging

logger = logging.getLogger("app.db.schema_postgres")

# Must match database.py SCHEMA_VERSION
SCHEMA_VERSION = 62


def create_postgres_schema(cursor) -> None:
    """Create all PostgreSQL tables, indexes, triggers, and seed data.

    Parameters
    ----------
    cursor : psycopg2 cursor
        An open cursor on a PostgreSQL connection.  The caller is
        responsible for committing the transaction.
    """

    # ------------------------------------------------------------------
    # 1. Schema version
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY
        )
    """)
    cursor.execute("DELETE FROM schema_version")
    cursor.execute(
        "INSERT INTO schema_version (version) VALUES (%s)",
        (SCHEMA_VERSION,),
    )

    # ------------------------------------------------------------------
    # 2. System settings (key-value store for global configuration)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 3. Admin user (single user system)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS admin (
            id INTEGER PRIMARY KEY DEFAULT 1,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            CHECK (id = 1)
        )
    """)

    # ------------------------------------------------------------------
    # 4. Agent profiles
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            config JSONB NOT NULL,
            mcp_tools JSONB DEFAULT '[]',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 5. Projects (workspaces)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            path TEXT NOT NULL,
            settings JSONB DEFAULT '{}',
            instance_id TEXT,
            link_group_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_projects_link_group ON projects(link_group_id)")

    # ------------------------------------------------------------------
    # 6. API users (for programmatic access with isolated workspaces)
    #    Placed before sessions because sessions has FK to api_users.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            username TEXT,
            password_hash TEXT,
            api_key_hash TEXT NOT NULL,
            project_id TEXT,
            profile_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            web_login_allowed BOOLEAN DEFAULT TRUE,
            description TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_used_at TIMESTAMP WITH TIME ZONE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # ------------------------------------------------------------------
    # 7. Git repositories (tracks git state per project)
    #    Placed before worktrees because worktrees has FK to git_repositories.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS git_repositories (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE,
            remote_url TEXT,
            default_branch TEXT DEFAULT 'main',
            github_repo_name TEXT,
            last_synced_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 8. Worktrees (for parallel session work on branches)
    #    Placed before sessions because sessions has FK to worktrees.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS worktrees (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            session_id TEXT,
            branch_name TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_branch TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (repository_id) REFERENCES git_repositories(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 9. Sessions (conversations)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            profile_id TEXT,
            api_user_id TEXT,
            sdk_session_id TEXT,
            worktree_id TEXT,
            worktree_branch TEXT,
            worktree_path TEXT,
            cloud_branch TEXT,
            automation_id TEXT,
            parked_task_kind TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            is_favorite BOOLEAN DEFAULT FALSE,
            total_cost_usd DOUBLE PRECISION DEFAULT 0,
            total_tokens_in INTEGER DEFAULT 0,
            total_tokens_out INTEGER DEFAULT 0,
            last_input_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            parent_session_id TEXT,
            fork_point_message_index INTEGER,
            instance_id TEXT,
            launched_model TEXT,
            v2_stream BOOLEAN NOT NULL DEFAULT TRUE,
            project_v2_id TEXT,
            branch TEXT,
            placement JSONB,
            working_set_ref TEXT,
            context_usage_json JSONB,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL,
            FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
        )
    """)

    # ------------------------------------------------------------------
    # 10. Session messages (with sync support)
    #     Includes content_tsv tsvector column for FTS.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_messages (
            id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input JSONB,
            metadata JSONB,
            profile_id TEXT,
            jsonl_uuid TEXT,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            content_tsv tsvector,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 11. FTS infrastructure (tsvector column + GIN index + triggers)
    # ------------------------------------------------------------------

    # GIN index on the tsvector column
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_messages_fts
        ON session_messages USING gin(content_tsv)
    """)

    # Trigger function: populate content_tsv on INSERT / UPDATE
    cursor.execute("""
        DO $$ BEGIN
            CREATE OR REPLACE FUNCTION session_messages_fts_update()
            RETURNS trigger AS $fn$
            BEGIN
                IF NEW.role IN ('user', 'assistant') AND NEW.content != '' THEN
                    NEW.content_tsv := to_tsvector('english', NEW.content);
                ELSE
                    NEW.content_tsv := NULL;
                END IF;
                RETURN NEW;
            END;
            $fn$ LANGUAGE plpgsql;
        END $$
    """)

    # Trigger on session_messages (BEFORE INSERT OR UPDATE)
    cursor.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_trigger WHERE tgname = 'trg_session_messages_fts'
            ) THEN
                CREATE TRIGGER trg_session_messages_fts
                BEFORE INSERT OR UPDATE OF content ON session_messages
                FOR EACH ROW
                EXECUTE FUNCTION session_messages_fts_update();
            END IF;
        END $$
    """)

    # ------------------------------------------------------------------
    # 12. Sync log for tracking changes (polling fallback)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id BIGSERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            data JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 13. Auth sessions (login tokens)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS auth_sessions (
            token TEXT PRIMARY KEY,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 14. Usage tracking
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id BIGSERIAL PRIMARY KEY,
            session_id TEXT,
            profile_id TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd DOUBLE PRECISION,
            duration_ms INTEGER,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 15. API user profiles junction table (many-to-many)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_profiles (
            api_user_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (api_user_id, profile_id),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 16. Login attempts tracking for brute force protection
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            id BIGSERIAL PRIMARY KEY,
            ip_address TEXT NOT NULL,
            username TEXT,
            success BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 17. Account lockout tracking
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS account_lockouts (
            id BIGSERIAL PRIMARY KEY,
            ip_address TEXT,
            username TEXT,
            locked_until TIMESTAMP WITH TIME ZONE NOT NULL,
            reason TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 18. API key web sessions
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_key_sessions (
            token TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 19. User preferences
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_preferences (
            id BIGSERIAL PRIMARY KEY,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value JSONB,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(user_type, user_id, key)
        )
    """)

    # ------------------------------------------------------------------
    # 20. Global subagents
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subagents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tools JSONB,
            model TEXT,
            skills JSONB,
            memory TEXT,
            mcp_servers JSONB,
            is_builtin BOOLEAN DEFAULT FALSE,
            default_name TEXT,
            default_description TEXT,
            default_prompt TEXT,
            default_tools JSONB,
            default_model TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 21. Permission rules for tool access control
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS permission_rules (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            tool_name TEXT NOT NULL,
            tool_pattern TEXT,
            decision TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 22. Tags for organizing sessions
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6366f1',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 23. Session-tag junction table
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_tags (
            session_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (session_id, tag_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 24. Session templates for starter prompts
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            prompt TEXT NOT NULL,
            profile_id TEXT,
            icon TEXT,
            category TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        )
    """)

    # ------------------------------------------------------------------
    # 25. Webhooks for external integrations
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS webhooks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret TEXT,
            events JSONB NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_triggered_at TIMESTAMP WITH TIME ZONE,
            failure_count INTEGER DEFAULT 0
        )
    """)

    # ------------------------------------------------------------------
    # 26. Audit log for security events
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            user_type TEXT DEFAULT 'admin',
            event_type TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            details JSONB,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 27. Rate limit configurations
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            requests_per_minute INTEGER DEFAULT 20,
            requests_per_hour INTEGER DEFAULT 200,
            requests_per_day INTEGER DEFAULT 1000,
            concurrent_requests INTEGER DEFAULT 3,
            priority INTEGER DEFAULT 0,
            is_unlimited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (api_key_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 28. Request log for rate limit tracking
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            endpoint TEXT,
            timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            duration_ms INTEGER,
            status TEXT DEFAULT 'success'
        )
    """)

    # ------------------------------------------------------------------
    # 29. Notes (global, not project-scoped)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            tags JSONB DEFAULT '[]',
            is_pinned BOOLEAN DEFAULT FALSE,
            file_path TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # Note attachments — files uploaded to a note and referenceable via @ in the
    # note body. Deleting the note cascades to its attachments.
    # ``data`` holds the file bytes so attachments live in the shared database
    # rather than only on the uploading machine's local disk. ``file_path`` is a
    # machine-local cache path; the bytes in ``data`` are the source of truth and
    # let other installs sharing this database serve the attachment.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS note_attachments (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER DEFAULT 0,
            data BYTEA,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_note_attachments_note ON note_attachments(note_id)"
    )

    # ------------------------------------------------------------------
    # 30. Queued messages (server-side message queue for streaming)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS queued_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            profile_id TEXT,
            project_id TEXT,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 31. Project hooks (Claude Code event handling)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_hooks (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            name TEXT NOT NULL,
            event TEXT NOT NULL,
            hook_type TEXT NOT NULL,
            command TEXT,
            timeout INTEGER DEFAULT 30000,
            url TEXT,
            secret TEXT,
            enabled BOOLEAN DEFAULT TRUE,
            tool_matcher TEXT,
            message_template TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_triggered_at TIMESTAMP WITH TIME ZONE,
            failure_count INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 32. Credential policies
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS credential_policies (
            id TEXT PRIMARY KEY,
            policy TEXT NOT NULL DEFAULT 'admin_provided',
            description TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 33. Per-user credentials (encrypted API keys, GitHub PAT)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_credentials (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        )
    """)

    # ------------------------------------------------------------------
    # 34. Per-user GitHub configuration
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_user_github_config (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL UNIQUE,
            github_username TEXT,
            github_avatar_url TEXT,
            default_repo TEXT,
            default_branch TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 35. Per-user credential policy overrides
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_credential_policies (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            policy TEXT NOT NULL DEFAULT 'optional',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        )
    """)

    # ------------------------------------------------------------------
    # 36. Mobile devices
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mobile_devices (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL UNIQUE,
            device_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            api_key_hash TEXT NOT NULL,
            fcm_token TEXT,
            unifiedpush_endpoint TEXT,
            capabilities JSONB DEFAULT '{}',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_seen TIMESTAMP WITH TIME ZONE
        )
    """)

    # ------------------------------------------------------------------
    # 36b. Installs (cross-install registry for a shared database)
    #      Each AI Shuffle install self-registers a row keyed by its
    #      instance_id so peers can list every install, show which device
    #      owns a session, and relay live control to the owning install.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS installs (
            id TEXT PRIMARY KEY,
            device_name TEXT NOT NULL,
            base_url TEXT,
            platform TEXT,
            version TEXT,
            capabilities JSONB DEFAULT '{}',
            last_seen TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_installs_last_seen ON installs(last_seen DESC)"
    )

    # ------------------------------------------------------------------
    # 36b. Claude accounts (v52). Multiple Anthropic (Claude Code OAuth)
    #      accounts with in-app switching. The account LIST is shared across
    #      installs; the ACTIVE pointer is per-install and lives in
    #      system_settings under key ``claude_active_account:{instance_id}``
    #      (same pattern as ``workspace_path:{instance_id}``) — so there is
    #      deliberately NO is_active column here. ``credentials`` holds an
    #      ``enc:``-prefixed Fernet blob of the full claudeAiOauth JSON;
    #      ``expires_at`` is a plaintext copy for display without decrypting.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS claude_accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            subscription_type TEXT,
            credentials TEXT,
            credential_fingerprint TEXT,
            expires_at BIGINT,
            has_refresh_token BOOLEAN DEFAULT TRUE,
            sync_pending BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_used_at TIMESTAMP WITH TIME ZONE
        )
    """)

    # ------------------------------------------------------------------
    # 36c. Usage snapshots (v53). Per-install Claude usage-limits snapshot,
    #      keyed by instance_id. The 5-hour Claude usage window is PER-ACCOUNT,
    #      and on a shared database two installs may run different Claude
    #      accounts — so each install persists its own snapshot here and the
    #      usage endpoint serves every install's snapshot (joined to the
    #      installs registry for device_name/online). ``windows`` is the
    #      normalized window list; ``account_*`` records which Claude account
    #      produced the reading (Nones in legacy single-account mode).
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_snapshots (
            instance_id TEXT PRIMARY KEY,
            windows JSONB NOT NULL DEFAULT '[]',
            available BOOLEAN DEFAULT FALSE,
            generation BIGINT,
            fetched_at BIGINT,
            account_id TEXT,
            account_email TEXT,
            account_name TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 36d. Background tasks (v55). Durable per-task lifecycle snapshots —
    #      WRITE-ONLY in Phase 1 of the lifecycle unification: task_registry
    #      mirrors start/update/complete/unpark transitions here through
    #      app.core.task_persistence (coalescing async writer, kill switch
    #      settings.durable_task_state). No reader depends on these rows yet;
    #      boot reconcile marks this install's stale non-terminal rows killed.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS background_tasks (
            task_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            instance_id TEXT,
            kind TEXT,
            is_sdk_background BOOLEAN DEFAULT FALSE,
            tool_use_id TEXT,
            title TEXT,
            status TEXT,
            unparked BOOLEAN DEFAULT FALSE,
            terminal_notified BOOLEAN DEFAULT FALSE,
            started_at TIMESTAMP WITH TIME ZONE,
            updated_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (task_id, session_id)
        )
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_background_tasks_instance_status
        ON background_tasks(instance_id, status)
    """)

    # ------------------------------------------------------------------
    # 37. Pending actions (waiting for mobile confirmation)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            params JSONB DEFAULT '{}',
            security_level TEXT DEFAULT 'normal',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 38. Trusted actions (auto-approve patterns)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trusted_actions (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            action_pattern TEXT NOT NULL,
            auto_approve BOOLEAN DEFAULT TRUE,
            require_preview BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 39. Mobile schedules (proactive notifications)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mobile_schedules (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            trigger_time TIMESTAMP WITH TIME ZONE,
            hour INTEGER,
            minute INTEGER,
            params JSONB DEFAULT '{}',
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            last_triggered_at TIMESTAMP WITH TIME ZONE,
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 40. Connectors (external messaging platform integrations)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS connectors (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            api_user_id TEXT,
            config JSONB NOT NULL DEFAULT '{}',
            events JSONB NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'disconnected',
            status_message TEXT,
            last_connected_at TIMESTAMP WITH TIME ZONE,
            last_error TEXT,
            last_event_at TIMESTAMP WITH TIME ZONE,
            failure_count INTEGER DEFAULT 0,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL
        )
    """)

    # ------------------------------------------------------------------
    # 41. Connector event log
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS connector_event_log (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'outbound',
            payload JSONB,
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 42. Connector sessions (maps platform chats to AI sessions)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS connector_sessions (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            platform_chat_id TEXT NOT NULL,
            platform_user_id TEXT,
            platform_user_name TEXT,
            session_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL,
            UNIQUE(connector_id, platform_chat_id)
        )
    """)

    # ------------------------------------------------------------------
    # 43. Automations (scheduled and event-driven AI tasks)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            trigger_type TEXT NOT NULL,
            trigger_config JSONB NOT NULL DEFAULT '{}',
            prompt TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            project_id TEXT,
            output_config JSONB NOT NULL DEFAULT '{"type": "session"}',
            max_turns INTEGER DEFAULT 1,
            timeout_seconds INTEGER DEFAULT 300,
            max_cost_per_run DOUBLE PRECISION,
            max_cost_per_day DOUBLE PRECISION,
            max_cost_total DOUBLE PRECISION,
            max_runs_per_day INTEGER,
            total_cost_used DOUBLE PRECISION DEFAULT 0.0,
            is_enabled BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'idle',
            last_run_at TIMESTAMP WITH TIME ZONE,
            last_error TEXT,
            run_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            requires_approval BOOLEAN DEFAULT FALSE,
            action_steps TEXT DEFAULT NULL,
            triggers TEXT DEFAULT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE RESTRICT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
        )
    """)

    # ------------------------------------------------------------------
    # 44. Automation runs
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automation_runs (
            id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            trigger_data JSONB,
            session_id TEXT,
            prompt_rendered TEXT,
            response TEXT,
            error TEXT,
            cost_usd DOUBLE PRECISION DEFAULT 0.0,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            output_status TEXT DEFAULT 'pending',
            output_error TEXT,
            started_at TIMESTAMP WITH TIME ZONE,
            completed_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
        )
    """)

    # ------------------------------------------------------------------
    # 45. Automation approvals
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automation_approvals (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            prompt_preview TEXT,
            decided_by TEXT,
            decided_at TIMESTAMP WITH TIME ZONE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            FOREIGN KEY (run_id) REFERENCES automation_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE
        )
    """)

    # ------------------------------------------------------------------
    # 45b. Automation fire claims (v50) — cross-install double-fire guard
    # ------------------------------------------------------------------
    # Every install schedules every enabled automation and the runner
    # election deliberately fails open, so one scheduled tick can be
    # attempted on more than one install. The primary key makes claiming a
    # tick atomic: exactly one INSERT ... ON CONFLICT DO NOTHING wins per
    # (automation_id, fire_bucket). No FK on automation_id — claims are
    # short-lived bookkeeping (pruned after ~7 days) and must not block
    # automation deletion on another install mid-claim.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS automation_fires (
            automation_id TEXT NOT NULL,
            fire_bucket TEXT NOT NULL,
            claimed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (automation_id, fire_bucket)
        )
    """)

    # ------------------------------------------------------------------
    # 46. fal.ai Model Catalog (dynamic model discovery cache)
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fal_model_catalog (
            endpoint_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            category TEXT NOT NULL,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            thumbnail_url TEXT,
            tags_json TEXT DEFAULT '[]',
            group_key TEXT,
            group_label TEXT,
            tool_type TEXT,
            output_type TEXT,
            updated_at TEXT,
            fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 47. Agent memory files (cross-install sync of the Claude Code CLI
    #     per-project memory directory: MEMORY.md + per-fact .md files).
    #     Keyed by `memory_key` = a project's link_group_id (stable across
    #     installs sharing a git remote) or its project_id when unlinked.
    #     `deleted` rows are tombstones so a delete on one install propagates.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_memory_files (
            id TEXT PRIMARY KEY,
            memory_key TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            content TEXT,
            content_hash TEXT,
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_by TEXT,
            UNIQUE (memory_key, relative_path)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_memory_key ON agent_memory_files(memory_key)"
    )

    # ==================================================================
    # INDEXES
    # ==================================================================

    # -- templates
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_templates_category ON templates(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_templates_profile ON templates(profile_id)")

    # -- sessions
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_profile ON sessions(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_favorite ON sessions(is_favorite)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_api_user ON sessions(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_worktree ON sessions(worktree_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC)")
    # Composite indexes for session list queries (most common access pattern)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project_updated ON sessions(project_id, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project_apiuser_updated ON sessions(project_id, api_user_id, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project_profile_updated ON sessions(project_id, profile_id, updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_title ON sessions(title)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sessions_sdk_session_id ON sessions(sdk_session_id)")
    # Note: idx_sessions_instance is created by _run_column_migrations after the
    # instance_id column is added (idempotent for both fresh installs and upgrades).

    # -- session_messages
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_messages_session ON session_messages(session_id)")
    # Rewind DB<->JSONL sync: fast lookup of a message by its SDK JSONL uuid
    # within a session (used by delete_session_messages_after_jsonl_uuid).
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_messages_session_uuid "
        "ON session_messages(session_id, jsonl_uuid)"
    )

    # -- usage_log
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_usage_log_created ON usage_log(created_at)")

    # -- auth_sessions
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires ON auth_sessions(expires_at)")

    # -- api_users
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_users_active ON api_users(is_active)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_api_users_username ON api_users(username) WHERE username IS NOT NULL")

    # -- api_user_profiles
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_profiles_user ON api_user_profiles(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_profiles_profile ON api_user_profiles(profile_id)")

    # -- sync_log
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_log_session ON sync_log(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_log_created ON sync_log(created_at)")

    # -- login_attempts
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_created ON login_attempts(created_at)")

    # -- account_lockouts
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_lockouts_ip ON account_lockouts(ip_address)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_account_lockouts_username ON account_lockouts(username)")

    # -- api_key_sessions
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_key_sessions_user ON api_key_sessions(api_user_id)")

    # -- user_preferences
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_preferences_user ON user_preferences(user_type, user_id)")

    # -- subagents
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_subagents_name ON subagents(name)")

    # -- permission_rules
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_rules_profile ON permission_rules(profile_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_permission_rules_tool ON permission_rules(tool_name)")

    # -- tags
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tags_name ON tags(name)")

    # -- session_tags
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_session ON session_tags(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_tags_tag ON session_tags(tag_id)")

    # -- webhooks
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_webhooks_active ON webhooks(is_active)")

    # -- rate_limits
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_user ON rate_limits(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_api_key ON rate_limits(api_key_id)")

    # -- request_log
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_log_user ON request_log(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_log_api_key ON request_log(api_key_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_log_timestamp ON request_log(timestamp)")

    # -- audit_log
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_event ON audit_log(event_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at)")

    # -- git_repositories
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_git_repositories_project ON git_repositories(project_id)")

    # -- worktrees
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_repository ON worktrees(repository_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_session ON worktrees(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_branch ON worktrees(branch_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_status ON worktrees(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_worktrees_repo_status ON worktrees(repository_id, status)")

    # -- notes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(is_pinned DESC, updated_at DESC)")

    # -- queued_messages
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_queued_messages_session ON queued_messages(session_id, position ASC)")

    # -- project_hooks
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_project_id ON project_hooks(project_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_hooks_event ON project_hooks(event)")

    # -- api_user_credentials
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_credentials_user ON api_user_credentials(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_credentials_type ON api_user_credentials(credential_type)")

    # -- api_user_github_config
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_api_user_github_config_user ON api_user_github_config(api_user_id)")

    # -- user_credential_policies
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_credential_policies_user ON user_credential_policies(api_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_credential_policies_type ON user_credential_policies(credential_type)")

    # -- mobile_devices
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mobile_devices_device_id ON mobile_devices(device_id)")

    # -- pending_actions
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_device ON pending_actions(device_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_expires ON pending_actions(expires_at)")

    # -- trusted_actions
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_trusted_actions_device ON trusted_actions(device_id)")

    # -- mobile_schedules
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mobile_schedules_device ON mobile_schedules(device_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_mobile_schedules_enabled ON mobile_schedules(enabled)")

    # -- connector_event_log
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connector_event_log_connector ON connector_event_log(connector_id, created_at DESC)")

    # -- connector_sessions
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connector_sessions_connector ON connector_sessions(connector_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connector_sessions_session ON connector_sessions(session_id)")

    # -- connectors
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_connectors_active ON connectors(is_active)")

    # -- automation_runs
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_automation ON automation_runs(automation_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_runs_status ON automation_runs(status)")

    # -- automation_approvals
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automation_approvals_status ON automation_approvals(status, created_at DESC)")

    # -- automations
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automations_enabled ON automations(is_enabled)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_automations_trigger ON automations(trigger_type)")

    # -- fal_model_catalog
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fal_catalog_category ON fal_model_catalog(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fal_catalog_tool_type ON fal_model_catalog(tool_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_fal_catalog_output_type ON fal_model_catalog(output_type)")

    # ------------------------------------------------------------------
    # 49. SDK SessionStore — Postgres-backed transcript mirror (v43)
    # ------------------------------------------------------------------
    # Implements the SDK 0.1.65+ SessionStore Protocol so session transcripts
    # (normally written as JSONL by the Claude Code subprocess) can be
    # persisted to Postgres as the single source of truth. The subprocess
    # still writes to an ephemeral CLAUDE_CONFIG_DIR; this table holds the
    # durable copy consumed by resume/list/summary APIs.
    #
    # ``subpath`` is NULL for main transcripts, populated for subagent
    # transcripts (e.g. "subagents/agent-{id}"). Per-entry ordering is by
    # auto-incrementing ``id`` which preserves append-call order. See
    # app/core/pg_session_store.py for the adapter.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_store_entries (
            id BIGSERIAL PRIMARY KEY,
            project_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            subpath TEXT,
            entry JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    # Lookups by (project_key, session_id, subpath) ordered by id for load().
    # Partial indexes distinguish main (NULL subpath) from subagent rows.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_main "
        "ON session_store_entries(project_key, session_id, id) "
        "WHERE subpath IS NULL"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_sub "
        "ON session_store_entries(project_key, session_id, subpath, id) "
        "WHERE subpath IS NOT NULL"
    )
    # For list_sessions() scoped by project_key.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_project "
        "ON session_store_entries(project_key) "
        "WHERE subpath IS NULL"
    )
    # PR C.1 read cutover: the app's detail/resume reads match on session_id
    # ALONE (the SDK-derived project_key diverges from the stored folder name —
    # see app/core/pg_session_store.py). These project_key-free partial indexes
    # serve that hot path (main transcript ordered by id; subagent subpaths).
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_session "
        "ON session_store_entries (session_id, id) "
        "WHERE subpath IS NULL"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_session_sub "
        "ON session_store_entries (session_id, subpath, id) "
        "WHERE subpath IS NOT NULL"
    )

    # Store-backed FTS: under postgres_only the assistant/tool history lives in
    # the JSONB ``entry`` (not session_messages). sse_entry_text() extracts the
    # searchable text of a user/assistant entry (skipping meta/sidechain rows)
    # and MUST be IMMUTABLE so it can back the expression GIN index below, which
    # makes full conversation history searchable (see search_sessions).
    cursor.execute("""
        CREATE OR REPLACE FUNCTION sse_entry_text(entry jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        AS $fn$
            SELECT CASE
                WHEN COALESCE(entry->>'type', '') NOT IN ('user', 'assistant') THEN NULL
                WHEN COALESCE((entry->>'isMeta')::boolean, false) THEN NULL
                WHEN COALESCE((entry->>'isSidechain')::boolean, false) THEN NULL
                WHEN jsonb_typeof(entry #> '{message,content}') = 'string'
                    THEN entry #>> '{message,content}'
                WHEN jsonb_typeof(entry #> '{message,content}') = 'array'
                    THEN (
                        SELECT string_agg(block ->> 'text', ' ')
                        FROM jsonb_array_elements(entry #> '{message,content}') AS block
                        WHERE block ->> 'type' = 'text'
                    )
                ELSE NULL
            END
        $fn$
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sse_fts "
        "ON session_store_entries USING gin (to_tsvector('english', sse_entry_text(entry))) "
        "WHERE subpath IS NULL"
    )

    # Summary sidecar written inside append() via fold_session_summary().
    # Scoped to main transcripts only (subagent appends must not mutate).
    # ``mtime_ms`` is storage write time (epoch ms) stamped by the adapter;
    # must share a clock with list_sessions().mtime for the same session.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_store_summaries (
            project_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            data JSONB NOT NULL,
            mtime_ms BIGINT NOT NULL,
            PRIMARY KEY (project_key, session_id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sss_project "
        "ON session_store_summaries(project_key)"
    )

    # ------------------------------------------------------------------
    # 50. Application error log (v44)
    # ------------------------------------------------------------------
    # Durable, queryable record of every error in the app — backend
    # unhandled exceptions / ERROR-level logs (captured via the root-logger
    # ErrorLogDBHandler) and frontend runtime / API / WebSocket errors
    # (POSTed to /api/v1/logs/client-error). Read by the admin UI and, more
    # importantly, by the GetAppErrors MCP tool so an agent can debug what
    # the user just hit ("check the logs and fix it"). Repeated errors with
    # the same ``fingerprint`` collapse onto one row (occurrence_count++,
    # updated_at bumped) so a render loop or crash storm can't flood it.
    # See app/db/error_logs.py and app/core/error_log_handler.py.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS error_log (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            level TEXT DEFAULT 'ERROR',
            message TEXT NOT NULL,
            detail TEXT,
            logger_name TEXT,
            url TEXT,
            status_code INTEGER,
            session_id TEXT,
            user_id TEXT,
            context JSONB,
            fingerprint TEXT,
            occurrence_count INTEGER DEFAULT 1,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_created ON error_log(created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_source ON error_log(source)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_session ON error_log(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_error_log_fingerprint ON error_log(fingerprint)")

    # ------------------------------------------------------------------
    # 49. Notifications — unified user-facing notification history (the bell).
    #
    # The single source of truth for every persisted banner / toast in the
    # app. Distinct from ``error_log`` (debug/observability): this is the
    # user-facing history surfaced by the NotificationCenter, with read /
    # dismissed state per row. Server-originated notifications are written by
    # app/services/notification_service.py; client-originated ones are POSTed
    # by the frontend notification store. See app/db/notifications.py.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            severity TEXT NOT NULL DEFAULT 'info',
            surface TEXT NOT NULL DEFAULT 'toast',
            category TEXT,
            source TEXT NOT NULL DEFAULT 'server',
            title TEXT,
            message TEXT NOT NULL,
            action_kind TEXT,
            action_payload JSONB,
            session_id TEXT,
            read BOOLEAN DEFAULT FALSE,
            dismissed BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_created ON notifications(user_id, created_at DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, read)")

    # ------------------------------------------------------------------
    # 50. session_events — append-only per-session event log (v2 core).
    #
    # The durable ordered record of everything that happens in a session.
    # Seqs are dense and monotonic per session, allocated atomically via the
    # session_event_counters companion table so a batch append reserves a
    # contiguous [first_seq, last_seq] range. Writes are fenced by the owning
    # run's fence_token (see app/db/session_events.py); readers tail by
    # ``seq > from_seq``. pg_notify on the ``session_events`` channel wakes the
    # gateway, with a poll fallback. See app/db/session_events.py.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            run_id TEXT,
            turn_id TEXT,
            event_type TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}',
            command_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (session_id, seq)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_session_events_session_seq ON session_events(session_id, seq)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_event_counters (
            session_id TEXT PRIMARY KEY,
            next_seq BIGINT NOT NULL DEFAULT 0
        )
    """)

    # ------------------------------------------------------------------
    # 51. runs — fenced, leased ownership of a session by a runner (v2 core).
    #
    # At most one active run per session (enforced by the partial unique
    # index), each carrying a monotonic ``fence_token`` that stamps every
    # event append so a stale/lost runner cannot write after being superseded.
    # Runs carry a heartbeat-renewed lease; expired leases are reaped. See
    # app/db/runs.py.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            runner_id TEXT NOT NULL,
            fence_token BIGINT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            heartbeat_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_one_active_per_session ON runs(session_id) WHERE state = 'active'")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id)")

    # ------------------------------------------------------------------
    # 52. pending_interactions — durable prompt/answer state machine (v2 core).
    #
    # Records interactions (permission prompts, tool approvals, …) awaiting a
    # user answer, with a TTL. Pure CRUD state machine here; session-event
    # wiring is Phase 1. See app/db/interactions.py.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_interactions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            run_id TEXT,
            kind TEXT NOT NULL,
            payload JSONB NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'pending',
            answer JSONB,
            answered_by_device TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
            answered_at TIMESTAMP WITH TIME ZONE
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_interactions_session_state ON pending_interactions(session_id, state)")

    # ------------------------------------------------------------------
    # 53. artifacts — content-addressed blob registry (v2 core).
    #
    # Deduplicated by content ``hash``; upserts are idempotent. See
    # app/db/artifacts.py.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            hash TEXT PRIMARY KEY,
            size BIGINT NOT NULL,
            mime TEXT,
            filename TEXT,
            created_by_session TEXT,
            runner_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 54. projects_v2 — v2 project registry keyed by unique origin.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects_v2 (
            id TEXT PRIMARY KEY,
            origin TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            settings JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)

    # ------------------------------------------------------------------
    # 55. checkouts — per-runner working-tree checkouts of a v2 project.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS checkouts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects_v2(id) ON DELETE CASCADE,
            runner_id TEXT NOT NULL,
            path TEXT NOT NULL,
            branch TEXT,
            head_sha TEXT,
            dirty BOOLEAN DEFAULT FALSE,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (project_id, runner_id, path)
        )
    """)

    # ------------------------------------------------------------------
    # 56. turns — the durable per-session turn queue + live-turn state
    #     machine (v2 core, Phase 2).
    #
    # A *turn* is one unit of work for a session (a user prompt, a queued
    # follow-up, an unpark wake, a failover retry, …). The table is the state
    # machine; the session_events log is the record. At most one turn per
    # session may be in a live state (running|interrupting|draining), enforced
    # by the partial unique index below — the same DB-raced ownership discipline
    # as ``runs``. Every transition is a single guarded UPDATE (see
    # app/db/turns.py); there are no in-memory locks. Event mirroring
    # (turn.* into session_events) is wired at the runtime layer, not here.
    # ------------------------------------------------------------------
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            source TEXT NOT NULL,
            prompt TEXT,
            profile_id TEXT,
            project_id TEXT,
            worktree_id TEXT,
            overrides JSONB NOT NULL DEFAULT '{}',
            payload JSONB NOT NULL DEFAULT '{}',
            command_id TEXT,
            parent_turn_id TEXT,
            run_id TEXT,
            interrupt_mode TEXT,
            stop_background BOOLEAN NOT NULL DEFAULT FALSE,
            position DOUBLE PRECISION,
            instance_id TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            started_at TIMESTAMP WITH TIME ZONE,
            ended_at TIMESTAMP WITH TIME ZONE,
            stop_reason TEXT,
            error TEXT
        )
    """)
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_one_live_per_session "
        "ON turns(session_id) WHERE state IN ('running','interrupting','draining')"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_turns_session_state ON turns(session_id, state)"
    )

    # ==================================================================
    # SEED DATA
    # ==================================================================

    # Default credential policies
    cursor.execute("""
        INSERT INTO credential_policies (id, policy, description)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, ('openai_api_key', 'optional', 'OpenAI API key for AI tools (TTS, STT, GPT Image, Sora)'))

    cursor.execute("""
        INSERT INTO credential_policies (id, policy, description)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, ('gemini_api_key', 'optional', 'Google Gemini API key for Nano Banana, Imagen, Veo'))

    cursor.execute("""
        INSERT INTO credential_policies (id, policy, description)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, ('meshy_api_key', 'optional', 'Meshy API key for 3D model generation'))

    cursor.execute("""
        INSERT INTO credential_policies (id, policy, description)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
    """, ('github_pat', 'user_provided', 'GitHub Personal Access Token for repository access'))

    logger.info("PostgreSQL schema created successfully (version %d)", SCHEMA_VERSION)
