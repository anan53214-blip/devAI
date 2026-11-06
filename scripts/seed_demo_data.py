from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db.models import Document, Issue, PRFile, PRReviewComment, PullRequest, Repository, WorkflowRun
from app.db.session import Base, SessionLocal, engine
from app.services.rag.indexing import index_repository_documents
from app.services.rag.vector_store import add_document


def prepare_demo_checkout() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    checkout = (project_root / ".data" / "demo-repo").resolve()
    if not checkout.is_relative_to(project_root.resolve()):
        raise ValueError("演示工作区路径超出了项目目录")
    files = {
        "README.md": (
            "# DevFlow 本机演示 API\n\n"
            "这个仓库提供预置 Issue、PR 和失败 CI 的代码证据。\n"
            "认证接口应将过期 JWT 转为 401。GitHub 同步示例目前将列表截断为两条，"
            "三条 Issue 的测试因此失败，修复时应保留完整列表。\n"
        ),
        "docs/architecture.md": (
            "# 演示架构\n\n"
            "GitHubClient 读取仓库信息，normalize_issues 处理 Issue 列表。\n"
            "失败 CI 对应 test_github_sync.py，预期保留三条 Issue，当前结果只有两条。\n"
        ),
        "backend/app/services/github/client.py": (
            "class GitHubClient:\n"
            "    async def get_repo(self, owner, repo):\n"
            "        return {\"full_name\": f\"{owner}/{repo}\", \"default_branch\": \"main\"}\n"
        ),
        "backend/app/services/repos_sync.py": (
            "def normalize_issues(items):\n"
            "    return items[:2]\n"
        ),
        "backend/app/api/routes/repos.py": (
            "def connect_repo(owner, repo):\n"
            "    return {\"full_name\": f\"{owner}/{repo}\", \"connected\": True}\n"
        ),
        "backend/app/tests/test_github_sync.py": (
            "from app.services.repos_sync import normalize_issues\n\n"
            "def test_sync_issues():\n"
            "    issues = [{\"id\": 1}, {\"id\": 2}, {\"id\": 3}]\n"
            "    assert len(normalize_issues(issues)) == 3\n"
        ),
        ".devflow/index.yml": (
            "version: 1\nproject_docs:\n  mode: allowlist\n  include:\n"
            "    - README.md\n    - docs/**\n    - rag_employee_handbook.md\n"
        ),
        ".gitignore": ".env\n__pycache__/\n.pytest_cache/\n",
        "rag_employee_handbook.md": (project_root / "docs/examples/rag_employee_handbook.md").read_text(encoding="utf-8"),
    }
    for relative_path, content in files.items():
        target = checkout / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=checkout, capture_output=True)
    if head.returncode != 0:
        subprocess.run(["git", "add", *files], cwd=checkout, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=DevFlow Demo", "-c", "user.email=demo@example.invalid", "commit", "-m", "Initialize local practice fixtures"],
            cwd=checkout, check=True, capture_output=True,
        )
    return checkout


def main() -> None:
    checkout = prepare_demo_checkout()
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        repo = db.query(Repository).filter(Repository.full_name == "course-demo/devflow-sample-api").one_or_none()
        if repo is None:
            repo = Repository(
                owner="course-demo",
                name="devflow-sample-api",
                full_name="course-demo/devflow-sample-api",
                provider="demo",
                description="本机演示仓库，包含预置的 Issue、PR、CI 和团队成员。",
                default_branch="main",
                github_id=10001,
            )
            db.add(repo)
            db.commit()
            db.refresh(repo)

        repo.provider = "demo"
        repo.local_path = str(checkout)
        repo.checkout_mode = "local"

        if db.query(Document).filter(Document.repo_id == repo.id, Document.source_type == "team_member").count() == 0:
            members = [
                {"name": "Alice", "role": "后端工程师", "strengths": "认证、JWT、Python 接口", "tech_stack": "Python, FastAPI, PostgreSQL"},
                {"name": "Bob", "role": "平台工程师", "strengths": "GitHub Actions、依赖与构建排障", "tech_stack": "Python, Docker, GitHub Actions"},
                {"name": "Carol", "role": "前端工程师", "strengths": "交互、前端测试与文档", "tech_stack": "TypeScript, Next.js, React"},
            ]
            for member in members:
                add_document(
                    db, repo.id, "team_member", None, f"团队成员 {member['name']}",
                    f"{member['name']} {member['role']} {member['strengths']}", member,
                )

        if db.query(Issue).filter(Issue.repo_id == repo.id).count() == 0:
            issues = [
                Issue(repo_id=repo.id, github_issue_id=101, number=1, title="Login API returns 500 when token expires", body="JWT expires and backend returns 500 instead of 401.", state="open", labels=["bug", "auth"], author="alice", assignees=[], created_at=datetime.now(timezone.utc)),
                Issue(repo_id=repo.id, github_issue_id=102, number=2, title="Add GitHub workflow sync", body="Sync recent workflow runs and expose failed CI runs in dashboard.", state="open", labels=["feature", "ci"], author="bob", assignees=["backend"], created_at=datetime.now(timezone.utc)),
                Issue(repo_id=repo.id, github_issue_id=103, number=3, title="Document local demo mode", body="README should explain how to run without GitHub token.", state="closed", labels=["documentation"], author="carol", assignees=[], created_at=datetime.now(timezone.utc)),
            ]
            db.add_all(issues)
            db.commit()

        if db.query(PullRequest).filter(PullRequest.repo_id == repo.id).count() == 0:
            pr = PullRequest(repo_id=repo.id, github_pr_id=201, number=8, title="Refactor GitHub sync service", body="Split GitHub API calls into issue, PR and actions modules.", state="open", author="dave", base_branch="main", head_branch="refactor/github-sync", created_at=datetime.now(timezone.utc))
            db.add(pr)
            db.commit()
            db.refresh(pr)
            db.add_all([
                PRFile(pr_id=pr.id, filename="backend/app/services/github/client.py", status="modified", additions=42, deletions=8, patch="+ async def get_repo(...)\n+ response.raise_for_status()"),
                PRFile(pr_id=pr.id, filename="backend/app/api/routes/repos.py", status="modified", additions=55, deletions=12, patch="+ @router.post('/connect')\n+ async def connect_repo(...)")
            ])
            db.add_all([
                PRReviewComment(pr_id=pr.id, github_comment_id=401, body="Please verify token handling is not logged.", path="backend/app/api/routes/repos.py", line=24, author="reviewer-a", created_at=datetime.now(timezone.utc)),
                PRReviewComment(pr_id=pr.id, github_comment_id=402, body="Consider splitting GitHub sync into smaller retriable steps.", path="backend/app/services/repos_sync.py", line=57, author="reviewer-b", created_at=datetime.now(timezone.utc)),
            ])

        if db.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).count() == 0:
            db.add_all([
                WorkflowRun(
                    repo_id=repo.id,
                    github_run_id=301,
                    name="backend-test",
                    status="completed",
                    conclusion="failure",
                    html_url="https://github.com/course-demo/devflow-sample-api/actions/runs/301",
                    jobs=[
                        {
                            "id": 9001,
                            "name": "pytest",
                            "status": "completed",
                            "conclusion": "failure",
                            "steps": [{"name": "Run backend tests", "status": "completed", "conclusion": "failure", "number": 4}],
                        }
                    ],
                    logs_text="pytest backend/app/tests/test_github_sync.py::test_sync_issues FAILED AssertionError: expected 3 issues, got 2",
                    created_at=datetime.now(timezone.utc),
                ),
                WorkflowRun(repo_id=repo.id, github_run_id=302, name="frontend-lint", status="completed", conclusion="success", html_url="https://github.com/course-demo/devflow-sample-api/actions/runs/302", logs_text="", created_at=datetime.now(timezone.utc))
            ])

        db.flush()
        index_repository_documents(db, repo.id)
        db.commit()
        print(f"Seeded demo data for repo {repo.full_name} ({repo.id})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
