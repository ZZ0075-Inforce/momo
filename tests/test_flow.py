"""flow.py 的解析與驗證測試。不需要 playwright。"""

import pytest

from momo_watch.flow import Action, Flow, FlowError, Guards, Step, load_flow, parse_flow


def make(**overrides):
    data = {
        "steps": [{"name": "去商品頁", "action": "goto", "url": "https://x/{code}"}],
    }
    data.update(overrides)
    return parse_flow(data)


class TestParseSteps:
    def test_minimal_flow(self):
        flow = make()
        assert len(flow.steps) == 1
        assert flow.steps[0].action is Action.GOTO

    def test_all_actions_parse(self):
        flow = parse_flow(
            {
                "steps": [
                    {"name": "a", "action": "goto", "url": "u"},
                    {"name": "b", "action": "click", "selector": "#s"},
                    {"name": "c", "action": "fill", "selector": "#s", "value": "v"},
                    {"name": "d", "action": "wait_for", "selector": "#s"},
                    {"name": "e", "action": "expect", "selector": "#s"},
                    {"name": "f", "action": "expect_not", "selector": "#s"},
                    {"name": "g", "action": "screenshot", "path": "p.png"},
                ]
            }
        )
        assert len(flow.steps) == 7

    def test_optional_and_mutating_flags(self):
        flow = parse_flow(
            {
                "steps": [
                    {
                        "name": "點",
                        "action": "click",
                        "selector": "#s",
                        "mutating": True,
                        "optional": True,
                        "timeout_ms": 1234,
                    }
                ]
            }
        )
        step = flow.steps[0]
        assert step.mutating is True
        assert step.optional is True
        assert step.timeout_ms == 1234

    def test_empty_steps_rejected(self):
        with pytest.raises(FlowError, match="沒有任何"):
            parse_flow({"steps": []})

    def test_unknown_action_rejected(self):
        with pytest.raises(FlowError, match="無效"):
            parse_flow({"steps": [{"name": "x", "action": "teleport"}]})

    def test_missing_name_rejected(self):
        with pytest.raises(FlowError, match="缺少 name"):
            parse_flow({"steps": [{"action": "goto", "url": "u"}]})

    @pytest.mark.parametrize(
        ("raw", "missing"),
        [
            ({"name": "x", "action": "goto"}, "url"),
            ({"name": "x", "action": "click"}, "selector"),
            ({"name": "x", "action": "screenshot"}, "path"),
            ({"name": "x", "action": "fill", "selector": "#s"}, "value"),
        ],
    )
    def test_missing_required_field_rejected(self, raw, missing):
        """設定檔缺欄位要在載入時就爆，不要等到搶購當下才炸。"""
        with pytest.raises(FlowError, match=missing):
            parse_flow({"steps": [raw]})


class TestGuards:
    def test_defaults_are_conservative(self):
        guards = make().guards
        assert guards.dry_run is True
        assert guards.stop_before_payment is True
        assert guards.max_attempts == 1
        assert guards.max_price is None

    def test_guards_are_read(self):
        flow = make(
            guards={
                "max_price": 15000,
                "dry_run": False,
                "stop_before_payment": False,
                "max_attempts": 3,
            }
        )
        assert flow.guards == Guards(
            max_price=15000, stop_before_payment=False, dry_run=False, max_attempts=3
        )


class TestRender:
    def test_substitutes_code(self):
        step = Step(name="x", action=Action.GOTO, url="https://m/?i_code={code}")
        assert step.render({"code": "6453015"}).url == "https://m/?i_code=6453015"

    def test_substitutes_in_selector_and_path(self):
        step = Step(
            name="x",
            action=Action.SCREENSHOT,
            path="shots/{code}.png",
            selector="[data-code='{code}']",
        )
        rendered = step.render({"code": "123"})
        assert rendered.path == "shots/123.png"
        assert rendered.selector == "[data-code='123']"

    def test_unknown_variable_is_an_error(self):
        step = Step(name="x", action=Action.GOTO, url="https://m/?q={nope}")
        with pytest.raises(FlowError, match="未知的變數"):
            step.render({"code": "123"})

    def test_render_preserves_flags(self):
        step = Step(name="x", action=Action.CLICK, selector="#s", mutating=True, optional=True)
        rendered = step.render({})
        assert rendered.mutating is True
        assert rendered.optional is True


class TestPlaceholderDetection:
    def test_todo_selector_is_detected(self):
        flow = parse_flow({"steps": [{"name": "x", "action": "click", "selector": "TODO_按鈕"}]})
        assert flow.has_placeholders is True

    def test_todo_url_is_detected(self):
        flow = parse_flow({"steps": [{"name": "x", "action": "goto", "url": "TODO_網址"}]})
        assert flow.has_placeholders is True

    def test_filled_flow_is_clean(self):
        flow = parse_flow({"steps": [{"name": "x", "action": "click", "selector": "#add-cart"}]})
        assert flow.has_placeholders is False

    def test_shipped_example_is_all_placeholders(self):
        """flow.example.toml 必須留著 TODO —— 它是範本不是可直接用的設定。

        萬一哪天有人把真的 selector 填進範例檔並提交，這個測試會擋下來。
        """
        flow = load_flow("flow.example.toml")
        assert flow.has_placeholders is True


class TestLoadFlow:
    def test_missing_file_gives_actionable_error(self, tmp_path):
        with pytest.raises(FlowError, match="flow.example.toml"):
            load_flow(tmp_path / "nope.toml")

    def test_example_file_parses(self):
        flow = load_flow("flow.example.toml")
        assert isinstance(flow, Flow)
        assert len(flow.steps) > 5
        assert flow.guards.stop_before_payment is True
        assert flow.guards.dry_run is True

    def test_example_never_submits_payment(self):
        """範例流程不該包含任何送出付款的步驟。"""
        flow = load_flow("flow.example.toml")
        mutating = [s for s in flow.steps if s.mutating]
        assert all("付款" not in s.name for s in mutating)
