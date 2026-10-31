expert_state_owner(single_process, common_lisp_expert).
expert_store(tek9, durable_derived_state).
expert_reasoner(swi_prolog, declared_operations_only).
expert_raw_source('events.jsonl', append_only).
expert_maintenance_listener(loopback_only, '127.0.0.1', 8788).
expert_maintenance_query(read_only, bounded).
expert_backfill_identity(stable_capture_event_id).
expert_backfill_checkpoint(contiguous_successful_prefix).
expert_usage_backfill(request_level, no_synthetic_task_or_price).
expert_transport_evidence(provider_transport, weak, opt_in).
expert_dataset_pagination(index, 'outcome-assertion-outcome-id').
expert_dataset_export(raw_capture_join, sha256_manifest).
expert_failure_mining_source(caller_supplied_normalized_captures).
expert_failure_mining_authority(prolog, 'outcome.decision/v2').
expert_failure_mining_scope(request, no_inferred_task_outcome).
expert_failure_mining_negative(prose_or_missing_fields, unknown).
expert_failure_mining_provenance(stable_evidence_id, capture_json_location).
expert_failure_mining_limit(evidence_items, 64).
expert_provider_status_meaning(403, access_denied).
expert_provider_status_meaning(429, rate_or_quota_limit).
subscription_capture_runtime(python, configured_client_traffic_only).
subscription_auth(codex, preserved_oauth_protocol_headers).
subscription_attribution(company, server_configured_only).
subscription_header_egress('x-llm-log-*', stripped).
subscription_logical_accounting(final_response_usage, attempts_separate).
subscription_fallback(openrouter, mapped_chat_completions_precommit_only).
subscription_fallback_loop_guard(via, 'llm-log-fallback').
subscription_body_hash(redacted_bytes, retention_independent).
subscription_redaction_context(actual_attempt_credentials, never_serialized).
subscription_missing_usage(unknown, no_estimate).
