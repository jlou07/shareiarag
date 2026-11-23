"""Calls the local MCP server like a client would: python test_client.py "query" [token|none]."""

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:8765/mcp"


async def main(query: str, token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])
            res = await session.call_tool("search", {"query": query})
            if res.isError:
                print("search error:", res.content[0].text)
                return
            results = json.loads(res.content[0].text)["results"]
            print(f"search -> {len(results)} results")
            for r in results[:8]:
                print(f"  {r['score']:.2f}  {r['title']}  {r['url']}")
            if results:
                f = await session.call_tool("fetch", {"id": results[0]["id"]})
                if f.isError:
                    print("fetch error:", f.content[0].text)
                    return
                doc = json.loads(f.content[0].text)
                total_chars = len(doc.get("text", ""))
                segments = 1
                while doc.get("next_offset") is not None:
                    f = await session.call_tool(
                        "fetch", {"id": results[0]["id"], "offset": doc["next_offset"]}
                    )
                    if f.isError:
                        print("fetch error:", f.content[0].text)
                        return
                    doc = json.loads(f.content[0].text)
                    total_chars += len(doc.get("text", ""))
                    segments += 1
                print(f"fetch -> {doc.get('title')} "
                      f"chunks={doc.get('metadata', {}).get('total_chunks')} "
                      f"chars={total_chars} segments={segments} error={doc.get('error')}")


if __name__ == "__main__":
    tok = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "none" else None
    asyncio.run(main(sys.argv[1], tok))
