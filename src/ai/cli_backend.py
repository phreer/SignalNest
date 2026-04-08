"""
cli_backend.py - 本地 AI CLI 工具封装 + 统一调用入口
======================================================
支持三种后端（通过 config.ai.backend 或环境变量 AI_BACKEND 配置）：
  - litellm:    LiteLLM 库（默认），需要 AI_API_KEY
  - claude-cli: `claude --print "<prompt>"`  (Claude Code CLI)
  - codex-cli:  `codex -q "<prompt>"`         (OpenAI Codex CLI)

_call_ai() 是统一入口，供 summarizer.py 和 ai_reader.py 共用。
"""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)
_CLI_TIMEOUT = 120  # 秒


class CLIBackendError(RuntimeError):
    """CLI 调用失败时抛出（非零退出码、超时、找不到可执行文件）"""

    pass


def _combine_messages(messages: list[dict]) -> str:
    """将 OpenAI role/content 消息列表拼成单个字符串，供 CLI 工具使用。"""
    parts = []
    for msg in messages:
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")
        parts.append(f"[{role}]\n{content}")
    return "\n\n".join(parts)


def call_claude_cli(messages: list[dict]) -> str:
    """调用 `claude --print` 并返回 stdout 文本。"""
    prompt = _combine_messages(messages)
    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
        )
    except FileNotFoundError:
        raise CLIBackendError(
            "找不到 claude 命令，请先安装 Claude Code CLI: https://claude.ai/code"
        )
    except subprocess.TimeoutExpired:
        raise CLIBackendError(f"claude CLI 超时（{_CLI_TIMEOUT}s）")
    if result.returncode != 0:
        raise CLIBackendError(
            f"claude CLI 退出码 {result.returncode}: {(result.stderr or '')[:200]}"
        )
    if result.stderr:
        logger.debug(f"claude-cli stderr: {result.stderr[:200]}")
    return result.stdout.strip()


def call_codex_cli(messages: list[dict]) -> str:
    """调用 `codex -q` 并返回 stdout 文本。"""
    prompt = _combine_messages(messages)
    try:
        result = subprocess.run(
            ["codex", "-q", prompt],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
        )
    except FileNotFoundError:
        raise CLIBackendError(
            "找不到 codex 命令，请先安装 OpenAI Codex CLI: npm install -g @openai/codex"
        )
    except subprocess.TimeoutExpired:
        raise CLIBackendError(f"codex CLI 超时（{_CLI_TIMEOUT}s）")
    if result.returncode != 0:
        raise CLIBackendError(
            f"codex CLI 退出码 {result.returncode}: {(result.stderr or '')[:200]}"
        )
    if result.stderr:
        logger.debug(f"codex-cli stderr: {result.stderr[:200]}")
    return result.stdout.strip()


def _call_ai(messages: list[dict], backend: str, call_kwargs: dict) -> str:
    """
    统一 AI 调用入口：LiteLLM 或本地 CLI，返回裸文本（已 strip）。
    供 summarizer.py 和 ai_reader.py 共用。

    Args:
        messages:    OpenAI 格式消息列表，如 [{"role": "system", "content": "..."}, ...]
        backend:     "litellm" | "claude-cli" | "codex-cli"
        call_kwargs: LiteLLM 参数（model, api_key, max_tokens, api_base）。
                     使用 CLI 后端时忽略。

    Returns:
        AI 返回的原始文本（已 strip）。

    Raises:
        CLIBackendError: CLI 调用失败。
        ValueError:      backend 值不合法。
        Exception:       LiteLLM 错误直接透传。
    """
    if backend == "litellm":
        import litellm

        response = litellm.completion(messages=messages, **call_kwargs)
        return response.choices[0].message.content.strip()
    elif backend == "claude-cli":
        return call_claude_cli(messages)
    elif backend == "codex-cli":
        return call_codex_cli(messages)
    else:
        raise ValueError(
            f"未知 AI 后端: {backend!r}，可选值: litellm | claude-cli | codex-cli"
        )


class LiteLLMResponse:
    """Structured result from a single LiteLLM call with tool support."""

    __slots__ = ("tool_calls", "final_text", "reasoning", "usage")

    def __init__(
        self,
        tool_calls: list[dict] | None,
        final_text: str | None,
        reasoning: str | None,
        usage: dict | None,
    ):
        self.tool_calls = tool_calls
        self.final_text = final_text
        # Text content present alongside tool_calls — the model's chain-of-thought
        self.reasoning = reasoning
        # {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}
        self.usage = usage


def call_litellm_with_tools(
    messages: list[dict],
    call_kwargs: dict,
    openai_tools: list[dict],
) -> LiteLLMResponse:
    """
    使用原生 Tool Calling 调用 LiteLLM。仅支持 litellm 后端。

    Returns:
        LiteLLMResponse with:
          - tool_calls: list of {"tool", "arguments", "call_id"} or None
          - final_text: plain text reply when no tool_calls
          - reasoning:  model content text emitted alongside tool_calls (chain-of-thought)
          - usage:      {"prompt_tokens", "completion_tokens", "total_tokens"} or None
    """
    import json as _json
    import litellm

    kw = {**call_kwargs, "tools": openai_tools, "tool_choice": "auto"}
    response = litellm.completion(messages=messages, **kw)
    msg = response.choices[0].message

    # Extract token usage when available
    usage: dict | None = None
    if hasattr(response, "usage") and response.usage:
        try:
            u = response.usage
            usage = {
                "prompt_tokens": int(getattr(u, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(u, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(u, "total_tokens", 0) or 0),
            }
        except Exception:
            pass

    if msg.tool_calls:
        calls = []
        for tc in msg.tool_calls:
            try:
                args = _json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            calls.append(
                {"tool": tc.function.name, "arguments": args, "call_id": tc.id}
            )
        # Capture any chain-of-thought text the model emitted alongside tool calls
        reasoning = (msg.content or "").strip() or None
        return LiteLLMResponse(
            tool_calls=calls, final_text=None, reasoning=reasoning, usage=usage
        )

    final_text = (msg.content or "").strip() or "Done."
    return LiteLLMResponse(
        tool_calls=None, final_text=final_text, reasoning=None, usage=usage
    )
