"""Preserve user instructions alongside the agent's interpretation, without another LLM."""

from paper_search.domain import Run, RunStatus, ToolInputError, now
from paper_search.ports import UnitOfWork, UowFactory


async def require_research(
    uow: UowFactory,
    run: Run,
    *,
    unavailable_message: str,
) -> None:
    async with uow() as tx:
        current_run = await tx.runs.get(run.id)
        session = await tx.sessions.get(run.session_id)
        brief = (
            current_run.brief
            if current_run.parent_id and current_run.brief is not None
            else session.brief
        )
    if current_run.status == RunStatus.CANCELLED:
        raise ToolInputError("任务已取消")
    if session.brief.input_revision != session.input_revision:
        raise ToolInputError("请先用 update_task 处理最新用户输入")
    if run.parent_id:
        async with uow() as tx:
            root = await tx.runs.get(run.parent_id)
            board = await tx.research.get(run.session_id)
        if root.status != RunStatus.RUNNING:
            raise ToolInputError("根任务已暂停或结束")
        task = next((item for item in board.tasks if item.id == run.id), None)
        if (
            not task
            or task.status in {"cancelled", "stale"}
            or task.basis_revision != session.brief.basis_revision
            or task.task_revision != run.task_revision
        ):
            raise ToolInputError("Worker 研究依据过期")
    elif not brief.open_questions:
        raise ToolInputError(unavailable_message)


async def read_intent(tx: UnitOfWork, run: Run) -> dict:
    current = await tx.runs.get(run.id)
    brief = (
        run.brief if run.parent_id else (await tx.sessions.get(run.session_id)).brief
    )
    runs = {item.id: item for item in await tx.runs.list(run.session_id)}
    messages = await tx.sessions.messages(run.session_id, limit=10000)
    users = [
        item
        for item in sorted(messages, key=lambda item: item.created_at)
        if item.role == "user"
        and item.input_revision <= current.input_revision
        and (
            item.run_id == run.id
            or (
                not run.parent_id
                and not runs[item.run_id].parent_id
                and runs[item.run_id].status
                not in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING}
            )
        )
    ]
    if run.parent_id:
        parent_users = [
            item
            for item in sorted(messages, key=lambda item: item.created_at)
            if item.role == "user"
            and item.input_revision <= current.input_revision
            and (item.id in brief.source_message_ids or item.run_id == run.parent_id)
        ]
        users = parent_users[-3:]
        active = [brief.objective]
    else:
        active = [item.content for item in users if item.run_id == run.id]
    return {
        "current_date_utc": now().date().isoformat(),
        "original_request": users[0].content if users else brief.objective,
        "current_request": active[-1] if active else brief.objective,
        "user_updates": [item.content for item in users[1:]],
        "input_revision": current.input_revision,
        "task": brief.model_dump(mode="json"),
        "constraints": brief.hard_constraints.model_dump(mode="json"),
        "paper_selection_policy": {
            "required": ["真实存在", "满足用户约束", "与需求高度相关"],
            "tie_breakers_after_relevance": [
                "顶会或顶刊",
                "知名高校、研究机构或公司",
                "有原始来源支持的官方代码或项目开源",
            ],
            "ignore_unless_explicitly_requested": ["引用数", "DOI"],
            "notes": [
                "开放获取不等于代码或项目开源",
                "DOI 可在内部用于身份确认，但不是默认质量信号",
            ],
        },
    }
