"""Terminate expired and untracked jarvis-* RunPod pods; run from a timer as a spend safety net."""
import asyncio
import os
import sys

from jarvis.compute.service import Compute, NotConfigured


async def main():
    try:
        deleted = await Compute().runpod().reap()
    except NotConfigured:
        return
    print(f"reaper: deleted {deleted}")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
