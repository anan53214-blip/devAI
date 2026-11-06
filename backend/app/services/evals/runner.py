from typing import Any
from uuid import UUID, uuid4

from langchain_core.utils.function_calling import convert_to_openai_tool
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Base, CodeSymbol, Document, Issue, PullRequest, Repository, WorkflowRun
from app.schemas.analysis import CIDebugOutput, IssueAnalysisOutput, PRReviewOutput
from app.services.agents.ci_debug_agent import CIDebugAgent
from app.services.agents.chat_agent import ChatAgent
from app.services.agents.issue_agent import IssueAgent
from app.services.agents.pr_review_agent import PRReviewAgent
from app.services.context_compression import (
    compression_eval_summary,
    compress_messages,
    compress_retrieval_results,
    fallback_memory_update,
    merge_memory,
    render_structured_memory,
)
from app.services.evals.ragas_runner import run_rag_quality_eval
from app.services.rag.retrieval import search_similar_documents
from app.services.skills import get_skill_registry


class NoopLLM:
    async def chat_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return {}


async def run_basic_eval(db: Session | None = None, repo_id: str | None = None) -> dict[str, Any]:
    issue_agent = IssueAgent(llm=NoopLLM())
    pr_agent = PRReviewAgent(llm=NoopLLM())
    ci_agent = CIDebugAgent(llm=NoopLLM())

    issue_cases = [
        ({"title": "Login API returns 500", "body": "Token expires and login fails", "labels": ["bug"]}, "Bug"),
        ({"title": "Document local demo mode", "body": "README should explain seed script", "labels": ["documentation"]}, "Documentation"),
        ({"title": "Add workflow sync", "body": "Sync recent GitHub Actions runs", "labels": ["feature", "ci"]}, "Ops"),
    ]
    issue_results = [await issue_agent.run(payload, {}) for payload, _expected in issue_cases]
    issue_accuracy = sum(result["category"] == expected for result, (_payload, expected) in zip(issue_results, issue_cases)) / len(issue_cases)

    pr_payload = {
        "number": 12,
        "title": "Refactor auth middleware",
        "body": "Split token validation and session refresh",
        "files": [
            {"filename": "backend/app/auth/middleware.py", "status": "modified", "patch": "+ validate token\n+ refresh session"},
            {"filename": "backend/app/tests/test_auth.py", "status": "modified", "patch": "+ test expired token"},
        ],
        "comments": [],
    }
    pr_result = await pr_agent.run(pr_payload, {})
    key_text = " ".join(pr_result.get("key_changes", []) + pr_result.get("files_need_attention", []))
    pr_key_file_coverage = sum(file["filename"] in key_text for file in pr_payload["files"]) / len(pr_payload["files"])

    rag_docs = [
        ("issue-login", "Login API returns 500 when token expires"),
        ("issue-docs", "Document local demo mode and README setup"),
        ("issue-ci", "GitHub Actions pytest job fails after dependency update"),
        ("retry-policy", "Refresh retries require an idempotency key and may retry only once"),
        ("release-guide", "Release builds are tagged and published after review"),
        ("frontend-layout", "Frontend layout uses Tailwind and responsive components"),
        ("database-backup", "PostgreSQL backups are retained for seven days"),
        ("team-workflow", "Team members rotate weekly report responsibilities"),
    ]
    retrieval_cases = [
        ("token expires login 500", "issue-login"),
        ("pytest dependency update failure", "issue-ci"),
        ("refresh retry idempotency key", "retry-policy"),
    ]
    retrieval_results: list[dict[str, Any]] = []
    tool_schema_checks: list[dict[str, Any]] = []
    smoke_engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(smoke_engine)
        with Session(smoke_engine) as smoke_db:
            smoke_repo = Repository(owner="eval", name="basic-smoke", full_name="eval/basic-smoke")
            smoke_db.add(smoke_repo)
            smoke_db.flush()
            for name, content in rag_docs:
                smoke_db.add(Document(repo_id=smoke_repo.id, source_type="knowledge_file", source_id=uuid4(), title=name, content=content))
            smoke_db.flush()
            for query, expected_title in retrieval_cases:
                hits = search_similar_documents(
                    smoke_db, smoke_repo.id, query, limit=3,
                    retrieval_method="fulltext", apply_rerank=False,
                )
                titles = [item["title"] for item in hits]
                retrieval_results.append({
                    "query": query,
                    "expected_title": expected_title,
                    "returned_titles": titles,
                    "passed": expected_title in titles,
                })

            from app.api.routes.chat import _build_conversation_tools

            raw_tools = _build_conversation_tools(None, None, smoke_db)
            tools = ChatAgent()._build_langchain_tools(raw_tools, {})
            for tool in tools.values():
                schema = convert_to_openai_tool(tool)["function"]
                tool_schema_checks.append({
                    "name": schema["name"],
                    "ready": bool(schema.get("description")) and schema.get("parameters", {}).get("type") == "object",
                })
    finally:
        smoke_engine.dispose()
    rag_hit_rate_at_3 = sum(item["passed"] for item in retrieval_results) / len(retrieval_results)
    rag_quality = await run_rag_quality_eval(db, repo_id, run_judge=False) if db is not None and repo_id else None

    long_messages = [
        {"role": "user", "content": "We decided that auth refresh belongs in middleware. " * 40},
        {"role": "assistant", "content": "Decision captured. Next step is adding tests for expired tokens. " * 36},
        {"role": "user", "content": "What did we decide about auth token refresh?"},
    ]
    compressed_messages, message_stats = compress_messages(long_messages, max_tokens=260)
    memory_update = fallback_memory_update(
        "Please implement auth token refresh in middleware",
        "Decision: keep refresh in middleware. Next step: add expired token tests.",
        "direct_answer",
        [],
    )
    merged_memory = merge_memory(
        {"summary": "", "facts": [], "decisions": [], "open_questions": [], "tasks": [], "user_preferences": [], "repo_context": [], "citations": []},
        memory_update,
        memory_update["summary"],
        max_tokens=240,
    )
    retrieval_items = compress_retrieval_results(
        "auth token refresh middleware",
        [
            {
                "id": "auth-doc",
                "title": "backend/app/auth/middleware.py#1",
                "source_type": "workspace_file",
                "source_id": "auth-doc",
                "score": 1.0,
                "_raw_content": ("unrelated setup notes. " * 80) + "auth middleware refreshes expired token and validates session.",
                "metadata": {"path": "backend/app/auth/middleware.py"},
            }
        ],
        max_tokens_per_item=80,
    )
    compression_quality = compression_eval_summary(
        "\n".join(item["content"] for item in long_messages),
        "\n".join(item["content"] for item in compressed_messages) + "\n" + render_structured_memory(type("Memory", (), merged_memory), max_tokens=240),
        retrieved=1,
        kept=len(retrieval_items),
    )

    ci_result = await ci_agent.run(
        {
            "name": "backend-test",
            "status": "completed",
            "conclusion": "failure",
            "jobs": [{"name": "pytest", "conclusion": "failure", "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
            "logs_text": "pytest tests/test_api.py FAILED AssertionError",
        },
        {},
    )
    ci_text = " ".join(
        [
            str(ci_result.get("failure_summary") or ""),
            str(ci_result.get("first_error") or ""),
            str(ci_result.get("root_cause") or ""),
        ]
    ).lower()
    ci_pytest_failure_detection = 1.0 if ci_result.get("failure_type") == "test" and "pytest" in ci_text else 0.0
    schema_checks = [
        IssueAnalysisOutput.model_validate(issue_results[0]),
        PRReviewOutput.model_validate(pr_result),
        CIDebugOutput.model_validate(ci_result),
    ]
    json_schema_pass_rate = len(schema_checks) / 3

    tool_call_schema_ready_rate = sum(item["ready"] for item in tool_schema_checks) / len(tool_schema_checks)
    registry = get_skill_registry()
    required_skill_entrypoints = {
        "issue-triage": "issue_agent.analyze_issue",
        "pr-review": "pr_review_agent.analyze_pr",
        "ci-debug": "ci_debug_agent.analyze_workflow_run",
        "weekly-report": "report_agent.generate_weekly_report",
        "safety-draft": "safety_agent.classify_action_risk",
        "project-investigation": "devflow_search_evidence",
    }
    loaded_skill_names = {skill.manifest.name for skill in registry.list_skills()}
    skill_registry_coverage = len(loaded_skill_names & set(required_skill_entrypoints)) / len(required_skill_entrypoints)
    skill_entrypoint_hits = 0
    for skill_name, entrypoint in required_skill_entrypoints.items():
        skill = registry.skill_for_entrypoint(entrypoint)
        if skill and skill.manifest.name == skill_name:
            skill_entrypoint_hits += 1
    skill_entrypoint_accuracy = skill_entrypoint_hits / len(required_skill_entrypoints)
    report_lines = [
        "# Basic Smoke Eval Report",
        "",
        "Scope: isolated fixtures, BM25 retrieval, and native tool schema validation.",
        "Model tool-selection accuracy is measured by the ChatAgent/RAGAS evaluation.",
        "",
        f"- Issue classification accuracy: {issue_accuracy:.2f}",
        f"- PR key-file coverage: {pr_key_file_coverage:.2f}",
        f"- RAG hit@3: {rag_hit_rate_at_3:.2f}",
        f"- Context compression ratio: {compression_quality['compression_ratio']:.2f}",
        f"- Skill registry coverage: {skill_registry_coverage:.2f}",
        f"- Skill entrypoint accuracy: {skill_entrypoint_accuracy:.2f}",
        f"- CI pytest failure detection: {ci_pytest_failure_detection:.2f}",
    ]
    if rag_quality:
        report_lines.append(f"- RAG recall@5: {rag_quality['retrieval']['recall_at_k']:.2f}")
    report_lines.extend(
        [
            f"- Native tool schema ready rate: {tool_call_schema_ready_rate:.2f}",
            f"- JSON schema pass rate: {json_schema_pass_rate:.2f}",
        ]
    )
    report_markdown = "\n".join(report_lines) + "\n"

    repository_sample: dict[str, Any] | None = None
    if db is not None and repo_id:
        repo_uuid = UUID(str(repo_id))
        repository_sample = {
            "issues": db.query(Issue).filter(Issue.repo_id == repo_uuid).count(),
            "pull_requests": db.query(PullRequest).filter(PullRequest.repo_id == repo_uuid).count(),
            "workflow_runs": db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo_uuid).count(),
            "code_symbols": db.query(CodeSymbol).filter(CodeSymbol.repo_id == repo_uuid).count(),
            "knowledge_documents": db.query(Document).filter(Document.repo_id == repo_uuid).count(),
        }

    return {
        "kind": "basic_smoke",
        "issue_classification_accuracy": round(issue_accuracy, 4),
        "pr_key_file_coverage": round(pr_key_file_coverage, 4),
        "rag_hit_rate_at_3": round(rag_hit_rate_at_3, 4),
        "tool_call_accuracy": None,
        "tool_call_schema_ready_rate": round(tool_call_schema_ready_rate, 4),
        "tool_schema_checks": tool_schema_checks,
        "retrieval_smoke": {"backend": "bm25", "candidate_documents": len(rag_docs), "cases": retrieval_results},
        "skill_registry_coverage": round(skill_registry_coverage, 4),
        "skill_entrypoint_accuracy": round(skill_entrypoint_accuracy, 4),
        "ci_pytest_failure_detection": round(ci_pytest_failure_detection, 4),
        "json_schema_pass_rate": round(json_schema_pass_rate, 4),
        "rag_quality": rag_quality,
        "context_compression": {
            "message_stats": message_stats,
            "retrieval_stats": retrieval_items[0].get("compression") if retrieval_items else {},
            "memory_sections": {
                "facts": len(merged_memory["facts"]),
                "decisions": len(merged_memory["decisions"]),
                "tasks": len(merged_memory["tasks"]),
            },
            "quality": compression_quality,
        },
        "cases": {
            "issue": len(issue_cases),
            "pr": 1,
            "rag": len(retrieval_cases),
            "ci": 1,
            "skills": len(required_skill_entrypoints),
            "context_compression": 3,
        },
        "repository_sample": repository_sample,
        "report_markdown": report_markdown,
    }
