from __future__ import annotations

from typing import Annotated

from pydantic import Field

from paper_search.diagnostics import current_tool_call
from paper_search.domain import (
    Artifact,
    MissingRecord,
    Paper,
    Run,
    ToolInputError,
    ToolResult,
)
from paper_search.ports import ArtifactStore, UowFactory
from paper_search.research import valid_result


def bibtex(papers: list[Paper]) -> str:
    def escape(text: str) -> str:
        replacements = {
            "\\": "\\textbackslash{}",
            **{char: "\\" + char for char in "{}%&#_$"},
        }
        return "".join(replacements.get(char, char) for char in text)

    entries = []
    for index, paper in enumerate(papers, start=1):
        key = f"paper_{index:02d}"
        fields = {
            "title": paper.title,
            "author": " and ".join(paper.authors),
            "year": str(paper.year) if paper.year else "",
            "journal": paper.venue or "",
            "doi": paper.doi or "",
            "url": str(paper.primary_url or ""),
        }
        lines = [
            f"@article{{{key},",
            *(
                f"  {name} = {{{escape(value)}}},"
                for name, value in fields.items()
                if value
            ),
            "}",
        ]
        entries.append("\n".join(lines))
    return "\n\n".join(entries) + "\n"


class ReportService:
    def __init__(self, run: Run, uow: UowFactory, artifacts: ArtifactStore):
        self.run, self.uow, self.artifacts = run, uow, artifacts

    async def create_report(
        self,
        title: Annotated[str, Field(min_length=1, max_length=200)],
        previous_id: str | None = None,
    ) -> ToolResult:
        """将当前已提交 ResearchResult 原样保存为报告；回答、选择和报告共享一份结果。"""
        async with self.uow() as tx:
            current = await tx.runs.get(self.run.id)
            session = await tx.sessions.get(self.run.session_id)
            board = await tx.research.get(self.run.session_id)
            if not valid_result(board.result, session.brief, current):
                raise ToolInputError("请先提交当前研究结果")
            report_id = (
                f"report-{self.run.id}-{current_tool_call.get()}"
                if current_tool_call.get()
                else None
            )
            if report_id:
                try:
                    existing = await tx.artifacts.get(report_id)
                except MissingRecord:
                    existing = None
                if existing:
                    return ToolResult(
                        content=f"报告已保存：{existing.title}",
                        data=existing.model_dump(mode="json"),
                        artifact_ids=(existing.id,),
                    )
            if previous_id:
                previous = await tx.artifacts.get(previous_id)
                if previous.session_id != session.id or previous.kind != "report":
                    raise ToolInputError("报告版本不属于本会话")
        file_id = await self.artifacts.put(board.result.markdown.encode("utf-8"))
        artifact = Artifact(
            **({"id": report_id} if report_id else {}),
            session_id=self.run.session_id,
            run_id=self.run.id,
            kind="report",
            title=title,
            file_id=file_id,
            mime_type="application/json"
            if session.brief.delivery.format == "json"
            else "text/markdown",
            previous_id=previous_id,
        )
        async with self.uow() as tx:
            from paper_search.research import current_basis

            await current_basis(tx, self.run)
            current_board = await tx.research.get(self.run.session_id)
            if (
                not current_board.result
                or current_board.result.result_revision != board.result.result_revision
            ):
                raise ToolInputError("结果已更新，请重新保存")
            await tx.artifacts.save(artifact)
            await tx.sessions.append_event(
                self.run.session_id,
                self.run.id,
                "artifact.created",
                {"artifact": artifact.model_dump(mode="json")},
            )
        return ToolResult(
            content=f"报告已保存：{title}，artifact_id={artifact.id}",
            data=artifact.model_dump(mode="json"),
            artifact_ids=(artifact.id,),
        )
