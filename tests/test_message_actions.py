"""Retry and edit rewrite one turn in place; neither appends a duplicate."""

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
