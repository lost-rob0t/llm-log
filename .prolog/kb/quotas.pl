% Runtime invariants exercised by tests/test_quotas.py.
quota_endpoint('/api/v1/quotas', python_capture_runtime).
quota_percent_semantics(used).
quota_token_accounting_distinct.
quota_plan_name_does_not_define_capacity.
quota_missing_state(unknown).
quota_expired_state(expired).
quota_provider_source(zai, zai_monitor).
quota_provider_source(gpt, codex_app_server).
quota_gpt_meter_scope_preserved.
quota_http_reads_snapshot_only.
quota_provider_credentials_excluded_from_response.
