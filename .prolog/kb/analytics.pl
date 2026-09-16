canonical_analytics_source('data/events.jsonl').
capture_usage_field(input, input_tokens).
capture_usage_field(output, output_tokens).
analytics_endpoint(summary, '/api/v1/stats/summary').
analytics_endpoint(timeline, '/api/v1/stats/timeline').
analytics_endpoint(models, '/api/v1/stats/models').
analytics_granularity(minute, 60).
analytics_granularity(hour, 3600).
analytics_granularity(day, 86400).
analytics_invariant(unknown_provider_usage_is_not_estimated).
analytics_invariant(range_start_inclusive).
analytics_invariant(range_end_exclusive).
capture_status_kind(upstream).
capture_status_kind(upstream_connect_error).
capture_status_kind(upstream_midstream_error).
stream_integrity_invariant(prepared_response_not_replaced).
stream_integrity_invariant(clean_sse_eof_is_not_terminal_evidence).
stream_integrity_invariant(non_sse_completion_is_unknown).
