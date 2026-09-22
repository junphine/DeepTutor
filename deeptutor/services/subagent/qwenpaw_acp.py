"""QwenPaw ACP backend — drive QwenPaw as an ACP client over stdio JSON-RPC.

This backend spawns ``qwenpaw acp`` and exchanges Agent Client Protocol (ACP)
messages via JSON-RPC over stdio. It follows the same patterns as the
DeepSeekHarnessBackend: detect availability, then consult with streaming events.

ACP mode supported:
  - QwenPaw as an ACP server: external clients connect to QwenPaw over ACP
  - QwenPaw using ACP as a tool: QwenPaw delegates to external ACP runners

This backend implements the "QwenPaw as ACP server" mode, where an external
client (e.g. DeepTutor) drives QwenPaw by sending ACP messages through stdio
JSON-RPC.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import uuid
from typing import Any

from deeptutor.services.subagent.base import OnEvent, SubagentBackend
from deeptutor.services.subagent.config import BackendConfig
from deeptutor.services.subagent.types import (
    EVENT_ERROR,
    EVENT_LOG,
    EVENT_REASONING,
    EVENT_TEXT,
    EVENT_TOOL,
    EVENT_TOOL_RESULT,
    ConsultResult,
    DetectResult,
    SubagentEvent,
)
from deeptutor.services.subagent.process import (
    not_found_detail,
    probe_version,
    stream_process_lines,
)
logger = logging.getLogger(__name__)


class QwenPawACPBackend(SubagentBackend):
    """QwenPaw ACP backend that drives QwenPaw via ``qwenpaw acp``.

    The backend spawns ``qwenpaw acp`` as a subprocess and communicates using
    Agent Client Protocol (ACP) over JSON-RPC on stdio. Each message is a
    JSON object sent/received on a separate line.

    Supported ACP event types:
      - ``assistant/chunk``: streaming text or reasoning delta
      - ``assistant/message``: final message complete
      - ``tool/call``: tool invocation
      - ``tool/result``: tool result
      - ``turn/end``: turn completion (success or error)
    """

    kind = "qwenpaw_acp"
    display_name = "QwenPaw ACP"
    cli_command = "qwenpaw"

    async def detect(self) -> DetectResult:
        ok, text = await probe_version([self.cli_command, "--version"])
        sdk = _qwenpaw_available()
        return DetectResult(
            kind=self.kind,
            display_name=self.display_name,
            available=ok or sdk,
            version=text if ok else ("qwenpaw Python package" if sdk else ""),
            detail="" if ok or sdk else not_found_detail(text, "qwenpaw CLI / Python package not found"),
        )

    def _build_acp_command(
        self, question: str, *, config: BackendConfig, images: list[str] | None = None
    ) -> list[str]:
        """Build the ``qwenpaw acp`` command line.

        The ``qwenpaw acp`` command reads prompts from stdin and outputs ACP
        messages as JSON on stdout (one JSON object per line). Stderr carries
        diagnostic/log output.
        """
        cmd = [self.cli_command, "acp"]
        if config.model:
            cmd += ["--model", config.model]
        if config.effort:
            cmd += ["--reasoning-effort", config.effort]
        cmd += list(config.extra_args)
        # The prompt is passed via stdin; we include a marker so the agent
        # knows this is the initial question.
        return cmd

    async def consult(
        self,
        question: str,
        *,
        on_event: OnEvent,
        cwd: str | None = None,
        session_id: str | None = None,
        config: BackendConfig | None = None,
        images: list[str] | None = None,
        partner_id: str | None = None,  # partner_id is assigned from agent_id
    ) -> ConsultResult:
        config = config or BackendConfig()
        sid = session_id or f"deeptutor-{uuid.uuid4().hex}"

        cmd = self._build_acp_question_command(question, cwd=cwd, agent_id=partner_id, config=config, images=images)
        result = ConsultResult(session_id=sid)
        answer_parts: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_active = False
        returncode = "0"

        async def emit(kind: str, text: str, raw: dict[str, Any] | None = None) -> None:
            result.event_count += 1
            await on_event(SubagentEvent(kind=kind, text=text, raw=raw or {}))

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd or None,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )

            # Send the initial prompt via stdin
            prompt = question
            if config.system_prompt.strip() and not session_id:
                prompt = f"{config.system_prompt.strip()}\n\n{question}"
            if images:
                prompt += "\n\nAttached local files:\n" + "\n".join(f"- {path}" for path in images)
            process.stdin.write((prompt + "\n").encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()

            async def _read_stderr() -> None:
                """Read stderr lines and emit LOG events."""
                assert process.stderr is not None
                async for line in process.stderr:
                    stripped = line.decode("utf-8", errors="replace").strip()
                    if stripped:
                        await emit(EVENT_LOG, stripped, {"stream": "stderr"})

            stderr_task = asyncio.create_task(_read_stderr())

            # Read stdout line by line (each line is a JSON ACP message)
            assert process.stdout is not None
            async for raw_line in process.stdout:
                raw_line = raw_line.decode("utf-8", errors="replace").strip()
                if not raw_line:
                    continue
                try:
                    message = json.loads(raw_line)
                except json.JSONDecodeError:
                    await emit(EVENT_LOG, f"Invalid ACP JSON: {raw_line[:100]}", {"stream": "stderr"})
                    continue

                await self._handle_acp_message(message, emit)

            returncode = str(process.returncode) if process.returncode is not None else "0"

            await stderr_task

        except Exception as exc:
            logger.warning("qwenpaw ACP consult failed: %s", exc, exc_info=True)
            result.success = False
            result.error = str(exc)
            await emit(EVENT_ERROR, result.error, {})

        result.final_text = "\n".join(answer_parts).strip()
        if returncode != "0" and result.success:
            result.success = False
            result.error = f"qwenpaw acp exited with code {returncode}"
            await emit(EVENT_ERROR, result.error, {"returncode": returncode})
        elif not result.final_text and result.success:
            result.success = False
            result.error = "qwenpaw acp returned no answer"
            await emit(EVENT_ERROR, result.error, {})

        return result

    def _build_acp_question_command(
        self, question: str, *, cwd: str, agent_id: str, config: BackendConfig, images: list[str] | None = None
    ) -> list[str]:
        """Build command for asking a question; prompt passed via stdin."""
        cmd = [self.cli_command, "acp"]
        if cwd:
            cmd += ["--workspace",cwd]
        if agent_id:
            cmd += ["--agent",agent_id]
        if config.model:
            cmd += ["--model", config.model]
        if config.effort:
            cmd += ["--reasoning-effort", config.effort]
        cmd += list(config.extra_args)
        return cmd

    async def _handle_acp_message(
        self, message: dict[str, Any], emit: callable
    ) -> None:
        """Handle a single ACP JSON message and emit appropriate events."""

        method = str(message.get("method") or "")
        params = message.get("params") or {}

        if method == "assistant/chunk":
            delta = str(params.get("delta") or "")
            chunk_type = str(params.get("type") or "")
            if chunk_type == "text-delta":
                # Accumulate text
                if not hasattr(self, "_text_buffer"):
                    self._text_buffer = ""
                self._text_buffer += delta
                text = self._text_buffer
                await emit(EVENT_TEXT, text, {"merge_id": "qwenpaw:final"})
            elif chunk_type == "reasoning-delta":
                if not hasattr(self, "_reasoning_buffer"):
                    self._reasoning_buffer = ""
                self._reasoning_buffer += delta
                text = self._reasoning_buffer
                await emit(EVENT_REASONING, text, {"merge_id": "qwenpaw:reasoning"})

        elif method == "assistant/message":
            # Final message complete - flush any buffered text/reasoning
            if hasattr(self, "_text_buffer"):
                text = self._text_buffer
                await emit(EVENT_TEXT, text, {"merge_id": "qwenpaw:final"})
                del self._text_buffer
            if hasattr(self, "_reasoning_buffer"):
                text = self._reasoning_buffer
                await emit(EVENT_REASONING, text, {"merge_id": "qwenpaw:reasoning"})
                del self._reasoning_buffer

            # Extract final text from params if available
            text = str(params.get("text") or "")
            if text:
                await emit(EVENT_TEXT, text, {})
            else:
                await emit(EVENT_TEXT, "", {})

        elif method == "tool/call":
            name = str(params.get("name") or "tool")
            arguments = str(params.get("arguments") or "").strip()
            text = f"{name}({arguments})" if arguments else name
            await emit(EVENT_TOOL, text, {"merge_id": f"qwenpaw:tool:{params.get('id', '')}"})

        elif method == "tool/result":
            name = str(params.get("name") or "tool")
            text = str(params.get("text") or params.get("result") or "(empty result)")
            await emit(EVENT_TOOL_RESULT, text, {"merge_id": f"qwenpaw:tool-result:{params.get('id', '')}"})

        elif method == "turn/end":
            kind = str(params.get("kind") or "")
            if kind == "error":
                detail = str(params.get("message") or "QwenPaw ACP turn failed")
                await emit(EVENT_ERROR, detail, params)
            else:
                # turn completed successfully - final text may already be emitted
                pass

        else:
            # Unknown ACP method - log it
            await emit(EVENT_LOG, f"Unknown ACP method: {method}", {"raw": message})

    def _env(self) -> dict[str, str]:
        """Return environment variables for the qwenpaw subprocess."""
        env = os.environ.copy()
        # Ensure PATH is available; qwenpaw must be on PATH or configured
        return env


def _qwenpaw_available() -> bool:
    """Check if the qwenpaw Python package is installed."""
    try:
        return importlib.util.find_spec("qwenpaw") is not None
    except (ImportError, ValueError):
        return False


__all__ = ["QwenPawACPBackend"]