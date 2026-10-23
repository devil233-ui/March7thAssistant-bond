# coding:utf-8
"""启动流程健壮性回归测试。

背景：游戏冷启动时窗口句柄已创建但画面还截不到（pyautogui 只枚举可见窗口），
旧实现会连刷 60 秒「截图失败：没有找到游戏窗口」→ 抛「截图超时」→ 被外层
当成「启动失败」强杀游戏并重开，玩家停在登录界面的状态被反复打断。

这里锁住三条行为，避免以后又退回「一截不到图就杀进程」：
1. 「窗口长时间截不到图」是独立异常类型，不走强杀重试分支；
2. 「点击进入」流程里每一次画面操作都经过容错封装，单次失败不会中断整轮启动；
3. 「窗口已存在」分支也要确认是否已进主界面，不能跳过「点击进入」。
"""
import inspect

import pytest


def _load_game_module():
    try:
        import tasks.game as game
        return game
    except Exception as e:  # 任务模块依赖较重，环境不可用时跳过
        pytest.skip(f"tasks.game 依赖不可用: {e}")


def _nested_source(module, outer_name, inner_name):
    """取出 outer 函数体内定义的 inner 嵌套函数源码。"""
    src = inspect.getsource(getattr(module, outer_name))
    lines = src.splitlines()
    start = None
    indent = None
    for index, line in enumerate(lines):
        if line.strip().startswith(f"def {inner_name}("):
            start = index
            indent = len(line) - len(line.lstrip())
            break
    if start is None:
        raise AssertionError(f"{outer_name} 中找不到嵌套函数 {inner_name}")
    collected = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        collected.append(line)
    return "\n".join(collected)


class TestWindowNotReadyIsNotLaunchFailure:
    def test_is_runtime_error(self):
        game = _load_game_module()
        assert issubclass(game.GameWindowNotReadyError, RuntimeError)

    def test_raised_directly_without_killing_game(self):
        """窗口未就绪必须被单独抛出，不能进入「stop_game + 重试」分支。"""
        game = _load_game_module()
        src = inspect.getsource(game.start_game)
        assert "except (CloudGameLoginTimeoutError, GameWindowNotReadyError):" in src, \
            "窗口未就绪应与云游戏登录超时一样直接抛出，而不是强杀游戏重试"
        # 未就绪的抛出点必须排在重试循环里那个会调用 stop_game 的通用分支之前
        guard_at = src.index("except (CloudGameLoginTimeoutError, GameWindowNotReadyError):")
        retry_at = src.index("for retry in range(MAX_RETRY):")
        stop_at = src.index("starrail.stop_game()", src.index("except Exception as e:", retry_at))
        assert guard_at < stop_at


class TestEnterFlowIsFaultTolerant:
    def test_no_raw_auto_calls(self):
        """「点击进入」里每个画面操作都必须走 _try 封装，否则单次截图失败就中断启动。"""
        game = _load_game_module()
        seg = _nested_source(game, "start_game", "check_and_click_enter")
        assert "auto.click_element(" not in seg, "存在未容错的 auto.click_element 调用"
        assert "auto.find_element(" not in seg, "存在未容错的 auto.find_element 调用"
        assert "_try(auto." in seg

    def test_bilibili_branch_still_detects_matched_text(self):
        """容错封装不能顺手删掉 B 服分支依赖的 matched_text 判断。"""
        game = _load_game_module()
        seg = _nested_source(game, "start_game", "check_and_click_enter")
        assert "auto.matched_text" in seg


class TestAlreadyRunningBranchEntersHall:
    def test_waits_for_capturable_window(self):
        game = _load_game_module()
        seg = _nested_source(game, "start_game", "ensure_in_game")
        assert "_window_capturable" in seg
        assert "GameWindowNotReadyError" in seg

    def test_checks_main_screen_before_clicking(self):
        """游戏已在运行时先判断是否已在主界面，避免在大厅里误点其它按钮。"""
        game = _load_game_module()
        seg = _nested_source(game, "start_game", "ensure_in_game")
        assert "_is_main_screen_ready" in seg
        assert "already_running" in seg

    def test_both_branches_call_ensure_in_game(self):
        """两条入口（新启动 / 窗口已存在）都必须经过 ensure_in_game。"""
        game = _load_game_module()
        seg = _nested_source(game, "start_game", "start_local_game")
        assert seg.count("ensure_in_game(") == 2, "启动分支与已运行分支都应确保进入主界面"
