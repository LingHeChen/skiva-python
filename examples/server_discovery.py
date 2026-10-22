"""Read your real Skiva server using SKIVA_SERVER_URL / SKIVA_CLIENT_TOKEN."""

import asyncio

from skiva_sdk import SkivaClient


async def main():
    async with SkivaClient.from_env() as client:
        async with client.bind(consistency="dynamic") as skills:
            catalog = await skills.catalog()
            print(catalog.render())
            for skill in catalog.skills:
                content = await skills.load_skill(
                    skill.name,
                    expected_snapshot_id=catalog.ref.snapshot_id,
                    expected_catalog_digest=catalog.catalog_digest,
                )
                # Metadata only; do not print potentially private Skill bodies by default.
                print(content.path, content.ref.snapshot_id, content.digest)


if __name__ == "__main__":
    asyncio.run(main())
