"""Agent Skills catalog with immutable, per-run snapshots and bounded references."""

from pathlib import Path

import yaml

from paper_search.domain import Run, SkillUse, ToolInputError, ToolResult


class SkillCatalog:
    def __init__(self, path: Path, uow, artifacts):
        self.path, self.uow, self.artifacts = path.resolve(), uow, artifacts
        self.entries = {}
        for file in sorted(path.glob("*/SKILL.md")):
            text = file.read_text(encoding="utf-8")
            header = yaml.safe_load(text.split("---", 2)[1])
            self.entries[header["name"]] = {
                "name": header["name"],
                "description": header["description"],
                "version": str(header.get("metadata", {}).get("version", "1")),
                "file": file,
            }

    def catalog(self):
        return [
            {key: value for key, value in item.items() if key != "file"}
            for item in self.entries.values()
        ]

    async def load(self, run, name):
        if name not in self.entries:
            raise ToolInputError("未知 Skill")
        entry = self.entries[name]
        async with self.uow() as tx:
            current = await tx.runs.get(run.id)
            existing = next(
                (item for item in current.skills if item.name == name), None
            )
        if existing:
            content = (await self.artifacts.read(existing.file_id)).decode()
        else:
            content = entry["file"].read_text(encoding="utf-8")
            file_id = await self.artifacts.put(content.encode())
            references = {
                file.relative_to(
                    entry["file"].parent
                ).as_posix(): await self.artifacts.put(file.read_bytes())
                for file in sorted(entry["file"].parent.rglob("*"))
                if file.is_file() and file.name != "SKILL.md"
            }
            existing = SkillUse(
                name=name,
                version=entry["version"],
                file_id=file_id,
                references=references,
            )
            async with self.uow() as tx:
                current = await tx.runs.get(run.id)
                if not any(item.name == name for item in current.skills):
                    await tx.runs.save(
                        current.model_copy(
                            update={"skills": (*current.skills, existing)}
                        )
                    )
                await tx.sessions.append_event(
                    run.session_id,
                    run.id,
                    "skill.used",
                    {
                        "name": name,
                        "version": existing.version,
                        "action": "load",
                        "file_id": existing.file_id,
                    },
                )
                await tx.sessions.append_event(
                    run.session_id,
                    run.id,
                    "run.updated",
                    {"run": (await tx.runs.get(run.id)).model_dump(mode="json")},
                )
        return ToolResult(content=content, data=existing.model_dump(mode="json"))

    async def reference(self, run, name, relative_path):
        async with self.uow() as tx:
            current = await tx.runs.get(run.id)
            skill = next((item for item in current.skills if item.name == name), None)
            key = relative_path.replace("\\", "/")
            if not skill or key not in skill.references:
                raise ToolInputError(
                    "请先加载 Skill；引用必须属于该 Run 已保存的支持资料"
                )
            skill = skill.model_copy(
                update={
                    "loaded_references": tuple(
                        dict.fromkeys((*skill.loaded_references, key))
                    )
                }
            )
            await tx.runs.save(
                current.model_copy(
                    update={
                        "skills": tuple(
                            skill if item.name == name else item
                            for item in current.skills
                        )
                    }
                )
            )
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "skill.used",
                {
                    "name": name,
                    "version": skill.version,
                    "action": "reference",
                    "file_id": skill.references[key],
                    "reference": key,
                },
            )
            await tx.sessions.append_event(
                run.session_id,
                run.id,
                "run.updated",
                {"run": (await tx.runs.get(run.id)).model_dump(mode="json")},
            )
        return ToolResult(
            content=(await self.artifacts.read(skill.references[key])).decode(),
            data={
                "name": name,
                "version": skill.version,
                "reference": key,
                "file_id": skill.references[key],
            },
        )


class SkillTools:
    def __init__(self, run: Run, skills: SkillCatalog):
        self.run, self.skills = run, skills

    async def load_skill(self, name: str) -> ToolResult:
        """按需加载 Skill 正文；同 Run 恢复使用已保存版本。工具/任务生命周期仍由 Harness 执行。"""
        return await self.skills.load(self.run, name)

    async def read_skill_reference(self, name: str, relative_path: str) -> ToolResult:
        """读取已加载业务 Skill 的支持资料，路径相对于该 Skill 目录。"""
        return await self.skills.reference(self.run, name, relative_path)
