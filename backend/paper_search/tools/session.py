from __future__ import annotations

from typing import Annotated

from pydantic import Field

from paper_search.domain import (
    ComparisonSeed,
    DeliverySpec,
    ExperimentalContext,
    Finding,
    ResearchMode,
    ResearchQuestion,
    Run,
    RunStatus,
    SearchConstraints,
    TaskAssignment,
    TaskBrief,
    ToolInputError,
    ToolResult,
    Value,
)
from paper_search.ports import AgentCallbacks, ArtifactStore, UowFactory
from paper_search.research import ResearchService, ResearchSubmission


class TaskUpdate(Value):
    input_revision: int = Field(ge=0)
    objective: str | None = None
    research_mode: ResearchMode | None = None
    focus: tuple[str, ...] | None = None
    seeds: tuple[ComparisonSeed, ...] | None = None
    experimental_context: ExperimentalContext | None = None
    delivery: DeliverySpec | None = None
    source_message_ids: tuple[str, ...] | None = None
    add_constraints: tuple[str, ...] = ()
    remove_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] | None = None
    assumptions: tuple[str, ...] | None = None
    research_questions: tuple[str, ...]
    hard_constraints: SearchConstraints | None = None


class SessionTools:
    def __init__(
        self,
        run: Run,
        uow: UowFactory,
        artifacts: ArtifactStore,
        callbacks: AgentCallbacks,
        research: ResearchService,
        memory,
    ):
        self.run, self.uow, self.artifacts = run, uow, artifacts
        self.callbacks, self.research, self.memory = callbacks, research, memory

    async def update_task(self, brief: TaskUpdate) -> TaskBrief:
        """解释最新用户意图。目标、焦点、种子和硬条件改变研究依据；数量/格式只是交付变化。嵌套状态仅更新显式字段，撤销使用 null/空列表；本地问答允许问题列表为空。"""
        async with self.uow() as tx:
            session = await tx.sessions.get(self.run.session_id)
            current = await tx.runs.get(self.run.id)
            if (
                self.run.parent_id
                or brief.input_revision != session.input_revision
                or current.input_revision != session.input_revision
                or current.status != RunStatus.RUNNING
            ):
                raise ToolInputError("只能由主 Agent 基于当前 input_revision 更新目标")
            raw_messages = await tx.sessions.messages(session.id, limit=10000)
            roots = {
                item.id for item in await tx.runs.list(session.id) if not item.parent_id
            }
            sources = {
                item.id: item
                for item in raw_messages
                if item.role == "user"
                and item.run_id in roots
                and item.input_revision <= session.input_revision
            }
            if (
                brief.source_message_ids is not None
                and not set(brief.source_message_ids) <= sources.keys()
            ):
                raise ToolInputError("需求引用必须指向当前会话已有的用户原话")
            old = session.brief
            updates = {
                key: getattr(brief, key)
                for key in brief.model_fields_set
                - {"add_constraints", "remove_constraints", "research_questions"}
                if getattr(brief, key) is not None
            }
            for name in ("hard_constraints", "delivery", "experimental_context"):
                incoming = updates.get(name)
                if incoming is not None:
                    updates[name] = getattr(old, name).model_copy(
                        update={
                            key: getattr(incoming, key)
                            for key in incoming.model_fields_set
                        }
                    )
            bounds = updates.get("hard_constraints", old.hard_constraints)
            if (
                bounds.date_from
                and bounds.date_to
                and bounds.date_from > bounds.date_to
            ):
                raise ToolInputError(
                    "新的时间下界超过当前上界；请明确修改或撤销对应条件"
                )
            updates["source_message_ids"] = (
                brief.source_message_ids
                if brief.source_message_ids is not None
                else tuple(
                    dict.fromkeys(
                        (
                            *old.source_message_ids,
                            *(
                                key
                                for key, item in sources.items()
                                if item.run_id == self.run.id
                            ),
                        )
                    )
                )
            )
            updates["constraints"] = tuple(
                dict.fromkeys(
                    (
                        *[
                            item
                            for item in old.constraints
                            if item not in brief.remove_constraints
                        ],
                        *brief.add_constraints,
                    )
                )
            )
            updates["open_questions"] = brief.research_questions
            changed = old.model_copy(update=updates)
            if changed == old:
                return old
            basis_fields = (
                "objective",
                "research_mode",
                "focus",
                "seeds",
                "experimental_context",
                "constraints",
                "hard_constraints",
            )
            basis_changed = any(
                getattr(old, key) != getattr(changed, key) for key in basis_fields
            )
            changed = changed.model_copy(
                update={
                    "revision": old.revision + 1,
                    "basis_revision": old.basis_revision + int(basis_changed),
                }
            )
            board = await tx.research.get(session.id)
            previous = (
                {item.text: item for item in board.questions}
                if not basis_changed
                else {}
            )
            questions = tuple(
                previous.get(text) or ResearchQuestion(text=text)
                for text in brief.research_questions
            )
            if not basis_changed and not brief.research_questions:
                questions = board.questions
            board = board.model_copy(
                update={
                    "questions": questions,
                    "result": None,
                    "tasks": tuple(
                        item.model_copy(
                            update={"status": "stale", "reason": "研究依据更新"}
                        )
                        if basis_changed
                        and item.basis_revision != changed.basis_revision
                        and item.status not in {"cancelled", "stale"}
                        else item
                        for item in board.tasks
                    ),
                }
            )
            await tx.sessions.save(session.model_copy(update={"brief": changed}))
            await tx.research.save(board)
            current = await tx.runs.get(self.run.id)
            await tx.runs.save(
                current.model_copy(
                    update={"brief": changed, "input_revision": session.input_revision}
                )
            )
            await tx.sessions.append_event(
                session.id,
                self.run.id,
                "task.updated",
                {"brief": changed.model_dump(mode="json")},
            )
            await tx.sessions.append_event(
                session.id,
                self.run.id,
                "research.updated",
                {"research": board.model_dump(mode="json")},
            )
        if basis_changed:
            for task in board.tasks:
                if task.root_run_id == self.run.id and task.status == "stale":
                    await self.callbacks.cancel(task.id, "研究依据更新")
        return changed

    async def record_findings(
        self, findings: list[Finding], question_updates: list[ResearchQuestion] = ()
    ) -> dict:
        """发布有来源的发现和判断；Worker 判断供主 Agent 采用，只有主 Agent 更新权威判断和问题状态。"""
        return await self.research.record(self.run, findings, question_updates)

    async def submit_research(self, result: ResearchSubmission) -> ToolResult:
        """主 Agent 统一提交主题/差异/baseline 结果。按项检查证据、硬条件与版本；合法项保留，未知条件待确认。"""
        accepted = await self.research.submit(self.run, result)
        await self.memory.capture_result(self.run, accepted)
        async with self.uow() as tx:
            brief = (await tx.sessions.get(self.run.session_id)).brief
        report_ids = ()
        if brief.delivery.format == "report":
            from paper_search.reports import ReportService

            report = await ReportService(
                self.run, self.uow, self.artifacts
            ).create_report(title=brief.objective or "论文研究报告")
            report_ids = report.artifact_ids
        return ToolResult(
            content=accepted.markdown,
            data=accepted.model_dump(mode="json"),
            artifact_ids=report_ids,
        )

    async def spawn_research_task(self, task: TaskAssignment) -> dict:
        """立即返回异步 discovery/evidence 任务句柄。证据任务无需查询；依赖引用已有任务，不允许递归委派。"""
        return (await self.callbacks.spawn(task)).model_dump(mode="json")

    async def collect_tasks(
        self,
        task_ids: tuple[str, ...] = (),
        after_cursor: int = 0,
        wait_ms: Annotated[int, Field(ge=0, le=30000)] = 0,
    ) -> dict:
        """按游标获取任务状态与增量发现，可有限等待；不读取完整子对话。"""
        return await self.callbacks.collect(task_ids, after_cursor, wait_ms)

    async def send_task_update(self, task_id: str, instruction: str) -> dict:
        """步骤边界追加指令，已完成 Worker 可在原预算内续接。"""
        return (await self.callbacks.send(task_id, instruction)).model_dump(mode="json")

    async def cancel_research_task(self, task_id: str, reason: str) -> dict:
        """取消任务，保留已取得的原始来源和发现。"""
        return await self.callbacks.cancel(task_id, reason)

    async def read_artifact(
        self,
        artifact_id: str,
        offset: Annotated[int, Field(ge=0)] = 0,
        limit: Annotated[int | None, Field(ge=1, le=6000)] = None,
    ) -> ToolResult:
        """分页读取已保存正文；offset 按字符计数，续读用上次返回的 offset/next_offset。默认页大小适配当前上下文，每页最多 6000 字符。"""
        page_size = limit or min(
            6000,
            max(1000, self.run.execution.model.context_window // 6 - 500),
        )
        async with self.uow() as tx:
            artifact = await tx.artifacts.get(artifact_id)
        if artifact.session_id != self.run.session_id:
            raise ToolInputError("artifact does not belong to this session")
        text = (await self.artifacts.read(artifact.file_id)).decode("utf-8")
        if artifact.kind == "tool_result":
            # Audit artifacts contain ToolResult, while pagination serves its
            # model-visible text, never another layer of escaped JSON.
            text = ToolResult.model_validate_json(text).content
        page = {
            "artifact_id": artifact_id,
            "offset": offset,
            "total": len(text),
            "next_offset": (
                offset + page_size if offset + page_size < len(text) else None
            ),
        }
        return ToolResult(
            content=(
                f"artifact_id={artifact_id}，offset={offset}，total={len(text)}，"
                f"next_offset={page['next_offset']}\n"
                + text[offset : offset + page_size]
            ),
            data=page,
            artifact_ids=(artifact_id,),
        )
