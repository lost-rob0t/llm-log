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
