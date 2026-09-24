"""QwenPaw Http backend — drive QwenPaw as an ACP client over Http JSON-RPC.
"""

from __future__ import annotations

import asyncio
import re
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
from .qwenpaw_remote_client import QwenPawRemoteClient
from deeptutor.multi_user.context import get_current_user

_SESSION_LINE = re.compile(r"^\s*session_id:\s*(\S+)\s*$", re.IGNORECASE)
logger = logging.getLogger(__name__)



class QwenPawRemoteBackend(SubagentBackend):
    """QwenPaw Remote backend that drives QwenPaw via ``qwenpaw Remote``.

    The backend spawns ``qwenpaw Remote`` as a subprocess and communicates using
    Agent Client Protocol (Remote) over JSON-RPC on stdio. Each message is a
    JSON object sent/received on a separate line

    """

    kind = "qwenpaw_remote"
    display_name = "QwenPaw Remote"
    cli_command = "qwenpaw"

    async def detect(self) -> DetectResult:
        sdk, server_version = _qwenpaw_available()
        detail = ""
        if not sdk:
            detail += "QwenPaw Server not start. "
        return DetectResult(
            kind=self.kind,
            display_name=self.display_name,
            available=sdk,
            version=server_version if sdk else "",
            detail=detail,
        )

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
        sid = session_id or f"{uuid.uuid4().hex}-deeptutor"

        result = ConsultResult(session_id=sid)
        answer_lines: list[str] = []
        reasoning_parts: list[str] = []
        reasoning_active = False
        returncode = "0"

        # Send the initial prompt via stdin (as ACP prompt message)
        prompt = question
        if config.system_prompt.strip() and not session_id:
            prompt = f"{config.system_prompt.strip()}\n\n{question}"
        if images:
            prompt += "\n\nAttached local files:\n" + "\n".join(f"- {path}" for path in images)
        # Send as ACP prompt via sessionUpdate/init pattern
        prompt_msgs =[{"type":"text","text":prompt}]

        async def emit(kind: str, text: str, raw: dict[str, Any] | None = None) -> None:
            result.event_count += 1
            await on_event(SubagentEvent(kind=kind, text=text, raw=raw or {}))

        try:
            current_user = get_current_user()
            qwenpaw_client = QwenPawRemoteClient(config.base_url,agent_id=partner_id,username=current_user.username)
            if images: # read images content
                qwenpaw_result = qwenpaw_client.process_directory(prompt_msgs,images,cwd,session_id=sid)
            else:
                qwenpaw_result = qwenpaw_client.prompt(prompt_msgs,cwd,session_id=sid)
            result.success = qwenpaw_result['success']
            result.final_text = qwenpaw_result['content']
            result.error = qwenpaw_result['error']
            if not result.success:
                await emit(EVENT_ERROR, result.error, {})
            elif not result.final_text and result.success:
                result.success = False
                result.error = "qwenpaw acp returned no answer"
                await emit(EVENT_ERROR, result.error, {})

            return result

        except Exception as exc:
            logger.warning("qwenpaw client sdk consult failed: %s", exc, exc_info=True)

        try:
            cmd = self._build_cli_question_command(question, cwd=cwd,agent_id=partner_id,config=config, images=images)

            async for channel, line in stream_process_lines(cmd, cwd=cwd):
                if channel == "exit":
                    returncode = line
                    continue
                if channel == "stderr":
                    match = _SESSION_LINE.match(line)
                    if match:
                        result.session_id = match.group(1)
                    elif line.strip():
                        await emit(EVENT_LOG, line, {"stream": "stderr"})
                    continue
                answer_lines.append(line)
                text = "\n".join(answer_lines).strip()
                if text:
                    await emit(
                        EVENT_TEXT,
                        text,
                        {"stream": "stdout"},
                        {"merge_id": "qwenpaw:final"},
                    )
        except Exception as exc:  # pragma: no cover - defensive process boundary
            logger.warning("qwenpaw consult failed: %s", exc, exc_info=True)
            result.success = False
            result.error = str(exc)
            await emit(EVENT_ERROR, result.error, {})

        result.final_text = "\n".join(answer_lines).strip()
        if returncode != "0" and result.success:
            result.success = False
            result.error = f"qwenpaw exited with code {returncode}"
            await emit(EVENT_ERROR, result.error, {"returncode": returncode})
        elif not result.final_text and result.success:
            result.success = False
            result.error = "qwenpaw returned no answer"
            await emit(EVENT_ERROR, result.error, {})
        return result



    def _build_cli_question_command(
        self, question: str, *, cwd: str, agent_id: str, config: BackendConfig, images: list[str] | None = None
    ) -> list[str]:
        """Build command for asking a question; prompt passed via stdin."""
        cmd = [self.cli_command, "agents", "chat"]
        if cwd:
            cmd += ["--workspace", cwd]
        if agent_id:
            cmd += ["--to-agent", agent_id]
        if config.model:
            cmd += ["--model", config.model]
        if config.effort:
            cmd += ["--reasoning-effort", config.effort]
        cmd += list(config.extra_args)
        cmd += ["--text",question]
        return cmd

    def _env(self) -> dict[str, str]:
        """Return environment variables for the qwenpaw subprocess."""
        env = os.environ.copy()
        current_user = get_current_user()
        env['current_user'] = current_user.username
        # Ensure PATH is available; qwenpaw must be on PATH or configured
        return env


def _qwenpaw_available() -> bool:
    """Check if the qwenpaw Python package is installed."""
    try:
        qwenpaw_client = QwenPawRemoteClient()
        status,text = qwenpaw_client.version()
        return status==200,text
    except (ImportError, ValueError):
        return False,''


__all__ = ["QwenPawRemoteBackend"]