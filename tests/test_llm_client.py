import io
import json
import socket
import urllib.error
from unittest.mock import patch

import pytest

from llm_client import LLMClient, LLMUserError


class FakeResponse:
    def __init__(self, lines=None, body=b""):
        self._lines = lines or []
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._body


def make_client(max_retries=2):
    return LLMClient(
        {
            "api_type": "openai_compat",
            "base_url": "https://api.deepseek.com",
            "api_key": "test-key",
            "timeout": 5,
            "max_retries": max_retries,
        },
        "deepseek-v4-flash",
    )


def test_chat_retries_temporary_dns_failure_and_keeps_model():
    dns_error = urllib.error.URLError(
        socket.gaierror(-3, "Temporary failure in name resolution")
    )
    response = FakeResponse([
        b'data: {"choices":[{"delta":{"content":"OK"}}]}\n',
        b"data: [DONE]\n",
    ])

    with patch("llm_client.time.sleep"), patch(
        "llm_client.urllib.request.urlopen", side_effect=[dns_error, response]
    ) as urlopen:
        result = make_client().chat([{"role": "user", "content": "ping"}])

    assert result == "OK"
    assert urlopen.call_count == 2
    payload = json.loads(urlopen.call_args_list[-1].args[0].data)
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["thinking"] == {"type": "disabled"}


def test_chat_rejects_empty_streamed_response():
    response = FakeResponse([
        b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
        b"data: [DONE]\n",
    ])

    with patch(
        "llm_client.urllib.request.urlopen", return_value=response
    ), pytest.raises(LLMUserError, match="未返回有效答复"):
        make_client(max_retries=0).chat([{"role": "user", "content": "ping"}])


def test_chat_reports_dns_failure_after_retry_budget():
    dns_error = urllib.error.URLError(
        socket.gaierror(-3, "Temporary failure in name resolution")
    )

    with patch("llm_client.time.sleep"), patch(
        "llm_client.urllib.request.urlopen", side_effect=[dns_error, dns_error, dns_error]
    ) as urlopen, pytest.raises(LLMUserError, match="域名解析暂时失败"):
        make_client().chat([{"role": "user", "content": "ping"}])

    assert urlopen.call_count == 3


def test_tool_chat_retries_transient_http_error():
    http_503 = urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions",
        503,
        "Service Unavailable",
        {},
        io.BytesIO(b'{"error":"busy"}'),
    )
    body = json.dumps({
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "ready"},
        }]
    }).encode()

    with patch("llm_client.time.sleep"), patch(
        "llm_client.urllib.request.urlopen",
        side_effect=[http_503, FakeResponse(body=body)],
    ) as urlopen:
        result = make_client().chat_with_tools([], [])

    assert result["message"]["content"] == "ready"
    assert urlopen.call_count == 2


def test_authentication_error_is_not_retried():
    http_401 = urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions",
        401,
        "Unauthorized",
        {},
        io.BytesIO(b'{"error":"invalid key"}'),
    )

    with patch(
        "llm_client.urllib.request.urlopen", side_effect=http_401
    ) as urlopen, pytest.raises(LLMUserError, match="鉴权失败"):
        make_client().chat([{"role": "user", "content": "ping"}])

    assert urlopen.call_count == 1
