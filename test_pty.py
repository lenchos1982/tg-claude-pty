#!/usr/bin/env python3
"""
Test script for PtyBridge.

Launches Claude Code via PTY, handles startup dialogs, sends a prompt,
and prints the response.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pty_bridge import PtyBridge


async def main():
    prompt = " ".join(sys.argv[1:]) or "What is 2+2? Answer in one word."

    print(f"Starting Claude Code via PTY...")
    print(f"Prompt: {prompt}")

    bridge = PtyBridge()

    try:
        bridge.start()
        print("[Claude is ready]")
    except Exception as e:
        print(f"ERROR: Failed to start Claude: {e}")
        sys.exit(1)

    try:
        print(f"\n--- Sending prompt ---")
        response = await bridge.send(prompt, timeout=120.0)
        print(f"\n{'='*60}")
        print("=== RESPONSE ===")
        print(response)
        print("=== END ===")
    except Exception as e:
        print(f"ERROR during send: {e}")
    finally:
        print("\nStopping Claude...")
        await bridge.stop()

    if response:
        print(f"\n✅ SUCCESS ({len(response)} chars)")
    else:
        print(f"\n❌ FAILED: No response")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
