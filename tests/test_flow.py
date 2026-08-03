"""flow.py 的解析與驗證測試。不需要 playwright。"""

import pytest

from momo_watch.flow import (
    Action,
    Flow,
    FlowError,
    Guards,
    Step,
    load_flow,
    parse_flow,
    placeholder_steps,
)


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

    def test_shipped_example_is_never_directly_runnable(self):
        """flow.example.toml 的購物車與結帳步驟必須留著 TODO。

        商品頁的 selector 是實測過的真值（見檔案裡的說明），但購物車之後的
        步驟要先有東西在購物車才看得到，只能由使用者自己填。萬一哪天有人把
        整份填滿並提交，這個測試會擋下來。
        """
        flow = load_flow("flow.example.toml")
        assert flow.has_placeholders is True


class TestDryRunSteps:
    """演練只跑到第一個 mutating 步驟，所以 selector 可以分批填。"""

    def make(self, *specs):
        return parse_flow(
            {
                "steps": [
                    {"name": n, "action": "click", "selector": s, "mutating": m}
                    for n, s, m in specs
                ]
            }
        )

    def test_stops_after_first_mutating(self):
        flow = self.make(
            ("看", "#a", False),
            ("點", "#b", True),
            ("之後", "#c", False),
            ("再點", "#d", True),
        )
        assert [s.name for s in flow.dry_run_steps] == ["看", "點"]

    def test_includes_the_mutating_step_itself(self):
        """演練要驗證那個 mutating 步驟的 selector 找不找得到，所以必須含它。"""
        flow = self.make(("點", "#b", True))
        assert [s.name for s in flow.dry_run_steps] == ["點"]

    def test_no_mutating_step_means_all_steps(self):
        flow = self.make(("a", "#a", False), ("b", "#b", False))
        assert len(flow.dry_run_steps) == 2

    def test_placeholders_after_first_mutating_do_not_block_dry_run(self):
        """這是重點：購物車與結帳還沒填，也要能先驗證商品頁。"""
        flow = self.make(
            ("確認按鈕", "#add", False),
            ("加入購物車", "#add", True),
            ("結帳", "TODO_結帳鈕", False),
        )
        assert flow.has_placeholders is True
        assert placeholder_steps(flow.dry_run_steps) == []

    def test_placeholder_before_first_mutating_still_blocks(self):
        flow = self.make(("確認按鈕", "TODO_按鈕", False), ("加入購物車", "#add", True))
        assert placeholder_steps(flow.dry_run_steps) == ["確認按鈕"]


class TestPlaceholderSteps:
    def test_reports_names_not_just_a_boolean(self):
        """使用者要知道是哪幾步沒填，不是只知道「有東西沒填」。"""
        flow = parse_flow(
            {
                "steps": [
                    {"name": "好的", "action": "click", "selector": "#ok"},
                    {"name": "壞的一", "action": "click", "selector": "TODO_x"},
                    {"name": "壞的二", "action": "goto", "url": "TODO_y"},
                ]
            }
        )
        assert placeholder_steps(flow.steps) == ["壞的一", "壞的二"]

    def test_empty_when_all_filled(self):
        flow = parse_flow({"steps": [{"name": "好", "action": "click", "selector": "#ok"}]})
        assert placeholder_steps(flow.steps) == []


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
