-- Schema-only SQLite fixture for the v49 migration boundary.
-- No application rows or historical source code.

CREATE TABLE account_lockouts (
            id INTEGER PRIMARY KEY,
            ip_address TEXT,
            username TEXT,
            locked_until TIMESTAMP NOT NULL,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE admin (
            id INTEGER PRIMARY KEY DEFAULT 1,
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (id = 1)
        );

CREATE TABLE agent_memory_files (
            id TEXT PRIMARY KEY,
            memory_key TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            content TEXT,
            content_hash TEXT,
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by TEXT,
            UNIQUE (memory_key, relative_path)
        );

CREATE TABLE api_key_sessions (
            token TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        );

CREATE TABLE api_user_credentials (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        );

CREATE TABLE api_user_github_config (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL UNIQUE,
            github_username TEXT,
            github_avatar_url TEXT,
            default_repo TEXT,
            default_branch TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE
        );

CREATE TABLE api_user_profiles (
            api_user_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (api_user_id, profile_id),
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        );

CREATE TABLE api_users (
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        );

CREATE TABLE audit_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            user_type TEXT DEFAULT 'admin',
            event_type TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE auth_sessions (
            token TEXT PRIMARY KEY,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE automation_approvals (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            prompt_preview TEXT,
            decided_by TEXT,
            decided_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES automation_runs(id) ON DELETE CASCADE,
            FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE
        );

CREATE TABLE automation_runs (
            id TEXT PRIMARY KEY,
            automation_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            trigger_data TEXT,
            session_id TEXT,
            prompt_rendered TEXT,
            response TEXT,
            error TEXT,
            cost_usd REAL DEFAULT 0.0,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            duration_ms INTEGER DEFAULT 0,
            output_status TEXT DEFAULT 'pending',
            output_error TEXT,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (automation_id) REFERENCES automations(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL
        );

CREATE TABLE automations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            trigger_type TEXT NOT NULL,
            trigger_config TEXT NOT NULL DEFAULT '{}',
            prompt TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            project_id TEXT,
            output_config TEXT NOT NULL DEFAULT '{"type": "session"}',
            max_turns INTEGER DEFAULT 1,
            timeout_seconds INTEGER DEFAULT 300,
            max_cost_per_run REAL,
            max_cost_per_day REAL,
            max_cost_total REAL,
            max_runs_per_day INTEGER,
            total_cost_used REAL DEFAULT 0.0,
            is_enabled BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'idle',
            last_run_at TIMESTAMP,
            last_error TEXT,
            run_count INTEGER DEFAULT 0,
            failure_count INTEGER DEFAULT 0,
            requires_approval BOOLEAN DEFAULT FALSE,
            action_steps TEXT DEFAULT NULL,
            triggers TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE RESTRICT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
        );

CREATE TABLE connector_event_log (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            direction TEXT NOT NULL DEFAULT 'outbound',
            payload TEXT,
            status TEXT DEFAULT 'pending',
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE
        );

CREATE TABLE connector_sessions (
            id TEXT PRIMARY KEY,
            connector_id TEXT NOT NULL,
            platform_chat_id TEXT NOT NULL,
            platform_user_id TEXT,
            platform_user_name TEXT,
            session_id TEXT,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (connector_id) REFERENCES connectors(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE SET NULL,
            UNIQUE(connector_id, platform_chat_id)
        );

CREATE TABLE connectors (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            api_user_id TEXT,
            config TEXT NOT NULL DEFAULT '{}',
            events TEXT NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'disconnected',
            status_message TEXT,
            last_connected_at TIMESTAMP,
            last_error TEXT,
            last_event_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL
        );

CREATE TABLE credential_policies (
            id TEXT PRIMARY KEY,
            policy TEXT NOT NULL DEFAULT 'admin_provided',
            description TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE error_log (
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
            context TEXT,
            fingerprint TEXT,
            occurrence_count INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE fal_model_catalog (
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
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE git_repositories (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL UNIQUE,
            remote_url TEXT,
            default_branch TEXT DEFAULT 'main',
            github_repo_name TEXT,
            last_synced_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

CREATE TABLE installs (
            id TEXT PRIMARY KEY,
            device_name TEXT NOT NULL,
            base_url TEXT,
            platform TEXT,
            version TEXT,
            capabilities TEXT DEFAULT '{}',
            last_seen TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY,
            ip_address TEXT NOT NULL,
            username TEXT,
            success BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE mobile_devices (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL UNIQUE,
            device_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            api_key_hash TEXT NOT NULL,
            fcm_token TEXT,
            unifiedpush_endpoint TEXT,
            capabilities TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        );

CREATE TABLE mobile_schedules (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            trigger_time TIMESTAMP,
            hour INTEGER,
            minute INTEGER,
            params TEXT DEFAULT '{}',
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        );

CREATE TABLE note_attachments (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER DEFAULT 0,
            data BYTEA,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        );

CREATE TABLE notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            is_pinned BOOLEAN DEFAULT FALSE,
            file_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE notifications (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            severity TEXT NOT NULL DEFAULT 'info',
            surface TEXT NOT NULL DEFAULT 'toast',
            category TEXT,
            source TEXT NOT NULL DEFAULT 'server',
            title TEXT,
            message TEXT NOT NULL,
            action_kind TEXT,
            action_payload TEXT,
            session_id TEXT,
            read BOOLEAN DEFAULT FALSE,
            dismissed BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE pending_actions (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            params TEXT DEFAULT '{}',
            security_level TEXT DEFAULT 'normal',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'pending',
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        );

CREATE TABLE permission_rules (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            tool_name TEXT NOT NULL,
            tool_pattern TEXT,
            decision TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE CASCADE
        );

CREATE TABLE profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            config TEXT NOT NULL,
            mcp_tools TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE project_hooks (
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            path TEXT NOT NULL,
            settings TEXT DEFAULT '{}',
            instance_id TEXT,
            link_group_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE queued_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            content TEXT NOT NULL,
            profile_id TEXT,
            project_id TEXT,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

CREATE TABLE rate_limits (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            requests_per_minute INTEGER DEFAULT 20,
            requests_per_hour INTEGER DEFAULT 200,
            requests_per_day INTEGER DEFAULT 1000,
            concurrent_requests INTEGER DEFAULT 3,
            priority INTEGER DEFAULT 0,
            is_unlimited BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_key_id) REFERENCES api_users(id) ON DELETE CASCADE
        );

CREATE TABLE request_log (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            api_key_id TEXT,
            endpoint TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            duration_ms INTEGER,
            status TEXT DEFAULT 'success'
        );

CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY
        );

CREATE TABLE session_messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input TEXT,
            metadata TEXT,
            profile_id TEXT,
            jsonl_uuid TEXT,
            version INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            content_tsv TEXT,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

CREATE TABLE session_store_entries (
            id INTEGER PRIMARY KEY,
            project_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            subpath TEXT,
            entry TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE session_store_summaries (
            project_key TEXT NOT NULL,
            session_id TEXT NOT NULL,
            data TEXT NOT NULL,
            mtime_ms BIGINT NOT NULL,
            PRIMARY KEY (project_key, session_id)
        );

CREATE TABLE session_tags (
            session_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, tag_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        );

CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            profile_id TEXT,
            api_user_id TEXT,
            sdk_session_id TEXT,
            worktree_id TEXT,
            worktree_branch TEXT,
            worktree_path TEXT,
            cloud_branch TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            is_favorite BOOLEAN DEFAULT FALSE,
            total_cost_usd REAL DEFAULT 0,
            total_tokens_in INTEGER DEFAULT 0,
            total_tokens_out INTEGER DEFAULT 0,
            last_input_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            turn_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            parent_session_id TEXT,
            fork_point_message_index INTEGER,
            instance_id TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL,
            FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
        );

CREATE TABLE subagents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tools TEXT,
            model TEXT,
            skills TEXT,
            memory TEXT,
            mcp_servers TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            default_name TEXT,
            default_description TEXT,
            default_prompt TEXT,
            default_tools TEXT,
            default_model TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE sync_log (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT,
            data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );

CREATE TABLE system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#6366f1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            prompt TEXT NOT NULL,
            profile_id TEXT,
            icon TEXT,
            category TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES profiles(id) ON DELETE SET NULL
        );

CREATE TABLE trusted_actions (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            action_pattern TEXT NOT NULL,
            auto_approve BOOLEAN DEFAULT TRUE,
            require_preview BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (device_id) REFERENCES mobile_devices(device_id) ON DELETE CASCADE
        );

CREATE TABLE usage_log (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            profile_id TEXT,
            model TEXT,
            tokens_in INTEGER,
            tokens_out INTEGER,
            cost_usd REAL,
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE user_credential_policies (
            id TEXT PRIMARY KEY,
            api_user_id TEXT NOT NULL,
            credential_type TEXT NOT NULL,
            policy TEXT NOT NULL DEFAULT 'optional',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE CASCADE,
            UNIQUE(api_user_id, credential_type)
        );

CREATE TABLE user_preferences (
            id INTEGER PRIMARY KEY,
            user_type TEXT NOT NULL,
            user_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_type, user_id, key)
        );

CREATE TABLE webhooks (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            secret TEXT,
            events TEXT NOT NULL DEFAULT '[]',
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_triggered_at TIMESTAMP,
            failure_count INTEGER DEFAULT 0
        );

CREATE TABLE worktrees (
            id TEXT PRIMARY KEY,
            repository_id TEXT NOT NULL,
            session_id TEXT,
            branch_name TEXT NOT NULL,
            worktree_path TEXT NOT NULL,
            base_branch TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (repository_id) REFERENCES git_repositories(id) ON DELETE CASCADE
        );

CREATE INDEX idx_account_lockouts_ip ON account_lockouts(ip_address);

CREATE INDEX idx_account_lockouts_username ON account_lockouts(username);

CREATE INDEX idx_agent_memory_key ON agent_memory_files(memory_key);

CREATE INDEX idx_api_key_sessions_user ON api_key_sessions(api_user_id);

CREATE INDEX idx_api_user_credentials_type ON api_user_credentials(credential_type);

CREATE INDEX idx_api_user_credentials_user ON api_user_credentials(api_user_id);

CREATE INDEX idx_api_user_github_config_user ON api_user_github_config(api_user_id);

CREATE INDEX idx_api_user_profiles_profile ON api_user_profiles(profile_id);

CREATE INDEX idx_api_user_profiles_user ON api_user_profiles(api_user_id);

CREATE INDEX idx_api_users_active ON api_users(is_active);

CREATE UNIQUE INDEX idx_api_users_username ON api_users(username) WHERE username IS NOT NULL;

CREATE INDEX idx_audit_log_created ON audit_log(created_at);

CREATE INDEX idx_audit_log_event ON audit_log(event_type);

CREATE INDEX idx_audit_log_user ON audit_log(user_id);

CREATE INDEX idx_auth_sessions_expires ON auth_sessions(expires_at);

CREATE INDEX idx_automation_approvals_status ON automation_approvals(status, created_at DESC);

CREATE INDEX idx_automation_runs_automation ON automation_runs(automation_id, created_at DESC);

CREATE INDEX idx_automation_runs_status ON automation_runs(status);

CREATE INDEX idx_automations_enabled ON automations(is_enabled);

CREATE INDEX idx_automations_trigger ON automations(trigger_type);

CREATE INDEX idx_connector_event_log_connector ON connector_event_log(connector_id, created_at DESC);

CREATE INDEX idx_connector_sessions_connector ON connector_sessions(connector_id);

CREATE INDEX idx_connector_sessions_session ON connector_sessions(session_id);

CREATE INDEX idx_connectors_active ON connectors(is_active);

CREATE INDEX idx_error_log_created ON error_log(created_at DESC);

CREATE INDEX idx_error_log_fingerprint ON error_log(fingerprint);

CREATE INDEX idx_error_log_session ON error_log(session_id);

CREATE INDEX idx_error_log_source ON error_log(source);

CREATE INDEX idx_fal_catalog_category ON fal_model_catalog(category);

CREATE INDEX idx_fal_catalog_output_type ON fal_model_catalog(output_type);

CREATE INDEX idx_fal_catalog_tool_type ON fal_model_catalog(tool_type);

CREATE INDEX idx_git_repositories_project ON git_repositories(project_id);

CREATE INDEX idx_installs_last_seen ON installs(last_seen DESC);

CREATE INDEX idx_login_attempts_created ON login_attempts(created_at);

CREATE INDEX idx_login_attempts_ip ON login_attempts(ip_address);

CREATE INDEX idx_mobile_devices_device_id ON mobile_devices(device_id);

CREATE INDEX idx_mobile_schedules_device ON mobile_schedules(device_id);

CREATE INDEX idx_mobile_schedules_enabled ON mobile_schedules(enabled);

CREATE INDEX idx_note_attachments_note ON note_attachments(note_id);

CREATE INDEX idx_notes_pinned ON notes(is_pinned DESC, updated_at DESC);

CREATE INDEX idx_notes_updated ON notes(updated_at DESC);

CREATE INDEX idx_notifications_user_created ON notifications(user_id, created_at DESC);

CREATE INDEX idx_notifications_user_read ON notifications(user_id, read);

CREATE INDEX idx_pending_actions_device ON pending_actions(device_id);

CREATE INDEX idx_pending_actions_expires ON pending_actions(expires_at);

CREATE INDEX idx_pending_actions_status ON pending_actions(status);

CREATE INDEX idx_permission_rules_profile ON permission_rules(profile_id);

CREATE INDEX idx_permission_rules_tool ON permission_rules(tool_name);

CREATE INDEX idx_project_hooks_event ON project_hooks(event);

CREATE INDEX idx_project_hooks_project_id ON project_hooks(project_id);

CREATE INDEX idx_projects_link_group ON projects(link_group_id);

CREATE INDEX idx_queued_messages_session ON queued_messages(session_id, position ASC);

CREATE INDEX idx_rate_limits_api_key ON rate_limits(api_key_id);

CREATE INDEX idx_rate_limits_user ON rate_limits(user_id);

CREATE INDEX idx_request_log_api_key ON request_log(api_key_id);

CREATE INDEX idx_request_log_timestamp ON request_log(timestamp);

CREATE INDEX idx_request_log_user ON request_log(user_id);

CREATE INDEX idx_session_messages_session ON session_messages(session_id);

CREATE INDEX idx_session_messages_session_uuid ON session_messages(session_id, jsonl_uuid);

CREATE INDEX idx_session_tags_session ON session_tags(session_id);

CREATE INDEX idx_session_tags_tag ON session_tags(tag_id);

CREATE INDEX idx_sessions_api_user ON sessions(api_user_id);

CREATE INDEX idx_sessions_favorite ON sessions(is_favorite);

CREATE INDEX idx_sessions_instance ON sessions(instance_id);

CREATE INDEX idx_sessions_parent ON sessions(parent_session_id);

CREATE INDEX idx_sessions_profile ON sessions(profile_id);

CREATE INDEX idx_sessions_project ON sessions(project_id);

CREATE INDEX idx_sessions_project_apiuser_updated ON sessions(project_id, api_user_id, updated_at DESC);

CREATE INDEX idx_sessions_project_profile_updated ON sessions(project_id, profile_id, updated_at DESC);

CREATE INDEX idx_sessions_project_updated ON sessions(project_id, updated_at DESC);

CREATE INDEX idx_sessions_sdk_session_id ON sessions(sdk_session_id);

CREATE INDEX idx_sessions_status ON sessions(status);

CREATE INDEX idx_sessions_title ON sessions(title);

CREATE INDEX idx_sessions_updated ON sessions(updated_at DESC);

CREATE INDEX idx_sessions_worktree ON sessions(worktree_id);

CREATE INDEX idx_sse_main ON session_store_entries(project_key, session_id, id) WHERE subpath IS NULL;

CREATE INDEX idx_sse_project ON session_store_entries(project_key) WHERE subpath IS NULL;

CREATE INDEX idx_sse_session ON session_store_entries (session_id, id) WHERE subpath IS NULL;

CREATE INDEX idx_sse_session_sub ON session_store_entries (session_id, subpath, id) WHERE subpath IS NOT NULL;

CREATE INDEX idx_sse_sub ON session_store_entries(project_key, session_id, subpath, id) WHERE subpath IS NOT NULL;

CREATE INDEX idx_sss_project ON session_store_summaries(project_key);

CREATE INDEX idx_subagents_name ON subagents(name);

CREATE INDEX idx_sync_log_created ON sync_log(created_at);

CREATE INDEX idx_sync_log_session ON sync_log(session_id);

CREATE INDEX idx_tags_name ON tags(name);

CREATE INDEX idx_templates_category ON templates(category);

CREATE INDEX idx_templates_profile ON templates(profile_id);

CREATE INDEX idx_trusted_actions_device ON trusted_actions(device_id);

CREATE INDEX idx_usage_log_created ON usage_log(created_at);

CREATE INDEX idx_user_credential_policies_type ON user_credential_policies(credential_type);

CREATE INDEX idx_user_credential_policies_user ON user_credential_policies(api_user_id);

CREATE INDEX idx_user_preferences_user ON user_preferences(user_type, user_id);

CREATE INDEX idx_webhooks_active ON webhooks(is_active);

CREATE INDEX idx_worktrees_branch ON worktrees(branch_name);

CREATE INDEX idx_worktrees_repo_status ON worktrees(repository_id, status);

CREATE INDEX idx_worktrees_repository ON worktrees(repository_id);

CREATE INDEX idx_worktrees_session ON worktrees(session_id);

CREATE INDEX idx_worktrees_status ON worktrees(status);
INSERT INTO schema_version (version) VALUES (49);
