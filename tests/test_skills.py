"""Behavioral checks for progressive disclosure and durable procedural memory."""
import json
from datetime import datetime, timezone

import pytest

from kova.agent import Agent
from kova.config import load_config
from kova.memory import Memory
from kova.skills import Skills, MAX_BODY
from kova.store import Store
from kova.tools import ToolContext, build_tools
from tests.fake_model import FakeModel, text, tool


def test_discovery_does_not_decode_or_load_body(tmp_path):
    skills = Skills(tmp_path)
    path = tmp_path / 'release.md'
    path.write_bytes(b'---\ndescription: Use for releases\n---\n' + b'\xff' * (MAX_BODY + 100))
    assert skills.list() == [{'slug': 'release', 'description': 'Use for releases'}]
    assert 'Use for releases' in skills.prompt()
    with pytest.raises(ValueError):
        skills.read('release')


def test_patch_is_exact_and_durable(tmp_path):
    skills = Skills(tmp_path)
    skills.add('release', 'Use for releases', 'Run checks. Then tag.')
    with pytest.raises(ValueError, match='already exists'):
        skills.add('release', 'Use elsewhere', 'Overwrite')
    with pytest.raises(ValueError, match='exactly once'):
        skills.patch('release', 'unknown', 'new')
    skills.patch('release', 'Then tag.', 'Then review.', description='Before releasing')
    assert Skills(tmp_path).read('release') == {
        'slug': 'release', 'description': 'Before releasing', 'body': 'Run checks. Then review.'}
    with pytest.raises(ValueError):
        skills.patch('release', 'Run checks.', 'x' * (MAX_BODY + 1))
    assert skills.read('release')['body'] == 'Run checks. Then review.'


@pytest.mark.parametrize('slug', ['../escape', '/tmp/escape', 'a/b', '.hidden', 'UPPER', 'a' * 61])
def test_paths_are_confined(tmp_path, slug):
    with pytest.raises(ValueError):
        Skills(tmp_path).add(slug, 'Use for testing', 'Test')


def test_symlinks_and_malformed_frontmatter_are_skipped(tmp_path):
    root = tmp_path / 'skills'
    root.mkdir()
    outside = tmp_path / 'outside.md'
    outside.write_text('---\ndescription: Outside\n---\nPrivate')
    (root / 'linked.md').symlink_to(outside)
    (root / 'malformed.md').write_text('---\ndescription: [unclosed\n---\nBody')
    skills = Skills(root)
    assert skills.list() == []
    with pytest.raises(ValueError, match='symlink'):
        skills.read('linked')
    with pytest.raises(ValueError, match='symlink'):
        skills.add('linked', 'Changed', 'Changed')
    assert 'Private' in outside.read_text()


async def test_model_creates_then_reuses_skill_without_eager_body(tmp_path):
    cfg = load_config(tmp_path)
    cfg.files_path = tmp_path
    store = Store(tmp_path / 'kova.db')
    memory = Memory(cfg.memory_path)
    now = lambda: datetime.now(timezone.utc)
    tools = build_tools(ToolContext(cfg, store, memory, now))
    fake = FakeModel([
        tool('skill_add', {'slug': 'release', 'description': 'Use before releasing', 'body': 'Unique procedure: run the release smoke check.'}),
        text('Learned it.'),
        tool('skill_read', {'slug': 'release'}),
        text('Using the release procedure.'),
    ])
    agent = Agent(cfg, store, memory, tools, transport=fake.transport)
    first = store.create_chat(permission='full', cwd=str(tmp_path))
    assert not [e async for e in agent.run_turn(first['id'], 'Learn this procedure') if e['type'] == 'error']
    # A fresh chat and registry must discover the persisted metadata.
    second = store.create_chat(cwd=str(tmp_path))
    agent = Agent(cfg, store, memory, build_tools(ToolContext(cfg, store, memory, now)), transport=fake.transport)
    events = [e async for e in agent.run_turn(second['id'], 'Prepare a release')]
    initial = json.dumps(fake.requests[2])
    assert 'Use before releasing' in initial
    assert 'Unique procedure' not in initial
    assert 'Unique procedure' in json.dumps(fake.requests[3])
    assert any(e['type'] == 'tool_result' and e['name'] == 'skill_read' and e['ok'] for e in events)
