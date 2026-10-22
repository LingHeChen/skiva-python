"""Pydantic AI 2.59.0: refresh discovery before every model request.

Only discovery instructions are replaced; historical tool results remain intact.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import InstructionPart
from pydantic_ai.toolsets import FunctionToolset

from ..errors import SkillNotFound, VersionMismatch
from ..view import SkillSession, SnapshotView


class SkivaSkills(AbstractCapability[Any]):
    """Stateless capability; deps supply a separate session per run.

    Pass get_session=lambda deps: deps.skills to preserve business dependencies.
    The application owns the session lifecycle.
    """

    id = "skiva-skills"

    def __init__(
        self, get_session: Callable[[Any], SkillSession | SnapshotView] = lambda deps: deps
    ):
        self._get_session = get_session

    def _session(self, ctx) -> SkillSession | SnapshotView:
        session = self._get_session(ctx.deps)
        if not isinstance(session, (SkillSession, SnapshotView)):
            raise TypeError(
                "SkivaSkills requires a SkillSession or SnapshotView in run dependencies"
            )
        return session

    def get_instructions(self):
        async def catalog_instructions(ctx: RunContext[Any]) -> InstructionPart:
            session = self._session(ctx)
            if isinstance(session, SkillSession):
                await session.refresh()
            catalog = await session.catalog()
            return InstructionPart(
                catalog.render(), name="catalog", dynamic=True, on_change="rewrite"
            )

        return catalog_instructions

    def get_toolset(self):
        toolset = FunctionToolset(id="skiva-skills-tools")

        @toolset.tool
        async def load_skill(
            ctx: RunContext[Any],
            name: str,
            expected_snapshot_id: str,
            expected_catalog_digest: str,
        ) -> str | dict[str, str]:
            """Load Skill instructions using the exact versions from the current catalog.

            Args:
                name: Skill name advertised in available_skills.
                expected_snapshot_id: snapshot_id from available_skills.
                expected_catalog_digest: catalog_digest from available_skills.
            """
            try:
                result = await self._session(ctx).load_skill(
                    name,
                    expected_snapshot_id=expected_snapshot_id,
                    expected_catalog_digest=expected_catalog_digest,
                )
                return result.render()
            except VersionMismatch as e:
                return {
                    "error": "VERSION_MISMATCH",
                    "expected": e.expected,
                    "actual": e.actual,
                    "action": "Use the refreshed catalog on the next request and reload.",
                }
            except SkillNotFound:
                return {"error": "SKILL_NOT_FOUND", "action": "Consult the current catalog."}

        @toolset.tool
        async def read_skill_file(
            ctx: RunContext[Any],
            path: str,
            expected_snapshot_id: str,
            expected_catalog_digest: str,
        ) -> str | dict[str, str]:
            """Read a UTF-8 reference; never silently change versions.

            Args:
                path: Manifest-relative path, including the Skill directory.
                expected_snapshot_id: Snapshot accompanying the loaded Skill.
                expected_catalog_digest: Catalog digest accompanying the loaded Skill.
            """
            try:
                result = await self._session(ctx).read_skill_file(
                    path,
                    expected_snapshot_id=expected_snapshot_id,
                    expected_catalog_digest=expected_catalog_digest,
                )
                return result.render()
            except VersionMismatch as e:
                return {
                    "error": "VERSION_MISMATCH",
                    "expected": e.expected,
                    "actual": e.actual,
                    "action": "Reload the Skill from the current catalog before its references.",
                }
            except SkillNotFound:
                return {"error": "FILE_NOT_FOUND", "action": "Consult the Skill's references."}

        return toolset
