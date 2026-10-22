-- Schema-only SQLite fixture for the v88 migration boundary.
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

CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_builtin BOOLEAN DEFAULT FALSE,
            config TEXT NOT NULL,
            mcp_tools TEXT DEFAULT '[]',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE CASCADE
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
            model TEXT,
            effort TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE SET NULL
        );

CREATE TABLE artifacts (
            hash TEXT PRIMARY KEY,
            size BIGINT NOT NULL,
            mime TEXT,
            filename TEXT,
            created_by_session TEXT,
            runner_id TEXT,
            -- Worker network: the device that first stored the bytes.
            owner_device_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE automation_fires (
            automation_id TEXT NOT NULL,
            fire_bucket TEXT NOT NULL,
            claimed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (automation_id, fire_bucket)
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
            -- Worker network: the device that fires this automation. A synced
            -- definition with no local owner stays inactive until assigned.
            owner_device_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE RESTRICT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
        );

CREATE TABLE background_tasks (
            task_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            instance_id TEXT,
            kind TEXT,
            is_sdk_background BOOLEAN DEFAULT FALSE,
            tool_use_id TEXT,
            title TEXT,
            status TEXT,
            terminal_notified BOOLEAN DEFAULT FALSE,
            summary TEXT,
            reason TEXT,
            report_path TEXT,
            report_chars BIGINT,
            started_at TIMESTAMP,
            updated_at TIMESTAMP,
            PRIMARY KEY (task_id, session_id)
        );

CREATE TABLE checkouts (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects_v2(id) ON DELETE CASCADE,
            runner_id TEXT NOT NULL,
            path TEXT NOT NULL,
            branch TEXT,
            head_sha TEXT,
            dirty BOOLEAN DEFAULT FALSE,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (project_id, runner_id, path)
        );

CREATE TABLE claude_accounts (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT,
            subscription_type TEXT,
            credentials TEXT,
            credential_fingerprint TEXT,
            expires_at BIGINT,
            has_refresh_token BOOLEAN DEFAULT TRUE,
            sync_pending BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP
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
            runner_id TEXT,
            -- Worker network: the device that runs this connector's session.
            owner_device_id TEXT,
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

CREATE TABLE fluid_session_events (
            session_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            event TEXT NOT NULL,
            PRIMARY KEY (session_id, seq)
        );

CREATE TABLE fluid_session_nodes (
            session_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            parent_id TEXT,
            ts REAL NOT NULL,
            message TEXT NOT NULL,
            PRIMARY KEY (session_id, node_id)
        );

CREATE TABLE fluid_sessions (
            session_id TEXT PRIMARY KEY,
            head_node_id TEXT,
            created_at REAL NOT NULL,
            cli_session_id TEXT,
            project_root TEXT
        );

CREATE TABLE git_ops (
            id TEXT PRIMARY KEY,
            runner_id TEXT NOT NULL,
            project_v2_id TEXT,
            op TEXT NOT NULL,
            params TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            result TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            approved_at TIMESTAMP,
            revoked_at TIMESTAMP,
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
            paired_client_id TEXT,
            api_key_expires_at TIMESTAMP,
            scopes TEXT NOT NULL DEFAULT '["controller", "mobile:notifications", "mobile:actions", "web:session"]',
            paired_at TIMESTAMP,
            revoked_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP
        );

CREATE TABLE mobile_hub_authorizations (
            device_id TEXT PRIMARY KEY,
            paired_client_id TEXT NOT NULL UNIQUE,
            account_id TEXT NOT NULL, hub_device_id TEXT NOT NULL UNIQUE,
            server_id TEXT NOT NULL
        );

CREATE TABLE mobile_hub_binding (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            hub_url TEXT NOT NULL, server_id TEXT NOT NULL,
            linked_by TEXT NOT NULL,
            active BOOLEAN NOT NULL DEFAULT FALSE
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

CREATE TABLE mobile_worker_authorizations (
        device_id TEXT PRIMARY KEY, paired_client_id TEXT NOT NULL UNIQUE,
        account_id TEXT NOT NULL, hub_device_id TEXT NOT NULL,
        server_id TEXT NOT NULL, UNIQUE(server_id, hub_device_id)
    );

CREATE TABLE mobile_worker_bindings (
        instance_id TEXT PRIMARY KEY, hub_url TEXT, server_id TEXT UNIQUE,
        linked_by TEXT, active BOOLEAN NOT NULL DEFAULT FALSE
    );

CREATE TABLE model_providers (
            id TEXT PRIMARY KEY,
            revision TEXT,
            name TEXT NOT NULL,
            provider_type TEXT NOT NULL,
            base_url TEXT NOT NULL,
            api_dialect TEXT,
            auth_token TEXT,
            models_json TEXT DEFAULT '[]',
            default_model TEXT,
            small_fast_model TEXT,
            extra_env_json TEXT DEFAULT '{}',
            context_window INTEGER,
            max_output_tokens INTEGER,
            enabled BOOLEAN DEFAULT TRUE,
            auth_mode TEXT,
            codex_access_token TEXT,
            codex_refresh_token TEXT,
            codex_account_id TEXT,
            codex_id_token_expires_at TIMESTAMP,
            codex_last_refresh TIMESTAMP,
            needs_reauth BOOLEAN DEFAULT FALSE,
            codex_refresh_lease_owner TEXT,
            codex_refresh_lease_expires_at TIMESTAMP,
            openai_admin_key TEXT,
            openai_billing_org_id TEXT,
            openai_billing_instance_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_catalog_cursors (
            session_id TEXT PRIMARY KEY,
            published_seq BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_joins (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            device_id TEXT,
            name TEXT,
            platform TEXT,
            signing_public TEXT,
            encryption_public TEXT,
            fingerprint TEXT,
            grant_id TEXT,
            poll_secret_hash TEXT,
            endpoint_url TEXT,
            inviter_fingerprint TEXT,
            legacy_instance_id TEXT,
            response TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        );

CREATE TABLE network_member_names (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_members (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'worker',
            platform TEXT,
            signing_public TEXT NOT NULL,
            encryption_public TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            last_seq_accepted BIGINT,
            last_incarnation_accepted INTEGER,
            incarnation INTEGER NOT NULL DEFAULT 1,
            capabilities TEXT NOT NULL DEFAULT '{}',
            endpoints TEXT NOT NULL DEFAULT '[]',
            version TEXT,
            legacy_instance_id TEXT,
            last_seen TIMESTAMP,
            added_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_membership_records (
            id TEXT PRIMARY KEY,
            record TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_memory_records (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_plugins_desired (
            id TEXT PRIMARY KEY,
            version TEXT,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            config TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_presence (
            device_id TEXT PRIMARY KEY,
            endpoints TEXT NOT NULL DEFAULT '[]',
            capabilities TEXT NOT NULL DEFAULT '{}',
            version TEXT,
            platform TEXT,
            incarnation INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_session_runtime (
            session_id TEXT PRIMARY KEY,
            owner_device_id TEXT NOT NULL,
            state TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE network_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            network_id TEXT NOT NULL,
            name TEXT,
            founder_device_id TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            key_version INTEGER NOT NULL DEFAULT 1,
            clock BIGINT NOT NULL DEFAULT 0,
            bootstrap_state TEXT NOT NULL DEFAULT 'ready',
            keyring TEXT NOT NULL DEFAULT '{}',
            recovery TEXT,
            hub TEXT,
            legacy_instance_map TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CHECK (id = 1)
        );

CREATE TABLE note_attachments (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            content_type TEXT,
            size_bytes INTEGER DEFAULT 0,
            data BYTEA,
            -- Worker network: content hash of ``data`` so peers fetch bytes on demand.
            blob_hash TEXT,
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
            surface TEXT NOT NULL DEFAULT 'notification',
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

CREATE TABLE owner_local_sessions (
        token_hash TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
        purpose TEXT NOT NULL, expires_at TEXT NOT NULL);

CREATE TABLE paired_client_native_sessions (
        client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL UNIQUE,
        expires_at TIMESTAMP NOT NULL
    );

CREATE TABLE paired_client_web_reservations (
        client_id TEXT PRIMARY KEY REFERENCES paired_clients(id) ON DELETE CASCADE,
        mobile_record_id TEXT NOT NULL, api_key_hash TEXT NOT NULL,
        reservation_hash TEXT NOT NULL UNIQUE,
        expires_at TIMESTAMP NOT NULL
    );

CREATE TABLE paired_clients (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL UNIQUE,
            device_name TEXT NOT NULL,
            platform TEXT NOT NULL,
            client_type TEXT NOT NULL DEFAULT 'browser',
            scopes TEXT NOT NULL DEFAULT '["controller"]',
            session_token_hash TEXT UNIQUE,
            session_expires_at TIMESTAMP,
            paired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP,
            revoked_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE pairing_grants (
            id TEXT PRIMARY KEY,
            token_hash TEXT NOT NULL UNIQUE,
            purpose TEXT NOT NULL DEFAULT 'controller',
            scopes TEXT NOT NULL DEFAULT '["controller"]',
            target_install_id TEXT,
            endpoint_url TEXT,
            device_label TEXT,
            created_by TEXT NOT NULL DEFAULT 'admin',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP,
            revoked_at TIMESTAMP,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 10
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

CREATE TABLE pending_interactions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            run_id TEXT,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL DEFAULT 'pending',
            answer TEXT,
            answered_by_device TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            answered_at TIMESTAMP
        );

CREATE TABLE pending_wakeups (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            profile_id TEXT,
            project_id TEXT,
            prompt TEXT NOT NULL,
            reason TEXT,
            model TEXT,
            source TEXT NOT NULL DEFAULT 'schedule_wakeup',
            instance_id TEXT,
            fire_at REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE permission_rules (
            id TEXT PRIMARY KEY,
            profile_id TEXT,
            tool_name TEXT NOT NULL,
            tool_pattern TEXT,
            decision TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE CASCADE
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

CREATE TABLE projects_v2 (
            id TEXT PRIMARY KEY,
            origin TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            settings TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE provider_usage_snapshots (
            provider_id TEXT PRIMARY KEY,
            provider_type TEXT,
            plan_type TEXT,
            windows TEXT NOT NULL DEFAULT '[]',
            credits TEXT,
            raw TEXT,
            fetched_at BIGINT,
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

CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            runner_id TEXT NOT NULL,
            fence_token BIGINT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active',
            lease_expires_at TIMESTAMP NOT NULL,
            heartbeat_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY
        );

CREATE TABLE session_event_counters (
            session_id TEXT PRIMARY KEY,
            next_seq BIGINT NOT NULL DEFAULT 0
        );

CREATE TABLE session_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            seq BIGINT NOT NULL,
            run_id TEXT,
            turn_id TEXT,
            event_type TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            command_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (session_id, seq)
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
            -- v84: highest ``session_store_entries.id`` already folded into
            -- ``data``. The fold is COALESCED (every few seconds / on read)
            -- instead of running on every ~100ms streaming append, so the
            -- summary has to know where it left off. Crash-safe: the fold is a
            -- pure function of the entries, so whatever was not folded before a
            -- crash is folded by the next append/read from this watermark.
            folded_through_id BIGINT NOT NULL DEFAULT 0,
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
            automation_id TEXT,
            parked_task_kind TEXT,
            title TEXT,
            status TEXT DEFAULT 'active',
            is_active BOOLEAN DEFAULT FALSE,
            total_cost_usd REAL DEFAULT 0,
            -- Cumulative usage. total_tokens_in counts FRESH input only (the
            -- provider normalizes Anthropic-style, so cache reads are excluded);
            -- the two total_cache_* columns carry the rest. True prompt tokens
            -- for a session = total_tokens_in + total_cache_read_tokens +
            -- total_cache_creation_tokens. BIGINT because a cache-heavy session
            -- blows past INTEGER's 2.1B ceiling.
            total_tokens_in BIGINT DEFAULT 0,
            total_tokens_out BIGINT DEFAULT 0,
            total_cache_read_tokens BIGINT DEFAULT 0,
            total_cache_creation_tokens BIGINT DEFAULT 0,
            -- Snapshot (NOT cumulative) of the LAST API call's usage — this is
            -- what the context bar renders on reload.
            last_input_tokens INTEGER DEFAULT 0,
            cache_creation_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            -- turn_count counts USER-VISIBLE turns (one per completed turn);
            -- total_model_calls counts the API hops inside them.
            turn_count INTEGER DEFAULT 0,
            total_model_calls INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            parent_session_id TEXT,
            fork_point_message_index INTEGER,
            instance_id TEXT,
            launched_model TEXT,
            voice_config TEXT,
            -- The FIRST model this session ever ran on. launched_model is
            -- overwritten by every mid-session switch, so a session that ran
            -- turns 1-6 on grok-4.5 and 7-16 on grok-4.6 was recorded as
            -- grok-4.6 only and its early history was unattributable. Written
            -- once (COALESCE, never overwritten — see sessions.update_session).
            first_model TEXT,
            v2_stream BOOLEAN NOT NULL DEFAULT TRUE,
            project_v2_id TEXT,
            branch TEXT,
            placement TEXT,
            working_set_ref TEXT,
            context_usage_json TEXT,
            provider_id TEXT,
            model TEXT,
            effort TEXT,
            permission_mode TEXT,
            context_window INTEGER,
            workspace_project_ids TEXT,
            fast BOOLEAN,
            mode TEXT,
            hidden BOOLEAN,
            -- v84: materialized "this session is a delegated ProviderAgent
            -- worker" flag. Replaces the per-row correlated
            -- ``NOT EXISTS (SELECT 1 FROM turns ...)`` probe the sessions-list
            -- queries used to carry (one index probe into ``turns`` per listed
            -- row AND per COUNT(*) row). NULL/FALSE = a normal session.
            is_worker BOOLEAN,
            -- v85: persisted pull-request lifecycle for the sidepanel. NULL on
            -- every pre-existing row (no PR known). pr_state is 'open' /
            -- 'merged' / 'closed'.
            pr_number INTEGER,
            pr_url TEXT,
            pr_state TEXT,
            -- Worker network: the device this session executes on. Replaces
            -- instance_id as the owner; NULL means "this worker" until the
            -- startup backfill stamps it.
            owner_device_id TEXT,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
            FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE SET NULL,
            FOREIGN KEY (api_user_id) REFERENCES api_users(id) ON DELETE SET NULL,
            FOREIGN KEY (worktree_id) REFERENCES worktrees(id) ON DELETE SET NULL
        );

CREATE TABLE sync_blobs (
            hash TEXT PRIMARY KEY,
            size BIGINT NOT NULL DEFAULT 0,
            present BOOLEAN NOT NULL DEFAULT FALSE,
            holders TEXT NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE sync_conflicts (
            id INTEGER PRIMARY KEY,
            collection TEXT NOT NULL,
            record_id TEXT NOT NULL,
            losing_clock BIGINT NOT NULL,
            losing_device_id TEXT NOT NULL,
            losing_record TEXT,
            winning_clock BIGINT NOT NULL,
            winning_device_id TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
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

CREATE TABLE sync_ops (
            ingest_id INTEGER PRIMARY KEY,
            origin_device_id TEXT NOT NULL,
            incarnation INTEGER NOT NULL,
            seq BIGINT NOT NULL,
            collection TEXT NOT NULL,
            record_id TEXT NOT NULL,
            op TEXT NOT NULL,
            clock BIGINT NOT NULL,
            lane TEXT NOT NULL DEFAULT 'settings',
            envelope TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (origin_device_id, incarnation, seq)
        );

CREATE TABLE sync_peers (
            device_id TEXT PRIMARY KEY,
            base_url TEXT,
            delivered_incarnation INTEGER NOT NULL DEFAULT 0,
            delivered_seq BIGINT NOT NULL DEFAULT 0,
            last_contact TIMESTAMP,
            last_error TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE sync_pending (
            origin_device_id TEXT NOT NULL,
            incarnation INTEGER NOT NULL,
            seq BIGINT NOT NULL,
            envelope TEXT NOT NULL,
            received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (origin_device_id, incarnation, seq)
        );

CREATE TABLE sync_records (
            collection TEXT NOT NULL,
            record_id TEXT NOT NULL,
            clock BIGINT NOT NULL,
            origin_device_id TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            order_seq BIGINT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (collection, record_id)
        );

CREATE TABLE sync_watermarks (
            origin_device_id TEXT NOT NULL,
            incarnation INTEGER NOT NULL,
            last_seq BIGINT NOT NULL DEFAULT 0,
            min_retained_seq BIGINT NOT NULL DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (origin_device_id, incarnation)
        );

CREATE TABLE system_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            revision TEXT,
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
            FOREIGN KEY (profile_id) REFERENCES agents(id) ON DELETE SET NULL
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

CREATE TABLE turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',
            source TEXT NOT NULL,
            prompt TEXT,
            profile_id TEXT,
            project_id TEXT,
            worktree_id TEXT,
            overrides TEXT NOT NULL DEFAULT '{}',
            payload TEXT NOT NULL DEFAULT '{}',
            command_id TEXT,
            parent_turn_id TEXT,
            run_id TEXT,
            interrupt_mode TEXT,
            stop_background BOOLEAN NOT NULL DEFAULT FALSE,
            position REAL,
            instance_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            stop_reason TEXT,
            error TEXT
        );

CREATE TABLE upload_blobs (
            id TEXT PRIMARY KEY,
            project_v2_id TEXT,
            session_id TEXT,
            source_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            size BIGINT NOT NULL DEFAULT 0,
            data BYTEA,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            materialized_at TIMESTAMP
        );

CREATE TABLE usage_log (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            profile_id TEXT,
            model TEXT,
            -- tokens_in is FRESH input only; the cache columns carry the rest of
            -- the prompt, so a row's true prompt size is the sum of all three.
            tokens_in INTEGER,
            tokens_out INTEGER,
            cache_read_tokens BIGINT DEFAULT 0,
            cache_creation_tokens BIGINT DEFAULT 0,
            cost_usd REAL,
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

CREATE TABLE usage_poll_state (
            identity TEXT PRIMARY KEY,
            next_attempt REAL NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            paused BOOLEAN NOT NULL DEFAULT FALSE
        );

CREATE TABLE usage_snapshots (
            instance_id TEXT PRIMARY KEY,
            windows TEXT NOT NULL DEFAULT '[]',
            available BOOLEAN DEFAULT FALSE,
            generation BIGINT,
            fetched_at BIGINT,
            account_id TEXT,
            account_email TEXT,
            account_name TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            revision TEXT,
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

CREATE INDEX idx_background_tasks_instance_status
        ON background_tasks(instance_id, status)
    ;

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

CREATE INDEX idx_git_ops_runner_status ON git_ops(runner_id, status);

CREATE INDEX idx_git_repositories_project ON git_repositories(project_id);

CREATE INDEX idx_installs_last_seen ON installs(last_seen DESC);

CREATE INDEX idx_login_attempts_created ON login_attempts(created_at);

CREATE INDEX idx_login_attempts_ip ON login_attempts(ip_address);

CREATE INDEX idx_mobile_devices_device_id ON mobile_devices(device_id);

CREATE INDEX idx_mobile_schedules_device ON mobile_schedules(device_id);

CREATE INDEX idx_mobile_schedules_enabled ON mobile_schedules(enabled);

CREATE INDEX idx_model_providers_enabled ON model_providers(enabled);

CREATE INDEX idx_note_attachments_note ON note_attachments(note_id);

CREATE INDEX idx_notes_pinned ON notes(is_pinned DESC, updated_at DESC);

CREATE INDEX idx_notes_updated ON notes(updated_at DESC);

CREATE INDEX idx_notifications_user_created ON notifications(user_id, created_at DESC);

CREATE INDEX idx_notifications_user_read ON notifications(user_id, read);

CREATE INDEX idx_paired_clients_active ON paired_clients(revoked_at, paired_at DESC);

CREATE UNIQUE INDEX idx_paired_clients_session ON paired_clients(session_token_hash) WHERE session_token_hash IS NOT NULL;

CREATE INDEX idx_pairing_grants_active ON pairing_grants(expires_at) WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE UNIQUE INDEX idx_pairing_grants_token ON pairing_grants(token_hash);

CREATE INDEX idx_pending_actions_device ON pending_actions(device_id);

CREATE INDEX idx_pending_actions_expires ON pending_actions(expires_at);

CREATE INDEX idx_pending_actions_status ON pending_actions(status);

CREATE INDEX idx_pending_interactions_session_state ON pending_interactions(session_id, state);

CREATE INDEX idx_pending_wakeups_instance
        ON pending_wakeups(instance_id, fire_at)
    ;

CREATE INDEX idx_pending_wakeups_session
        ON pending_wakeups(session_id)
    ;

CREATE INDEX idx_permission_rules_profile ON permission_rules(profile_id);

CREATE INDEX idx_permission_rules_tool ON permission_rules(tool_name);

CREATE INDEX idx_project_hooks_event ON project_hooks(event);

CREATE INDEX idx_project_hooks_project_id ON project_hooks(project_id);

CREATE INDEX idx_projects_instance ON projects(instance_id);

CREATE INDEX idx_projects_link_group ON projects(link_group_id);

CREATE INDEX idx_queued_messages_session ON queued_messages(session_id, position ASC);

CREATE UNIQUE INDEX idx_runs_one_active_per_session ON runs(session_id) WHERE state = 'active';

CREATE INDEX idx_runs_session ON runs(session_id);

CREATE INDEX idx_session_events_session_seq ON session_events(session_id, seq);

CREATE INDEX idx_session_messages_session ON session_messages(session_id);

CREATE INDEX idx_session_messages_session_uuid ON session_messages(session_id, jsonl_uuid);

CREATE INDEX idx_session_tags_session ON session_tags(session_id);

CREATE INDEX idx_session_tags_tag ON session_tags(tag_id);

CREATE INDEX idx_sessions_active ON sessions(is_active);

CREATE INDEX idx_sessions_api_user ON sessions(api_user_id);

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

CREATE INDEX idx_sync_log_created ON sync_log(created_at);

CREATE INDEX idx_sync_log_session ON sync_log(session_id);

CREATE INDEX idx_sync_ops_record ON sync_ops(collection, record_id);

CREATE INDEX idx_tags_name ON tags(name);

CREATE INDEX idx_templates_category ON templates(category);

CREATE INDEX idx_templates_profile ON templates(profile_id);

CREATE INDEX idx_trusted_actions_device ON trusted_actions(device_id);

CREATE UNIQUE INDEX idx_turns_one_live_per_session ON turns(session_id) WHERE state IN ('running','interrupting','draining');

CREATE INDEX idx_turns_session_state ON turns(session_id, state);

CREATE INDEX idx_upload_blobs_session ON upload_blobs(session_id);

CREATE INDEX idx_usage_log_created ON usage_log(created_at);

CREATE INDEX idx_usage_log_session ON usage_log(session_id);

CREATE INDEX idx_user_credential_policies_type ON user_credential_policies(credential_type);

CREATE INDEX idx_user_credential_policies_user ON user_credential_policies(api_user_id);

CREATE INDEX idx_user_preferences_user ON user_preferences(user_type, user_id);

CREATE INDEX idx_webhooks_active ON webhooks(is_active);

CREATE INDEX idx_worktrees_branch ON worktrees(branch_name);

CREATE INDEX idx_worktrees_repo_status ON worktrees(repository_id, status);

CREATE INDEX idx_worktrees_repository ON worktrees(repository_id);

CREATE INDEX idx_worktrees_session ON worktrees(session_id);

CREATE INDEX idx_worktrees_status ON worktrees(status);
INSERT INTO schema_version (version) VALUES (88);
