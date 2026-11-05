import asyncio
import json
import os
import time
from contextlib import AsyncExitStack

from dotenv import load_dotenv
from groq import Groq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

load_dotenv()

GROQ_MODEL = "openai/gpt-oss-20b"


def mcp_tool_to_openai_schema(tool) -> dict:
    """Chuyển schema tool của MCP sang format OpenAI/Groq function calling."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    }


async def run_agent():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("Chưa có GROQ_API_KEY trong file .env")

    client = Groq(api_key=api_key)

    server_params = StdioServerParameters(
        command=".venv/bin/python",
        args=["server.py"],
    )

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server_params))
        session: ClientSession = await stack.enter_async_context(
            ClientSession(read, write)
        )
        await session.initialize()

        tools_result = await session.list_tools()
        groq_tools = [mcp_tool_to_openai_schema(t) for t in tools_result.tools]

        print(f"Đã kết nối MCP server. {len(tools_result.tools)} tool khả dụng:")
        for t in tools_result.tools:
            print(f"  - {t.name}: {t.description}")
        print("\nGõ câu hỏi (vd: 'kiểm tra nginx có đang chạy không'), Ctrl+C để thoát.\n")

        messages = [
            {
                "role": "system",
                "content": (
                    "Bạn là trợ lý quản trị hệ thống Linux. "
                    "Dùng các tool được cung cấp để trả lời chính xác, "
                    "trả lời ngắn gọn bằng tiếng Việt."
                ),
            }
        ]

        while True:
            try:
                user_input = input("Bạn: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input:
                continue

            messages.append({"role": "user", "content": user_input})

            try:
                for _ in range(6):  # giới hạn số bước Plan -> Act -> Observe
                    print("  [đang gọi Groq API...]")
                    t0 = time.time()
                    response = client.chat.completions.create(
                        model=GROQ_MODEL,
                        messages=messages,
                        tools=groq_tools,
                        tool_choice="auto",
                    )
                    print(f"  [Groq phản hồi sau {time.time() - t0:.1f}s]")

                    msg = response.choices[0].message
                    messages.append(msg.model_dump(exclude_none=True))

                    if not msg.tool_calls:
                        print(f"Agent: {msg.content}\n")
                        break

                    for tc in msg.tool_calls:
                        args = json.loads(tc.function.arguments or "{}")
                        print(f"  [gọi tool] {tc.function.name}({args})")
                        try:
                            result = await session.call_tool(tc.function.name, args)
                            output_text = "\n".join(
                                c.text for c in result.content if hasattr(c, "text")
                            )
                        except Exception as e:
                            output_text = f"Lỗi khi gọi tool: {e}"

                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": output_text,
                            }
                        )
                else:
                    print("Agent: (dừng vì vượt quá số bước cho phép)\n")

            except Exception as e:
                print(f"Agent: Xin lỗi, có lỗi khi gọi Groq API ({type(e).__name__}): {e}")
                print("  → Thử lại câu hỏi hoặc đợi vài giây rồi hỏi lại.\n")
                messages.pop()  # bỏ lượt hỏi lỗi để không làm hỏng ngữ cảnh


if __name__ == "__main__":
    asyncio.run(run_agent())
