import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from kova.config import load_config
from kova.memory import Memory
from kova.store import Store
from kova.tools import ToolContext, ToolError, build_tools, openai_schemas
from kova.tools import web


@pytest.fixture
def ctx(tmp_path):
    config = load_config(tmp_path)
    config.files_path = tmp_path / 'files'
    now = datetime(2026, 11, 12, 10, tzinfo=timezone(timedelta(hours=1)))
    return ToolContext(config, Store(tmp_path / 'db'), Memory(tmp_path / 'memory'), lambda: now)


def test_build_tools_names_and_schemas(ctx):
    tools = build_tools(ctx)
    assert set(tools) == {'web_search', 'web_fetch', 'list_files', 'read_file', 'write_file', 'reminder_add', 'reminder_list', 'job_add', 'job_list', 'job_remove', 'skill_load', 'skill_list', 'skill_write', 'skill_remove', 'memory_add', 'memory_patch', 'memory_remove', 'memory_list', 'memory_search', 'memory_save', 'recall', 'search_chats', 'system_info', 'search_files', 'edit_file', 'ask_user', 'run_command', 'update_plan'}
    assert all(s['function']['parameters']['type'] == 'object' for s in openai_schemas(tools))


def test_files_roundtrip(ctx):
    tools = build_tools(ctx)
    tools['write_file'].run({'path': 'sub/a.txt', 'content': 'hello'})
    assert tools['read_file'].run({'path': 'sub/a.txt'}) == 'hello'
    assert 'a.txt' in tools['list_files'].run({'path': 'sub'})


def test_files_refuse_escape(ctx, tmp_path):
    tools = build_tools(ctx)
    ctx.config.files_path.mkdir()
    (tmp_path / 'outside').mkdir()
    (ctx.config.files_path / 'link').symlink_to(tmp_path / 'outside')
    for path in ('../x', '/etc/passwd', 'link/x'):
        with pytest.raises(ToolError, match='outside'):
            tools['read_file'].run({'path': path})


def test_read_file_cap(ctx):
    ctx.config.files_path.mkdir()
    (ctx.config.files_path / 'large').write_text('x' * 200_000)
    result = build_tools(ctx)['read_file'].run({'path': 'large'})
    assert len(result) <= 100 * 1024 + len('\n[truncated]')
    assert '[truncated]' in result


def test_web_search_formats(ctx, monkeypatch):
    monkeypatch.setattr(web, 'search', lambda q: [{'title': q, 'url': 'https://example.com', 'snippet': 'hi'}])
    assert json.loads(build_tools(ctx)['web_search'].run({'query': 'test'}))[0]['title'] == 'test'


def test_web_fetch_strips_html(ctx, monkeypatch):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text='<h1>Hello</h1><script>secret</script><style>bad</style><p>world</p>'))
    monkeypatch.setattr(web, '_transport', transport)
    monkeypatch.setattr(web, '_resolve', lambda host: ['93.184.215.14'])
    fetch = build_tools(ctx)['web_fetch'].run
    result = fetch({'url': 'https://example.com'})
    assert 'Hello' in result and 'world' in result and 'secret' not in result and 'bad' not in result
    with pytest.raises(ToolError):
        fetch({'url': 'file:///etc/passwd'})


def test_reminder_add_naive_is_local_and_past_refused(ctx):
    tools = build_tools(ctx)
    assert 'Reminder #1' in tools['reminder_add'].run({'when': '2026-11-12T11:00', 'text': 'hi'})
    assert ctx.store.list_reminders()[0]['when'] == '2026-11-12T11:00:00+01:00'
    assert json.loads(tools['reminder_list'].run({}))[0]['text'] == 'hi'
    with pytest.raises(ToolError, match='future'):
        tools['reminder_add'].run({'when': '2026-11-12T09:00', 'text': 'past'})


def test_memory_tools(ctx):
    tools = build_tools(ctx)
    tools['memory_save'].run({'title': 'Dog', 'body': 'Fido', 'tags': ['pet']})
    results = json.loads(tools['memory_search'].run({'query': 'Dog'}))
    assert results[0]['title'] == 'Dog' and results[0]['body'] == 'Fido'


def test_web_fetch_refuses_private_and_bad_redirects(ctx, monkeypatch):
    from kova.tools import ToolError

    addresses = {'example.com': ['93.184.215.14'], 'router.lan': ['192.168.1.1'], 'localhost': ['127.0.0.1']}
    monkeypatch.setattr(web, '_resolve', lambda host: addresses[host])
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={'location': 'http://router.lan/admin'})

    monkeypatch.setattr(web, '_transport', httpx.MockTransport(handler))
    fetch = build_tools(ctx)['web_fetch'].run
    for url in ['http://localhost:7700/health', 'http://router.lan/', 'https://example.com/']:
        with pytest.raises(ToolError):
            fetch({'url': url})
    assert seen == ['https://93.184.215.14/']  # the redirect to the LAN was never followed
