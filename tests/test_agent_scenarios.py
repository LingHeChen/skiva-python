"""Real Pydantic execution loop with scripted model decisions, never paid inference."""

import json
import re
from xml.etree import ElementTree

import pytest
from scenarios import ScenarioServer

pytest.importorskip("pydantic_ai")
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from skiva_sdk import AuthorizationDenied
from skiva_sdk.integrations.pydantic_ai import SkivaSkills


def versions(info):
    assert info.instructions.count("<available_skills ") == 1
    return dict(
        expected_snapshot_id=re.search('snapshot_id="([^"]+)"', info.instructions)[1],
        expected_catalog_digest=re.search('catalog_digest="([^"]+)"', info.instructions)[1],
    )


def returns(messages):
    return [p.content for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


async def test_multitool_audit_update_rollback_and_history_provenance():
    server = ScenarioServer()
    steps = []

    async def model(messages, info):
        ver = versions(info)
        step = len(steps)
        steps.append(ver["expected_snapshot_id"])
        if step == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart("load_skill", dict(name="expense-audit", **ver)),
                    ToolCallPart("load_skill", dict(name="release-review", **ver)),
                ]
            )
        if step == 1:
            assert len(returns(messages)) == 2
            # Update between discovery/model response and tool execution.
            server.head = "release-2"
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_skill_file", dict(path="expense-audit/references/policy.json", **ver)
                    )
                ]
            )
        if step == 2:
            assert ver["expected_snapshot_id"] == "release-2"
            assert returns(messages)[-1]["error"] == "VERSION_MISMATCH"
            return ModelResponse(
                parts=[ToolCallPart("load_skill", dict(name="expense-audit", **ver))]
            )
        if step == 3:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_skill_file", dict(path="expense-audit/references/policy.json", **ver)
                    )
                ]
            )
        if step == 4:
            body = ElementTree.fromstring(returns(messages)[-1])
            assert body.attrib["snapshot_id"] == "release-2"
            assert json.loads(body.text)["cap"] == 600
            # Rollback makes even the formerly newer loaded content stale.
            server.head = "release-1"
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_skill_file",
                        dict(path="expense-audit/references/currency.json", **ver),
                    )
                ]
            )
        if step == 5:
            assert returns(messages)[-1]["error"] == "VERSION_MISMATCH"
            return ModelResponse(
                parts=[ToolCallPart("load_skill", dict(name="expense-audit", **ver))]
            )
        if step == 6:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_skill_file", dict(path="expense-audit/references/policy.json", **ver)
                    )
                ]
            )
        parsed = ElementTree.fromstring(returns(messages)[-1])
        assert json.loads(parsed.text)["cap"] == 1000
        # Both historical versions survive; this is provenance, not automatic forgetting.
        envelopes = [ElementTree.fromstring(x) for x in returns(messages) if isinstance(x, str)]
        assert {e.attrib["snapshot_id"] for e in envelopes} == {"release-1", "release-2"}
        return ModelResponse(parts=[TextPart("rollback cap=1000; snapshots remain labeled")])

    agent = Agent(FunctionModel(model), capabilities=[SkivaSkills()])
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as session:
            result = await agent.run("Audit synthetic receipts", deps=session)
            assert result.output == "rollback cap=1000; snapshots remain labeled"
    assert steps == [
        "release-1",
        "release-1",
        "release-2",
        "release-2",
        "release-2",
        "release-1",
        "release-1",
        "release-1",
    ]


async def test_new_run_with_old_history_gets_current_catalog():
    server = ScenarioServer()
    seen = []

    async def model(messages, info):
        ver = versions(info)
        seen.append(ver["expected_snapshot_id"])
        return ModelResponse(parts=[TextPart(ver["expected_snapshot_id"])])

    agent = Agent(FunctionModel(model), capabilities=[SkivaSkills()])
    async with server.client() as client:
        async with client.bind() as first:
            result = await agent.run("first", deps=first)
        server.head = "release-2"
        async with client.bind() as second:
            result = await agent.run("continue", deps=second, message_history=result.all_messages())
            assert result.output == "release-2"
    assert seen == ["release-1", "release-2"]


async def test_revocation_aborts_before_next_model_request():
    server = ScenarioServer()
    seen = 0

    async def model(messages, info):
        nonlocal seen
        seen += 1
        ver = versions(info)
        server.revoked.add("all")
        return ModelResponse(parts=[ToolCallPart("load_skill", dict(name="expense-audit", **ver))])

    agent = Agent(FunctionModel(model), capabilities=[SkivaSkills()])
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as session:
            with pytest.raises(AuthorizationDenied):
                await agent.run("audit", deps=session)
    assert seen == 1


async def test_same_snapshot_permission_change_updates_next_request():
    server = ScenarioServer()
    catalogs = []

    async def model(messages, info):
        ver = versions(info)
        catalogs.append(ver)
        if len(catalogs) == 1:
            server.allowed["all"].remove("expense-audit")
            return ModelResponse(
                parts=[ToolCallPart("load_skill", dict(name="expense-audit", **ver))]
            )
        assert catalogs[0]["expected_snapshot_id"] == ver["expected_snapshot_id"]
        assert catalogs[0]["expected_catalog_digest"] != ver["expected_catalog_digest"]
        assert '<skill name="expense-audit"' not in info.instructions
        assert returns(messages)[-1]["error"] == "VERSION_MISMATCH"
        return ModelResponse(parts=[TextPart("permission changed")])

    agent = Agent(FunctionModel(model), capabilities=[SkivaSkills()])
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as session:
            result = await agent.run("audit", deps=session)
    assert result.output == "permission changed"


async def test_streaming_uses_current_discovery():
    server = ScenarioServer()

    async def stream(messages, info):
        ver = versions(info)
        yield "snapshot="
        yield ver["expected_snapshot_id"]

    agent = Agent(FunctionModel(stream_function=stream), capabilities=[SkivaSkills()])
    async with server.client(consistency="dynamic") as client:
        async with client.bind() as session:
            server.head = "release-2"
            async with agent.run_stream("audit", deps=session) as result:
                assert await result.get_output() == "snapshot=release-2"


async def test_unknown_skill_and_missing_reference_are_structured_results():
    server = ScenarioServer()
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        ver = versions(info)
        if requests == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("load_skill", dict(name="not-installed", **ver)),
                    ToolCallPart("read_skill_file", dict(path="expense-audit/missing.md", **ver)),
                ]
            )
        assert {r["error"] for r in returns(messages)} == {"SKILL_NOT_FOUND", "FILE_NOT_FOUND"}
        return ModelResponse(parts=[TextPart("missing resources handled")])

    agent = Agent(FunctionModel(model), capabilities=[SkivaSkills()])
    async with server.client() as client:
        async with client.bind() as skills:
            result = await agent.run("missing", deps=skills)
    assert result.output == "missing resources handled"
