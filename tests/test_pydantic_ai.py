import asyncio
import re
from dataclasses import dataclass

import httpx
import pytest

pytest.importorskip("pydantic_ai")
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from skiva_sdk import SkillSession, SkivaClient
from skiva_sdk.integrations.pydantic_ai import SkivaSkills


async def test_catalog_replaced_between_model_requests_preserving_history(distribution):
    calls = []

    async def model(messages, info):
        instructions = info.instructions
        calls.append(instructions)
        assert instructions.count("<available_skills ") == 1
        version = re.search('snapshot_id="([^"]+)"', instructions)[1]
        catalog = re.search('catalog_digest="([^"]+)"', instructions)[1]
        if len(calls) == 1:
            assert version == "A"
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "load_skill",
                        dict(
                            name="review",
                            expected_snapshot_id=version,
                            expected_catalog_digest=catalog,
                        ),
                    )
                ]
            )
        if len(calls) == 2:
            distribution.head = "B"
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_skill_file",
                        dict(
                            path="review/references/rules.md",
                            expected_snapshot_id="A",
                            expected_catalog_digest=catalog,
                        ),
                    )
                ]
            )
        assert version == "B"
        results = [
            p
            for m in messages
            if hasattr(m, "parts")
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        ]
        assert any(isinstance(p.content, str) and 'snapshot_id="A"' in p.content for p in results)
        assert any(
            isinstance(p.content, dict) and p.content.get("error") == "VERSION_MISMATCH"
            for p in results
        )
        return ModelResponse(parts=[TextPart("updated")])

    @dataclass
    class Deps:
        skills: SkillSession
        business_id: str

    agent = Agent(
        FunctionModel(model),
        deps_type=Deps,
        capabilities=[SkivaSkills(get_session=lambda d: d.skills)],
    )
    async with SkivaClient(
        "https://skiva.test",
        "test-secret",
        consistency="dynamic",
        transport=httpx.MockTransport(distribution),
    ) as sdk:
        async with sdk.bind() as skills:
            result = await agent.run("Review", deps=Deps(skills, "business"))
            assert result.output == "updated"
            assert len(calls) == 3


async def test_concurrent_runs_do_not_share_catalog_state(distribution):
    async def model(messages, info):
        version = re.search('snapshot_id="([^"]+)"', info.instructions)[1]
        await asyncio.sleep(0)
        return ModelResponse(parts=[TextPart(version)])

    agent = Agent(FunctionModel(model), capabilities=[SkivaSkills()])
    async with SkivaClient(
        "https://skiva.test", "test-secret", transport=httpx.MockTransport(distribution)
    ) as sdk:
        async with sdk.bind() as a:
            distribution.head = "B"
            async with sdk.bind() as b:
                results = await asyncio.gather(agent.run("A", deps=a), agent.run("B", deps=b))
                assert [r.output for r in results] == ["A", "B"]
