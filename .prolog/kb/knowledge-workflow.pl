durable_knowledge_store(org_roam, 'roam/').
durable_knowledge_store(symbolic, '.prolog/kb/').
local_execution_store('.prolog/runs/').
knowledge_relation(issue, solution, org_id_link).
agent_skill(llm_log_knowledge, '.opencode/skills/llm-log-knowledge/SKILL.md').
agent_skill(llm_log_ci, '.opencode/skills/llm-log-ci/SKILL.md').
ci_poller('scripts/poll-ci.sh', background_capable).
