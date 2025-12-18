from watchfiles import awatch
import asyncio
import httpx
import os
import pathlib


async def run_watch(path: str):
    async with httpx.AsyncClient() as client:
        async for changes in awatch(path):
            for change, file in changes:
                if os.path.isfile(file):
                    await client.post(
                        "http://localhost:8080/events/artifact",
                        json={
                            "action": "updated",
                            "kind": "file",
                            "uri": pathlib.Path(file).resolve().as_posix(),
                            "metadata": {},
                        },
                    )


if __name__ == "__main__":
    asyncio.run(run_watch(os.getenv("WATCH_PATH", ".wx")))
