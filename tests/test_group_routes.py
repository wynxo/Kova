"""Group settings, membership and live updates through the authenticated API."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from kova.config import ensure_token, load_config, save_config
from kova.server import create_app


@pytest.fixture
def group_api(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text('[groups]\npreset = "panel"\n', encoding="utf-8")
    cfg = load_config(home)
    cfg.files_path = tmp_path / "files"
    cfg.memory_path = tmp_path / "memory"
    cfg.active_model().model = "test-model"
    app = create_app(cfg, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(home)}"
        yield client, app, cfg


def create(client, **fields):
    response = client.post("/chats", json={"members": ["kova"], **fields})
    assert response.status_code == 200, response.text
    return response.json()


def test_group_uses_configured_preset_and_returns_ordered_members(group_api):
    client, _, cfg = group_api
    save_config(cfg)
    assert load_config(cfg.home).groups_preset == "panel"
    assert client.post("/bots", json={"id": "coder", "name": "Coder"}).status_code == 200
    chat = create(client, members=["coder", "kova"])
    assert chat["kind"] == "group" and chat["bot_id"] is None
    assert chat["preset"] == "panel"
    assert chat["group"]["answer"]["mode"] == "everyone"
    assert chat["group"]["see_earlier_replies"] is False
    assert [(m["id"], m["name"], m["position"], m["muted"], m["settings"])
            for m in chat["members"]] == [("coder", "Coder", 0, False, {}),
                                          ("kova", "Kova", 1, False, {})]
    assert "avatar" in chat["members"][0]
    for read in (client.get("/chats").json()[0], client.get(f"/chats/{chat['id']}").json()):
        assert {key: read[key] for key in ("members", "group", "group_settings", "preset")} == {
            key: chat[key] for key in ("members", "group", "group_settings", "preset")}


@pytest.mark.parametrize("fields, status", [
    ({"members": []}, 422),
    ({"members": ["kova"] * 9}, 422),
    ({"members": ["kova", "kova"]}, 422),
    ({"members": ["missing"]}, 404),
    ({"members": ["kova"], "bot_id": "coder"}, 422),
    ({"members": ["kova"], "ephemeral": True}, 422),
    ({"preset": "panel"}, 422),
    ({"members": ["kova"], "preset": "imaginary"}, 422),
    ({"members": ["kova"], "group": {"limits": {"max_bot_turns": 17}}}, 422),
])
def test_invalid_group_create_leaves_no_chat(group_api, fields, status):
    client, _, _ = group_api
    assert client.post("/chats", json=fields).status_code == status
    assert client.get("/chats").json() == []


def test_group_patch_merges_and_resets_nested_settings(group_api):
    client, app, _ = group_api
    chat = create(client, preset="ask-one", group={"answer": {"count": 4}})
    path = f"/chats/{chat['id']}"
    response = client.patch(path, json={"group": {"answer": {"mode": "count"}}})
    assert response.status_code == 200
    assert response.json()["preset"] == "custom"
    assert response.json()["group"]["answer"]["count"] == 4
    assert app.state.store.get_chat(chat["id"])["group_settings"] == {
        "answer": {"mode": "count", "count": 4}}
    reset = client.patch(path, json={"group": {"answer": {"count": None, "mode": None}}})
    assert reset.json()["group"]["answer"]["count"] == 2
    assert reset.json()["group_settings"] == {}
    assert reset.json()["preset"] == "ask-one"


def test_switching_preset_resets_old_values_but_preserves_safety_settings(group_api):
    client, _, _ = group_api
    chat = create(client, preset="debate", group={"approvals": "deny",
                  "folder": "own", "limits": {"max_bot_turns": 4}})
    response = client.patch(f"/chats/{chat['id']}", json={"preset": "panel"})
    assert response.status_code == 200
    group = response.json()["group"]
    assert group["replies"]["mode"] == "off"
    assert group["replies"]["max_hops"] == 3
    assert group["instructions"] == ""
    assert group["see_earlier_replies"] is False
    assert (group["approvals"], group["folder"]) == ("deny", "own")
    assert group["limits"]["max_bot_turns"] == 4
    # Approvals, folders and limits never hide the preset the group runs.
    assert response.json()["preset"] == "panel"


def test_safety_settings_keep_the_preset_label(group_api):
    client, _, _ = group_api
    chat = create(client, preset="team")
    path = f"/chats/{chat['id']}"
    edit = client.patch(path, json={"group": {"approvals": "deny", "limits": {"max_seconds": 120}}})
    assert edit.status_code == 200
    assert edit.json()["preset"] == "team"
    assert client.get(path).json()["preset"] == "team"


@pytest.mark.parametrize("group", [{"retry": "stale"}, {"memory": "group"}])
def test_settings_that_do_not_work_yet_are_refused(group_api, group):
    client, _, _ = group_api
    assert client.post("/chats", json={"members": ["kova"], "group": group}).status_code == 422


def test_older_stored_keys_are_ignored_not_fatal(group_api):
    client, app, _ = group_api
    chat = create(client, preset="panel")
    store = app.state.store
    store._conn.execute("UPDATE chats SET group_settings = ? WHERE id = ?",
                        ('{"answer": {"mode": "everyone"}, "replies": {"mode": "off"}, '
                         '"see_earlier_replies": false, "retry": "rerun", "memory": "group"}',
                         chat["id"]))
    store._conn.commit()
    read = client.get(f"/chats/{chat['id']}").json()
    assert read["preset"] == "panel"
    assert "retry" not in read["group"] and "memory" not in read["group"]
    edit = client.patch(f"/chats/{chat['id']}", json={"group": {"approvals": "deny"}})
    assert edit.status_code == 200
    assert "memory" not in edit.json()["group_settings"]


def test_member_routes_reorder_patch_overrides_and_keep_history(group_api):
    client, app, _ = group_api
    assert client.post("/bots", json={"id": "coder", "name": "Coder"}).status_code == 200
    chat = create(client)
    chat_id = chat["id"]
    path = f"/chats/{chat_id}/members"
    assert client.post(path, json={"bot_id": "coder"}).status_code == 200
    assert client.post(path, json={"bot_id": "coder"}).status_code == 409
    edit = client.patch(f"{path}/coder", json={"position": 0, "muted": True,
                        "settings": {"max_rounds": 2, "approvals": "deny"}})
    assert edit.status_code == 200
    assert edit.json()["settings"] == {"max_rounds": 2, "approvals": "deny"}
    assert edit.json()["muted"] is True
    get = client.get(f"/chats/{chat_id}").json()
    assert [(m["id"], m["position"]) for m in get["members"]] == [("coder", 0), ("kova", 1)]
    reset = client.patch(f"{path}/coder", json={"settings": {"max_rounds": None}})
    assert reset.json()["settings"] == {"approvals": "deny"}
    inherited = client.patch(f"{path}/coder", json={"settings": {"approvals": "same"}})
    assert inherited.status_code == 200
    assert inherited.json()["settings"] == {}
    app.state.store.add_message(chat_id, "assistant", "Reply", author="coder", author_name="Coder")
    assert client.delete(f"{path}/coder").status_code == 200
    assert client.delete(f"{path}/coder").status_code == 404
    get = client.get(f"/chats/{chat_id}").json()
    assert [(m["id"], m["position"]) for m in get["members"]] == [("kova", 0)]
    assert get["messages"][-1]["author"] == "coder"


def test_deleting_a_bot_removes_it_from_every_group(group_api):
    client, app, _ = group_api
    assert client.post("/bots", json={"id": "coder", "name": "Coder"}).status_code == 200
    first = create(client, members=["coder", "kova"])
    second = create(client, members=["coder"])
    app.state.store.add_message(first["id"], "assistant", "Kept", author="coder",
                                author_name="Coder")
    assert client.delete("/bots/coder").status_code == 200
    one = client.get(f"/chats/{first['id']}").json()
    assert [(m["id"], m["position"]) for m in one["members"]] == [("kova", 0)]
    assert one["messages"][0]["author_name"] == "Coder"
    assert client.get(f"/chats/{second['id']}").json()["members"] == []


def test_live_group_patch_does_not_change_running_snapshot(group_api):
    client, app, _ = group_api
    chat = create(client, preset="ask-one")
    chat_id = chat["id"]
    seen = []

    async def scenario():
        # The runner boundary reads the chat once per user message. Exercise the
        # real HTTP stream and patch while this turn is deliberately suspended.
        started = asyncio.Event()
        finish = asyncio.Event()

        async def run_turn(chat_id, text, **kwargs):
            snapshot = app.state.store.get_chat(chat_id)["group_settings"]
            started.set()
            if text == "first":
                await finish.wait()
            seen.append(snapshot)
            yield {"type": "done"}

        app.state.agent.run_turn = run_turn
        bus = app.state.bus.subscribe()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://testserver",
                                     headers=dict(client.headers)) as api:
            path = f"/chats/{chat_id}"
            first = asyncio.create_task(api.post(f"{path}/messages", json={"text": "first"}))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                patch = await api.patch(path, json={"group": {"answer": {"mode": "everyone"}}})
                assert patch.status_code == 200
                blocked = await api.patch(path, json={"mode": "chat"})
                assert blocked.status_code == 409
            finally:
                finish.set()
            response = await first
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            changed = {"type": "group_settings_changed", "chat_id": chat_id,
                       "changed": {"answer": {"mode": "everyone"}}, "by": "user"}
            assert changed in events
            assert changed in [bus.get_nowait() for _ in range(bus.qsize())]
            await api.post(f"{path}/messages", json={"text": "second"})

    client.portal.call(scenario)
    assert seen == [{}, {"answer": {"mode": "everyone"}}]


def test_every_published_member_setting_can_be_saved(group_api):
    """#406 #408: the member route accepts what GET /group-settings advertises."""
    client, _, _ = group_api
    assert client.post("/bots", json={"id": "coder", "name": "Coder"}).status_code == 200
    chat = create(client, members=["coder", "kova"])
    path = f"/chats/{chat['id']}/members/coder"
    schema = client.get("/group-settings").json()["member_settings"]
    keys = {setting["key"] for setting in schema}
    assert {"answers", "muted"} <= keys
    for value in next(s for s in schema if s["key"] == "answers")["values"]:
        response = client.patch(path, json={"settings": {"answers": value}})
        assert response.status_code == 200, (value, response.text)
    always = client.patch(path, json={"settings": {"answers": "always"}}).json()
    assert always["settings"]["answers"] == "always"
    same = client.patch(path, json={"settings": {"answers": "same"}}).json()
    assert "answers" not in same["settings"]
    assert client.patch(path, json={"settings": {"answers": True}}).status_code == 422
    muted = client.patch(path, json={"settings": {"muted": True}})
    assert muted.status_code == 200, muted.text
    assert muted.json()["muted"] is True and "muted" not in muted.json()["settings"]
    assert client.patch(path, json={"settings": {"muted": None}}).json()["muted"] is False
    assert client.patch(path, json={"settings": {"max_rounds": 99}}).status_code == 422
    assert client.patch(path, json={"settings": {"nonsense": 1}}).status_code == 422


@pytest.mark.parametrize(("cost", "status"), [(0, 200), (0.5, 200), (-1, 422)])
def test_group_cost_ceiling_accepts_zero(group_api, cost, status):
    """#407: the schema's minimum for limits.max_cost_usd is 0."""
    client, _, _ = group_api
    chat = create(client)
    response = client.patch(f"/chats/{chat['id']}",
                            json={"group": {"limits": {"max_cost_usd": cost}}})
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["group"]["limits"]["max_cost_usd"] == cost
