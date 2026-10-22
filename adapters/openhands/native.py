"""Native OpenHands SDK session using the lane and only the world's MCP tools."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

from adapters.environment import agent_environment


def create_conversation(
    *,
    run_id,
    run_token,
    model,
    workspace,
    lane_url="http://127.0.0.1:8081/openai/v1",
    world_url="http://127.0.0.1:7778",
    python=sys.executable,
    max_steps=100,
    output_tokens=2048,
    reasoning_effort=None,
    callbacks=None,
):
    from openhands.sdk import LLM, Agent, Conversation
    from openhands.sdk.mcp import MCPServer
    from pydantic import SecretStr

    if (
        not run_token
        or not isinstance(run_id, str)
        or not run_id.startswith("r_")
        or not 1 <= max_steps <= 1000
    ):
        raise ValueError("A real run capability and bounded native session are required")
    llm = LLM(
        model="openai/" + model.removeprefix("openai/"),
        api_key=SecretStr(run_token),
        base_url=lane_url,
        extra_headers={"X-World-Run-Id": run_id, "X-World-Run-Token": run_token},
        usage_id="landing-party",
        num_retries=0,
        max_output_tokens=output_tokens,
        reasoning_effort=reasoning_effort,
        drop_params=False,
    )
    server = MCPServer(
        command=python,
        args=["-m", "worldd.mcp"],
        env={
            "WORLD_RUN_ID": SecretStr(run_id),
            "WORLD_BASE_URL": SecretStr(world_url),
            "WORLD_RUN_TOKEN": SecretStr(run_token),
        },
    )
    agent = Agent(
        llm=llm, tools=[], mcp_config={"world": server}, include_default_tools=["FinishTool"]
    )
    return Conversation(
        agent=agent,
        workspace=Path(workspace),
        max_iteration_per_run=max_steps,
        visualizer=None,
        callbacks=callbacks or [],
        delete_on_close=True,
    )


def run_session(*, briefing, goal="", transcript=None, **kwargs):
    with (
        Path(transcript).open("x", encoding="utf-8") if transcript else nullcontext(None)
    ) as output:

        def record(event):
            if output:
                output.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
                output.flush()

        conversation = create_conversation(callbacks=[record], **kwargs)
        try:
            conversation.send_message("\n\n".join(part for part in (briefing, goal) if part))
            conversation.run()
            return {
                "status": str(conversation.state.execution_status),
                "harness": "openhands-sdk",
                "run_id": kwargs["run_id"],
            }
        finally:
            conversation.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=os.environ.get("WORLD_RUN_ID"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--briefing", type=Path, required=True)
    parser.add_argument("--goal", type=Path)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--lane-url", default="http://127.0.0.1:8081/openai/v1")
    parser.add_argument("--world-url", default="http://127.0.0.1:7778")
    parser.add_argument("--max-steps", type=int, default=100)
    args = parser.parse_args()
    token = os.environ.get("WORLD_RUN_TOKEN")
    environment = agent_environment(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    for key in (
        "WORLD_CONTROL_TOKEN",
        "WORLD_OPENAI_API_KEY",
        "WORLD_ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        os.environ.pop(key, None)
    print(
        json.dumps(
            run_session(
                run_id=args.run_id,
                run_token=token,
                model=args.model,
                workspace=args.workspace,
                briefing=args.briefing.read_text(),
                goal=args.goal.read_text() if args.goal else "",
                transcript=args.transcript,
                lane_url=args.lane_url,
                world_url=args.world_url,
                max_steps=args.max_steps,
            )
        )
    )


if __name__ == "__main__":
    main()
