alert_decision_source('alert-decisions.jsonl').
alert_policy_scope(transport_evidence_only).
alert_policy_delivery_transport(none).

alert_policy_rule(downstream_client_disconnect, log_only, info, none, 0, 0, 0).
alert_policy_rule(gateway_or_account, alert, warning, account_gateway, 0, 900, 1).
alert_policy_rule(provider_transient, alert_if_unrecovered, warning, provider_transient, 10, 60, 1).
alert_policy_rule(protocol_integrity, alert_if_unrecovered, warning, protocol_integrity, 10, 300, 1).
alert_policy_rule(unknown_transport, alert, warning, unknown_transport, 0, 60, 1).

alert_policy_recovery_action(suppress_recovered).
alert_policy_recovery_reason(recovered_within_retry_correlation).

alert_policy_invariant(client_disconnect_never_provider_alert).
alert_policy_invariant(account_gateway_model_attribution_absent).
alert_policy_invariant(recovery_suppression_links_failed_event).
alert_policy_invariant(policy_failure_never_blocks_capture_or_forwarding).
alert_policy_invariant(no_request_sha256_in_decision_projection).
alert_policy_invariant(no_router_attempt_provider_detail_in_decision_projection).
alert_policy_invariant(no_external_notification_delivery_in_core_policy).

alert_decision_field(decision_id).
alert_decision_field(event_id).
alert_decision_field(observed_at).
alert_decision_field(action).
alert_decision_field(severity).
alert_decision_field(rate_class).
alert_decision_field(grace_seconds).
alert_decision_field(rate_window_seconds).
alert_decision_field(max_notifications_per_window).
alert_decision_field(reason).
