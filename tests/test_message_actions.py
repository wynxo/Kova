"""Retry and edit show one turn at a time, keeping earlier attempts as alternatives."""

from tests.test_server import server  # noqa: F401 - fixture


def _messages(client, headers, chat):
    return client.get(f"/chats/{chat}", headers=headers).json()["messages"]


def _turn(client, headers, chat, text, **extra):
    response = client.post(f"/chats/{chat}/messages", headers=headers, json={"text": text, **extra})
    assert response.status_code == 200
    assert "event: done" in response.text
    return response


def test_retry_replaces_the_reply_without_duplicating_the_message(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    _turn(client, headers, chat, "hello")
    assert [m["role"] for m in _messages(client, headers, chat)] == ["user", "assistant"]

    response = client.post(f"/chats/{chat}/retry", headers=headers)
    assert response.status_code == 200
    assert "event: done" in response.text

    messages = _messages(client, headers, chat)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "hello"
    assert messages[1]["content"] == "Second"
    # The model saw the words once, not the old round plus a copy of them.
    sent = [m for m in fake.requests[-1]["messages"] if m["role"] == "user"]
    assert [m["content"] for m in sent] == ["hello"]


def test_retry_keeps_the_replaced_reply_as_an_alternative(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    _turn(client, headers, chat, "hello")

    response = client.post(f"/chats/{chat}/retry", headers=headers)
    assert response.status_code == 200

    saved = client.get(f"/chats/{chat}", headers=headers).json()
    assert [m["content"] for m in saved["messages"]] == ["hello", "Second"]
    assert saved["alternatives"]["index"] == 2
    assert saved["alternatives"]["count"] == 2
    # Nothing was deleted: the first attempt is still on disk, inactive.
    stored = client.app.state.store.get_messages(chat, include_inactive=True)
    assert [(m["role"], m["content"], m["active"]) for m in stored] == [
        ("user", "hello", False),
        ("assistant", "Hello there!", False),
        ("user", "hello", True),
        ("assistant", "Second", True),
    ]

    flipped = client.post(f"/chats/{chat}/alternatives", headers=headers,
                          json={"turn_id": saved["alternatives"]["turn_id"], "index": 1})
    assert flipped.status_code == 200
    assert [m["content"] for m in flipped.json()["messages"]] == ["hello", "Hello there!"]
    assert flipped.json()["alternatives"]["index"] == 1
    assert [m["content"] for m in _messages(client, headers, chat)] == ["hello", "Hello there!"]

    back = client.post(f"/chats/{chat}/alternatives", headers=headers,
                       json={"turn_id": saved["alternatives"]["turn_id"], "index": 2})
    assert back.status_code == 200
    assert [m["content"] for m in back.json()["messages"]] == ["hello", "Second"]


def test_edit_keeps_the_old_words_and_reply_as_an_alternative(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    _turn(client, headers, chat, "first words")

    response = client.post(f"/chats/{chat}/edit", headers=headers, json={"text": "rewritten"})
    assert response.status_code == 200

    saved = client.get(f"/chats/{chat}", headers=headers).json()
    assert [m["content"] for m in saved["messages"]] == ["rewritten", "Second"]
    assert saved["alternatives"]["count"] == 2
    stored = client.app.state.store.get_messages(chat, include_inactive=True)
    assert [(m["role"], m["content"], m["active"]) for m in stored] == [
        ("user", "first words", False),
        ("assistant", "Hello there!", False),
        ("user", "rewritten", True),
        ("assistant", "Second", True),
    ]

    first = client.post(f"/chats/{chat}/alternatives", headers=headers,
                        json={"turn_id": saved["alternatives"]["turn_id"], "index": 1})
    assert first.status_code == 200
    assert [m["content"] for m in first.json()["messages"]] == ["first words", "Hello there!"]
    assert first.json()["messages"][0]["role"] == "user"


def test_an_older_turn_cannot_be_flipped_after_a_new_message(server):
    from tests.fake_model import text

    client, headers, cfg, fake = server
    fake.scripts.append(text("Third"))
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    _turn(client, headers, chat, "hello")
    assert client.post(f"/chats/{chat}/retry", headers=headers).status_code == 200
    old_turn = client.get(f"/chats/{chat}", headers=headers).json()["alternatives"]
    _turn(client, headers, chat, "carry on")

    stale = client.post(f"/chats/{chat}/alternatives", headers=headers,
                        json={"turn_id": old_turn["turn_id"], "index": 1})
    assert stale.status_code == 409
    # The archived attempt is still stored; a stale control only changes nothing.
    stored = client.app.state.store.get_messages(chat, include_inactive=True)
    assert [m["content"] for m in stored if m["role"] == "assistant"] == [
        "Hello there!", "Second", "Third",
    ]


def test_edit_rewrites_the_last_message_and_runs_it_again(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    _turn(client, headers, chat, "first words")

    response = client.post(f"/chats/{chat}/edit", headers=headers, json={"text": "rewritten"})
    assert response.status_code == 200
    assert "event: done" in response.text

    messages = _messages(client, headers, chat)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "rewritten"
    assert messages[1]["content"] == "Second"


def test_edit_keeps_the_files_on_the_message(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    row = client.post(f"/chats/{chat}/attachments", headers=headers,
                      files={"file": ("notes.txt", b"FILE BODY")}).json()
    _turn(client, headers, chat, "read this", attachments=[row["id"]])

    response = client.post(f"/chats/{chat}/edit", headers=headers, json={"text": "edited words"})
    assert response.status_code == 200

    messages = _messages(client, headers, chat)
    assert messages[0]["content"] == "edited words"
    assert messages[0]["attachments"] == [{"id": row["id"], "name": "notes.txt"}]
    # The edited turn still carries the extracted text, not just the file name.
    sent = [m for m in fake.requests[-1]["messages"] if m["role"] == "user"]
    assert "FILE BODY" in sent[-1]["content"]


def test_retry_needs_a_message_and_a_turn_that_is_not_compacted(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    assert client.post(f"/chats/{chat}/retry", headers=headers).status_code == 409
    assert client.post(f"/chats/{chat}/edit", headers=headers,
                       json={"text": "nothing to edit"}).status_code == 409

    _turn(client, headers, chat, "hello")
    store = client.app.state.store
    message = store.last_user_message(chat)
    assert message["content"] == "hello"
    assert store.save_compaction(chat, 0, message["id"], "earlier context") is True
    assert client.post(f"/chats/{chat}/retry", headers=headers).status_code == 409
    assert client.post(f"/chats/{chat}/edit", headers=headers,
                       json={"text": "too late"}).status_code == 409


def test_retry_with_another_model_validates_before_replacing_the_reply(server):
    from dataclasses import replace

    client, headers, cfg, fake = server
    cfg.models.append(replace(cfg.models[0], name="alternate", model="other-model"))
    chat = client.post("/chats", headers=headers, json={"model": cfg.models[0].name}).json()["id"]
    _turn(client, headers, chat, "hello")
    before = _messages(client, headers, chat)
    invalid = client.post(f"/chats/{chat}/retry", headers=headers, json={"model": "missing"})
    assert invalid.status_code == 404
    assert _messages(client, headers, chat) == before
    stale = client.post(f"/chats/{chat}/retry", headers=headers,
                        json={"expected_reply": before[0]["id"]})
    assert stale.status_code == 409
    assert _messages(client, headers, chat) == before

    response = client.post(f"/chats/{chat}/retry", headers=headers,
                           json={"model": "alternate", "expected_reply": before[-1]["id"]})
    assert response.status_code == 200
    assert "event: done" in response.text
    assert fake.requests[-1]["model"] == "other-model"
    assert client.get(f"/chats/{chat}", headers=headers).json()["model"] == cfg.models[0].name
    assert len(_messages(client, headers, chat)) == 2


def test_branch_from_reply_copies_only_its_history(server):
    client, headers, cfg, fake = server
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    store = client.app.state.store
    store.add_message(chat, "user", "first")
    # Imported history may explicitly store an empty tool-call list.
    store.add_message(chat, "assistant", "first answer", tool_calls=[])
    _turn(client, headers, chat, "second")
    messages = _messages(client, headers, chat)
    assert all(msg.get("created") for msg in messages)
    first_reply = messages[1]["id"]
    created = client.post(f"/chats/{chat}/fork", headers=headers, params={"through": first_reply})
    assert created.status_code == 200
    fork = created.json()
    assert fork["id"] != chat
    assert [msg["content"] for msg in fork["messages"]] == [msg["content"] for msg in messages[:2]]
    assert _messages(client, headers, chat) == messages
    for invalid in (messages[0]["id"], messages[-1]["id"] + 1):
        assert client.post(f"/chats/{chat}/fork", headers=headers,
                           params={"through": invalid}).status_code == 404


def test_reply_usage_keeps_the_provider_used_at_generation(server):
    client, headers, cfg, _ = server
    from kova.config import ProviderEntry

    cfg.providers.append(ProviderEntry(id="demo", name="Demo provider", kind="custom",
                                       base_url=cfg.models[0].base_url))
    cfg.models[0].provider = "demo"
    chat = client.post("/chats", headers=headers, json={}).json()["id"]
    reply = _turn(client, headers, chat, "hello")
    assert '"provider": "Demo provider"' in reply.text
    cfg.providers[0].name = "Renamed provider"
    assert _messages(client, headers, chat)[-1]["usage"]["provider"] == "Demo provider"
