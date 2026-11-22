"""Cost and speed meter (#75): usage parsing, pricing, storage, totals and surfaces."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from kova import cli, usage
from kova.agent import Agent
from kova.config import ModelEntry, ProviderEntry, ensure_token, load_config, save_config
from kova.memory import Memory
from kova.models import ModelClient
from kova.server import create_app
from kova.store import Store
from kova.tools import ToolContext, build_tools
from kova.usage import Price, TurnMeter, normalize_usage, price_for, summarize
from tests.fake_model import FakeModel, text, tool


def entry_for(kind: str, model: str, base_url: str = "https://api.example.test/v1", name: str = "m"):
    provider = ProviderEntry("p", "Provider", base_url, kind=kind)
    return ModelEntry(name, base_url, model, provider="p", _provider=provider)


# -- parsing ---------------------------------------------------------------

def test_normalize_usage_reads_each_wire_format():
    assert normalize_usage({"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}) == {
        "prompt_tokens": 12, "completion_tokens": 5}
    assert normalize_usage({"input_tokens": 7, "output_tokens": 3}) == {
        "prompt_tokens": 7, "completion_tokens": 3}
    # Anthropic reports cached prompt tokens next to input_tokens.
    assert normalize_usage({"input_tokens": 10, "cache_read_input_tokens": 90,
                            "cache_creation_input_tokens": 5, "output_tokens": 1}) == {
        "prompt_tokens": 105, "completion_tokens": 1}
    for junk in (None, "12", [], {}, {"prompt_tokens": "x"}, {"prompt_tokens": -1}, {"prompt_tokens": True}):
        assert normalize_usage(junk) is None


async def test_openai_stream_asks_for_usage_and_reports_it():
    fake = FakeModel([text("Hello")], usage=[{"prompt_tokens": 42, "completion_tokens": 2}])
    client = ModelClient(ModelEntry("m", "https://api.example.test/v1", "model"), transport=fake.transport)
    events = [event async for event in client.stream([{"role": "user", "content": "hi"}])]
    assert fake.requests[0]["stream_options"] == {"include_usage": True}
    assert [event.usage for event in events if event.type == "usage"] == [
        {"prompt_tokens": 42, "completion_tokens": 2}]
    assert "".join(event.text for event in events if event.type == "text") == "Hello"
    assert events[-1].type == "done"


async def test_a_server_that_rejects_stream_options_is_asked_again_without_it():
    payloads = []

    def handle(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if "stream_options" in payload:
            return httpx.Response(400, json={"error": "Unrecognized request argument: stream_options"})
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')

    client = ModelClient(ModelEntry("m", "https://old.example.test/v1", "model"),
                         transport=httpx.MockTransport(handle))
    assert client.include_usage
    events = [event async for event in client.stream([{"role": "user", "content": "hi"}])]
    assert [event.text for event in events if event.type == "text"] == ["ok"]
    assert ["stream_options" in payload for payload in payloads] == [True, False]
    # Kova remembers, so later rounds and turns do not pay for a failed request.
    [event async for event in client.stream([{"role": "user", "content": "again"}])]
    assert "stream_options" not in payloads[-1] and len(payloads) == 3
    later = ModelClient(ModelEntry("m", "https://old.example.test/v1", "model"),
                        transport=httpx.MockTransport(handle))
    [event async for event in later.stream([{"role": "user", "content": "next turn"}])]
    assert "stream_options" not in payloads[-1] and len(payloads) == 4
    ModelClient._rejects_usage_option.clear()


@pytest.mark.parametrize("adapter", ["anthropic", "responses"])
async def test_native_adapters_report_usage(adapter):
    provider = ProviderEntry("p", "Provider", "https://api.example.test/v1", adapter=adapter, api_key="k")
    entry = ModelEntry("m", provider.base_url, "model", provider="p", _provider=provider)
    if adapter == "anthropic":
        frames = [
            {"type": "message_start", "message": {"usage": {"input_tokens": 20, "cache_read_input_tokens": 80,
                                                            "output_tokens": 1}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 9}},
            {"type": "message_stop"},
        ]
    else:
        frames = [
            {"type": "response.output_text.delta", "delta": "hi"},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 100, "output_tokens": 9}}},
        ]

    def handle(request):
        return httpx.Response(200, text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames))

    events = [event async for event in ModelClient(entry, transport=httpx.MockTransport(handle)).stream(
        [{"role": "user", "content": "hi"}])]
    assert [event.usage for event in events if event.type == "usage"] == [
        {"prompt_tokens": 100, "completion_tokens": 9}]
    assert events[-1].type == "done"


# -- pricing ---------------------------------------------------------------

def test_known_cloud_models_have_a_price_and_dated_snapshots_share_it():
    assert price_for(entry_for("openai", "gpt-4o-mini")) == Price(0.15, 0.60, "builtin")
    assert price_for(entry_for("anthropic", "claude-sonnet-4-5-20250929")) == Price(3.0, 15.0, "builtin")
    assert price_for(entry_for("openai", "gpt-4o-2024-08-06")) == Price(2.5, 10.0, "builtin")
    # A snapshot with a different list price has its own row.
    assert price_for(entry_for("openai", "gpt-4o-2024-05-13")) == Price(5.0, 15.0, "builtin")


def test_unknown_models_are_never_given_a_made_up_price():
    # A new model whose id merely starts with a known one is not guessed at.
    assert price_for(entry_for("anthropic", "claude-opus-4-9")) is None
    assert price_for(entry_for("openai", "gpt-4o-mini-tts")) is None
    assert price_for(entry_for("openrouter", "openai/gpt-4o")) is None
    assert price_for(entry_for("custom", "gpt-4o")) is None


def test_local_models_cost_nothing():
    assert price_for(entry_for("custom", "qwen3", "http://127.0.0.1:8080/v1")).local
    assert price_for(entry_for("local", "qwen3", "http://192.0.2.10:8080/v1")) == Price(0, 0, "local")
    managed = ModelEntry("box", "http://127.0.0.1:9/v1", "qwen3", provider="managed-qwen3")
    assert price_for(managed).local


def test_config_overrides_beat_the_table_by_name_kind_or_model():
    cloud = entry_for("openai", "gpt-4o", name="work")
    assert price_for(cloud, {"work": (1.0, 2.0)}) == Price(1.0, 2.0, "config")
    assert price_for(cloud, {"openai/gpt-4o": (3.0, 4.0)}) == Price(3.0, 4.0, "config")
    assert price_for(cloud, {"gpt-4o": (5.0, 6.0)}) == Price(5.0, 6.0, "config")
    # An override can also price a LAN server that is not free for this user.
    lab = entry_for("local", "big", "http://192.0.2.10:8080/v1", name="lab")
    assert price_for(lab, {"lab": (0.5, 0.5)}).source == "config"
    assert Price(2.5, 10.0, "builtin").cost(1_000_000, 100_000) == pytest.approx(3.5)


def test_pricing_and_budget_load_from_config_and_survive_a_save(tmp_path):
    cfg = load_config(tmp_path)
    path = tmp_path / "config.toml"
    path.write_text(path.read_text() + '\n[pricing]\n"gpt-4o" = { input = 2, output = 8 } # mine\n'
                    '"broken" = { input = "x" }\n"negative" = { input = -1, output = 1 }\n'
                    '\n[usage]\nmonthly_budget_usd = 12.5\n')
    cfg = load_config(tmp_path)
    assert cfg.pricing == {"gpt-4o": (2.0, 8.0)}
    assert cfg.usage_monthly_budget_usd == 12.5
    cfg.usage_monthly_budget_usd = 0
    save_config(cfg)
    saved = path.read_text()
    assert '"gpt-4o" = { input = 2, output = 8 } # mine' in saved
    assert "monthly_budget_usd" not in saved
    assert load_config(tmp_path).pricing == {"gpt-4o": (2.0, 8.0)}


def test_config_set_accepts_a_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    from typer.testing import CliRunner
    result = CliRunner().invoke(cli.app, ["config", "set", "usage.monthly_budget_usd", "20"])
    assert result.exit_code == 0, result.output
    assert load_config(tmp_path).usage_monthly_budget_usd == 20.0
    result = CliRunner().invoke(cli.app, ["config", "set", "usage.monthly_budget_usd", "--", "-1"])
    assert result.exit_code == 1, result.output
    assert load_config(tmp_path).usage_monthly_budget_usd == 20.0


# -- the meter ----------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_meter_sums_rounds_and_excludes_tool_time_from_speed():
    clock = Clock()
    meter = TurnMeter(clock)
    meter.start_round()
    clock.now += 0.5                       # waiting for the first token
    meter.output("abc")
    clock.now += 1.0                       # generating
    meter.usage({"prompt_tokens": 100, "completion_tokens": 20})
    meter.end_round([])
    clock.now += 30.0                      # a slow tool run: not generation
    meter.start_round()
    clock.now += 0.2
    meter.output("x")
    clock.now += 1.0
    meter.usage({"prompt_tokens": 150, "completion_tokens": 20})
    meter.end_round([])
    summary = meter.summary(entry_for("openai", "gpt-4o"))
    assert summary["prompt_tokens"] == 250 and summary["completion_tokens"] == 40
    assert summary["estimated"] is False
    assert summary["ms_first_token"] == 500
    assert summary["ms_generation"] == 2000
    assert summary["ms_total"] == 32700
    assert summary["tokens_per_second"] == 20.0
    assert summary["cost_usd"] == pytest.approx((250 * 2.5 + 40 * 10) / 1e6)
    assert summary["local"] is False and summary["model"] == "m"


def test_meter_estimates_a_round_without_usage():
    meter = TurnMeter(Clock())
    meter.start_round()
    meter.output("a" * 40)
    meter.end_round([{"role": "user", "content": "b" * 400}])
    summary = meter.summary(entry_for("custom", "mystery"))
    assert summary["estimated"] is True
    assert summary["completion_tokens"] == 10
    assert 100 <= summary["prompt_tokens"] <= 120
    assert summary["cost_usd"] is None and summary["tokens_per_second"] is None


def test_estimates_count_an_image_as_an_image_not_as_its_base64():
    picture = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 400_000}}
    estimate = usage.estimate_tokens([{"role": "user", "content": [{"type": "text", "text": "what is this"}, picture]}])
    assert usage.IMAGE_TOKENS < estimate < usage.IMAGE_TOKENS + 50


def test_footer_reads_like_the_issue():
    line = usage.footer({"prompt_tokens": 1234, "completion_tokens": 340, "cost_usd": 0.0041,
                         "tokens_per_second": 38.0})
    assert line == "1.2k in · 340 out · ~$0.0041 · 38 tok/s"
    assert usage.footer({"prompt_tokens": 5, "completion_tokens": 5, "local": True,
                         "estimated": True}) == "~5 in · ~5 out · $0.00 local"


# -- the agent and store ---------------------------------------------------------

@pytest.fixture
def setup(tmp_path):
    cfg = load_config(tmp_path)
    store = Store(tmp_path / "db.sqlite")
    memory = Memory(cfg.memory_path)
    tools = build_tools(ToolContext(cfg, store, memory, lambda: datetime.now().astimezone()))
    return cfg, store, memory, tools


async def turn(setup, scripts, usage_blocks=None, chat_id=None, agent=None):
    cfg, store, memory, tools = setup
    chat_id = chat_id or store.create_chat()["id"]
    fake = FakeModel(scripts, usage=usage_blocks)
    agent = agent or Agent(cfg, store, memory, tools)
    agent.transport = fake.transport
    events = [event async for event in agent.run_turn(chat_id, "hello")]
    return events, chat_id


async def test_a_tool_turn_sums_every_round_and_stores_it_on_the_answer(setup):
    cfg, store, _, _ = setup
    events, chat_id = await turn(setup, [tool("system_info", {}), text("All good")],
                                 [{"prompt_tokens": 1000, "completion_tokens": 30},
                                  {"prompt_tokens": 1100, "completion_tokens": 12}])
    done = events[-1]
    assert done["type"] == "done"
    assert done["usage"]["prompt_tokens"] == 2100 and done["usage"]["completion_tokens"] == 42
    assert done["usage"]["estimated"] is False
    # The test model is a custom server Kova has no price for: tokens only.
    assert done["usage"]["local"] is False and done["usage"]["cost_usd"] is None
    messages = store.get_messages(chat_id, include_usage=True)
    answer = messages[-1]
    assert answer["content"] == "All good" and answer["usage"]["completion_tokens"] == 42
    # Only the final answer carries the turn's total; the tool round does not.
    assert [("usage" in m) for m in messages] == [False, False, False, True]
    # Without asking, the message shape is unchanged.
    assert "usage" not in store.get_messages(chat_id)[-1]


async def test_a_server_without_usage_gets_an_estimate(setup):
    events, _ = await turn(setup, [text("Hi there")])
    assert events[-1]["usage"]["estimated"] is True
    assert events[-1]["usage"]["completion_tokens"] == 2


async def test_budget_warning_is_shown_once_a_day_and_blocks_nothing(setup):
    cfg, store, memory, tools = setup
    cfg.pricing = {cfg.active: (1_000_000.0, 0.0)}        # $1 per prompt token
    cfg.usage_monthly_budget_usd = 10.0
    agent = Agent(cfg, store, memory, tools)
    first, _ = await turn(setup, [text("one")], [{"prompt_tokens": 5, "completion_tokens": 1}], agent=agent)
    assert "budget_warning" not in first[-1]
    second, _ = await turn(setup, [text("two")], [{"prompt_tokens": 4, "completion_tokens": 1}], agent=agent)
    warning = second[-1]["budget_warning"]
    assert warning["spent_usd"] == 9.0 and warning["monthly_usd"] == 10.0 and warning["warning"] is True
    third, _ = await turn(setup, [text("three")], [{"prompt_tokens": 4, "completion_tokens": 1}], agent=agent)
    assert third[-1]["type"] == "done" and "budget_warning" not in third[-1]


def test_an_old_database_gains_the_usage_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT, created TEXT NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, tool_calls TEXT, tool_call_id TEXT);
        INSERT INTO chats VALUES ('c1', 'old', '2026-01-01T00:00:00+00:00');
        INSERT INTO messages (chat_id, role, content) VALUES ('c1', 'assistant', 'hi');
    """)
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.get_messages("c1", include_usage=True) == [{"role": "assistant", "content": "hi"}]
    store.record_usage("c1", 1, {"prompt_tokens": 3, "completion_tokens": 4, "model": "m"})
    assert store.get_messages("c1", include_usage=True)[0]["usage"]["completion_tokens"] == 4


def test_forks_copy_no_usage_and_incognito_leaves_none(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    chat = store.create_chat()["id"]
    message = store.add_message(chat, "assistant", "hi")
    store.record_usage(chat, message, {"prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.5})
    fork = store.fork_chat(chat)["id"]
    assert "usage" not in store.get_messages(fork, include_usage=True)[0]
    secret = store.create_chat(ephemeral=True)["id"]
    hidden = store.add_message(secret, "assistant", "shh")
    store.record_usage(secret, hidden, {"prompt_tokens": 99, "completion_tokens": 99})
    rows = store.usage_rows("2000-01-01T00:00:00+00:00")
    assert [row["prompt_tokens"] for row in rows] == [10]
    store.delete_chat(chat)
    assert store.usage_rows("2000-01-01T00:00:00+00:00") == []


# -- totals ---------------------------------------------------------------------

def row(created: datetime, model="cloud", prompt=100, completion=10, cost=0.01, estimated=0):
    return {"created": created.astimezone(timezone.utc).isoformat(), "usage_model": model,
            "prompt_tokens": prompt, "completion_tokens": completion, "cost_usd": cost,
            "usage_estimated": estimated, "ms_generation": 1000, "usage_local": 0}


def test_summarize_groups_by_local_day_and_model_and_checks_the_budget():
    now = datetime(2026, 3, 20, 12, 0, tzinfo=timezone(timedelta(hours=1)))
    rows = [
        row(now - timedelta(days=40)),                      # before the month and the window
        row(now - timedelta(days=12), cost=0.5),            # this month, outside a 7-day window
        row(now - timedelta(days=1), model="lab", cost=None, estimated=1),
        row(now, cost=0.25),
        row(now.replace(hour=0, minute=30), cost=0.25),     # 23:30 UTC yesterday, today locally
    ]
    data = summarize(rows, days=7, now=now, budget_usd=1.0)
    assert data["since"] == "2026-03-14"
    assert data["total"]["answers"] == 3 and data["total"]["unpriced"] == 1 and data["total"]["estimated"] == 1
    assert data["today"]["answers"] == 2 and data["today"]["cost_usd"] == 0.5
    assert data["month"]["cost_usd"] == 1.0
    assert [day["day"] for day in data["by_day"]] == ["2026-03-19", "2026-03-20"]
    assert [model["model"] for model in data["by_model"]] == ["cloud", "lab"]
    assert data["by_model"][0]["tokens_per_second"] == 10.0
    assert data["budget"] == {"monthly_usd": 1.0, "spent_usd": 1.0, "ratio": 1.0, "warning": True}
    assert summarize([], days=7, now=now)["budget"]["warning"] is False


def test_window_start_covers_the_month_and_the_window_in_utc():
    now = datetime(2026, 3, 20, 12, 0, tzinfo=timezone(timedelta(hours=1)))
    assert usage.window_start(7, now) <= "2026-02-28T23:00:00+00:00"
    assert usage.window_start(60, now) <= "2026-01-20T00:00:00+00:00"
    assert usage.window_start(7, now).endswith("+00:00")


# -- surfaces -------------------------------------------------------------------

def test_usage_route_and_saved_chats_show_the_numbers(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    fake = FakeModel([text("Hello there!")], usage=[{"prompt_tokens": 1500, "completion_tokens": 3}])
    app = create_app(cfg, transport=fake.transport)
    headers = {"Authorization": f"Bearer {ensure_token(cfg.home)}"}
    with TestClient(app) as client:
        chat_id = client.post("/chats", headers=headers).json()["id"]
        client.post(f"/chats/{chat_id}/messages", json={"text": "hello"}, headers=headers)
        saved = client.get(f"/chats/{chat_id}", headers=headers).json()["messages"][-1]["usage"]
        assert saved["prompt_tokens"] == 1500 and saved["completion_tokens"] == 3
        data = client.get("/usage?days=7", headers=headers).json()
        assert data["days"] == 7 and data["today"]["answers"] == 1
        assert data["by_model"][0]["model"] == cfg.active
        assert data["prices_checked"] == usage.PRICES_CHECKED
        assert client.get("/usage?days=0", headers=headers).status_code == 422
        assert client.get("/usage").status_code == 401


def test_kova_usage_prints_a_table_and_json(monkeypatch):
    from typer.testing import CliRunner
    bucket = {"answers": 2, "prompt_tokens": 2400, "completion_tokens": 300, "cost_usd": 0.012,
              "unpriced": 0, "estimated": 0, "ms_generation": 10000, "tokens_per_second": 30.0}
    data = {"days": 30, "since": "2026-01-01", "total": bucket, "today": bucket, "month": bucket,
            "by_day": [], "by_model": [{"model": "cloud", **bucket}],
            "budget": {"monthly_usd": 0.0, "spent_usd": 0.012, "ratio": None, "warning": False},
            "prices_checked": usage.PRICES_CHECKED}

    class Client:
        def usage(self, days):
            assert days == 30
            return data

    monkeypatch.setattr(cli, "get_client", lambda: Client())
    result = CliRunner().invoke(cli.app, ["usage"])
    assert result.exit_code == 0, result.output
    for expected in ("cloud", "2.4k", "300", "~$0.01", "30 tok/s", "Today", "This month"):
        assert expected in result.output
    result = CliRunner().invoke(cli.app, ["usage", "--json"])
    assert json.loads(result.output) == data


def test_terminal_chat_shows_the_servers_numbers_after_a_reply():
    import io
    from kova.tui.render import Renderer
    r = Renderer(io.StringIO(), color=False)
    r.begin_turn()
    r.handle({"type": "text", "text": "Hi."})
    r.handle({"type": "done", "usage": {"prompt_tokens": 1500, "completion_tokens": 40,
                                         "tokens_per_second": 25.5, "local": True}})
    r.finish_turn()
    assert "1.5k in   40 out   $0.00 local   25.5 tok/s" in r.out.getvalue()
