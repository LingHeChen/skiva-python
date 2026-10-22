"""Generated transition sequences compare public reads to a release-state oracle."""

import asyncio
import json

from hypothesis import given, settings
from hypothesis import strategies as st
from scenarios import ScenarioServer


@given(st.lists(st.sampled_from(["release-1", "release-2"]), min_size=1, max_size=20))
@settings(max_examples=40, deadline=None, derandomize=True)
def test_generated_publish_rollback_sequences(sequence):
    async def scenario():
        server = ScenarioServer()
        async with server.client() as client:
            async with client.bind() as pinned, client.bind(consistency="dynamic") as dynamic:
                old_ref = pinned.ref
                for release in sequence:
                    server.head = release
                    await dynamic.refresh()
                    fixed = json.loads(
                        await pinned.read_text("expense-audit/references/policy.json")
                    )
                    moving = await dynamic.read_skill_file("expense-audit/references/policy.json")
                    assert fixed["cap"] == 1000
                    assert pinned.ref == old_ref
                    assert moving.ref.snapshot_id == release
                    assert json.loads(moving.text)["cap"] == (
                        1000 if release == "release-1" else 600
                    )

    asyncio.run(scenario())
