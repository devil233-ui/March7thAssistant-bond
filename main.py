import os
import sys
import argparse
# 将当前工作目录设置为程序所在的目录，确保无论从哪里执行，其工作目录都正确设置为程序本身的位置，避免路径错误。
os.chdir(os.path.dirname(sys.executable) if getattr(sys, 'frozen', False)else os.path.dirname(os.path.abspath(__file__)))

from utils.console import ensure_utf8_output

# 输出流固定成 UTF-8：非 UTF-8 环境（cp1252 / cp936 / cp932 的控制台或管道）下打印中文会直接抛
# UnicodeEncodeError；而且 GUI 的内嵌日志（app/log_interface.py）现在也按 UTF-8 解码子进程输出。
ensure_utf8_output()

from utils.dpi import configure_dpi_awareness
from utils.tasks import AVAILABLE_TASKS


configure_dpi_awareness()


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        prog='March7th Assistant',
        description='三月七小助手 - 崩坏：星穹铁道自动化工具 (CLI)',
        epilog='更多信息请访问: https://m7a.top\n'
               '运行自定义流程: March7th Assistant.exe --workflow-name "流程名称"',
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    # 位置参数组
    positional = parser.add_argument_group('位置参数')
    positional.add_argument(
        'task',
        nargs='?',
        choices=list(AVAILABLE_TASKS.keys()),
        metavar='TASK',
        help='要执行的任务名称（可选，不指定则执行完整运行）'
    )

    # 可选参数组
    optional = parser.add_argument_group('可选参数')
    optional.add_argument(
        '--no-run-immediately',
        action='store_true',
        help='禁用立即运行'
    )
    optional.add_argument(
        '-h', '--help',
        action='help',
        help='显示此帮助信息并退出'
    )
    optional.add_argument(
        '-l', '--list',
        action='store_true',
        help='列出所有可用的任务'
    )
    optional.add_argument(
        '--list-workflows',
        action='store_true',
        help='列出所有可用的流程'
    )
    optional.add_argument(
        '--workflow-name',
        metavar='NAME',
        help='按名称运行流程（用 --list-workflows 查看可用名称）'
    )
    optional.add_argument(
        '--workflow-step-path',
        metavar='PATH',
        help='仅执行指定步骤路径，例如 0/1/2'
    )

    args = parser.parse_args()

    # 处理 --list 参数
    if args.list:
        from module.localization import load_language
        from utils.tasks import task_display_names

        load_language()
        print("\n可用的任务列表:")
        print("-" * 40)
        for task_id, task_name in task_display_names().items():
            print(f"  {task_id:<20} {task_name}")
        print("-" * 40)
        print("\n使用示例:")
        print("  启动并执行完整运行:     March7th Assistant.exe main")
        print("  执行每日实训:           March7th Assistant.exe daily")
        sys.exit(0)

    # 处理 --list-workflows 参数
    if args.list_workflows:
        from module.workflow import list_workflow_names

        names = list_workflow_names()
        print("\n可用的流程列表:")
        print("-" * 40)
        if names:
            for name in names:
                print(f"  {name}")
        else:
            print("  （暂无流程，请先在图形界面的流程编排中创建）")
        print("-" * 40)
        print("\n使用示例:")
        print('  运行流程:       March7th Assistant.exe --workflow-name "流程名称"')
        print('  运行指定步骤:   March7th Assistant.exe --workflow-name "流程名称" --workflow-step-path 0/1')
        sys.exit(0)

    if args.task and args.workflow_name:
        parser.error('不能同时指定 TASK 和 --workflow-name')

    if args.workflow_step_path and not args.workflow_name:
        parser.error('--workflow-step-path 需要配合 --workflow-name 使用')

    return args


args = parse_args()


import atexit
import base64
import ctypes
import ctypes.wintypes
import subprocess
import threading
import time

if sys.platform == 'win32':
    from utils.admin import is_user_admin, run_as_admin
    if not is_user_admin():
        try:
            run_as_admin()
            sys.exit(0)
        except Exception:
            sys.exit(1)

from module.config import cfg
from module.logger import log
from module.notification import notif
from module.telemetry import telemetry
from module.notification.notification import NotificationLevel

from module.localization import load_language

# 初始化界面语言（CLI 界面输出与后续 tr() 使用；日志按约定固定中文原文）
load_language()
from module.ocr import ocr
from module.workflow import WorkflowRunner, load_workflow_execution_payload, describe_available_workflows
from utils.screenshot_util import save_error_screenshot

import tasks.game as game
from module.game import cloud_game
import tasks.reward as reward
import tasks.challenge as challenge
import tasks.version as version
import tasks.version.app_update as app_update_task

from tasks.daily.daily import Daily
from tasks.daily.fight import Fight
from tasks.power.power import Power
from tasks.weekly.universe import Universe
from tasks.daily.redemption import Redemption
from tasks.weekly.currency_wars import CurrencyWars
from tasks.weekly.divergent_universe import DivergentUniverse
from tasks.base.genshin_starRail_fps_unlocker import Genshin_StarRail_fps_unlocker
from tasks.base import screen_test


from utils.console import pause_on_error, pause_on_success, pause_always, is_docker_started
from utils.pause import (pause_ctl, reset_files, write_command,
                         COMMAND_PAUSE, COMMAND_RESUME)

telemetry.init(
    enabled=cfg.get_value("telemetry_enable", True),
    telemetry_id=cfg.get_value("telemetry_id", ""),
    telemetry_secret=cfg.get_value("telemetry_secret", ""),
    version=cfg.version,
)


def first_run():
    if not is_docker_started() and not cfg.get_value(base64.b64decode("YXV0b191cGRhdGU=").decode("utf-8")):
        log.error("首次使用请先打开图形界面 March7th Launcher")
        pause_always()
        sys.exit(0)


_LOG_OVERLAY_PROC = None


# ---------------------------------------------------------------- 运行控制热键
# F9 暂停/恢复、F10 停止。全局热键由独立线程注册：RegisterHotKey(None, ...) 绑到该线程，
# 消息只进它的队列，不会和主线程的自动化操作抢消息。
_STOP_EVENT = threading.Event()
_CURRENT_WAR = None
_CONTROL_PATH = None          # 暂停控制文件路径，F9 通过写它下发指令
CONTROL_HOTKEY_PAUSE = 0x78   # VK_F9
CONTROL_HOTKEY_STOP = 0x79    # VK_F10


class ControlHotkeys(threading.Thread):
    """F9 暂停/恢复、F10 停止；游戏窗口在前台时也能用（全局热键）。"""

    def __init__(self):
        super().__init__(daemon=True, name="M7AControlHotkeys")
        self._stopped = threading.Event()
        self._registered = []

    def run(self):
        user32 = ctypes.windll.user32
        WM_HOTKEY, PM_REMOVE = 0x0312, 0x0001
        for hotkey_id, vk in ((1, CONTROL_HOTKEY_PAUSE), (2, CONTROL_HOTKEY_STOP)):
            if user32.RegisterHotKey(None, hotkey_id, 0, vk):
                self._registered.append(hotkey_id)
            else:
                name = "F9" if vk == CONTROL_HOTKEY_PAUSE else "F10"
                log.warning(f"{name} 热键注册失败（可能被其它程序占用）")
        if not self._registered:
            return
        log.info("控制热键已就绪：F9 暂停/恢复，F10 停止")
        try:
            msg = ctypes.wintypes.MSG()
            while not self._stopped.is_set():
                if user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
                    if msg.message == WM_HOTKEY:
                        self._handle(int(msg.wParam))
                else:
                    time.sleep(0.05)
        finally:
            for hotkey_id in self._registered:
                user32.UnregisterHotKey(None, hotkey_id)

    def _handle(self, hotkey_id: int):
        if hotkey_id == 1:
            # 必须写控制文件，不能只调 pause_ctl.request_pause()：
            # 那个只改内存状态，而 watcher 线程每 0.3 秒读一次文件，
            # 文件里还留着上一轮的 resume，会立刻把这次暂停撤掉（按了没反应）。
            if not _CONTROL_PATH:
                return
            command = COMMAND_RESUME if pause_ctl.is_paused() else COMMAND_PAUSE
            try:
                write_command(_CONTROL_PATH, command)
            except Exception as e:
                log.error(f"写入暂停指令失败：{e}")
        elif hotkey_id == 2:
            # 停止键就该立刻停：走「放弃并结算」要等结算动画几分钟，这里直接退进程。
            # 游戏留着不动，方便手动接着打或看结算。
            log.warning("收到停止指令（F10），立即退出（游戏保留）")
            _STOP_EVENT.set()
            try:
                stop_log_overlay()
            except Exception:
                pass
            try:
                sys.stdout.flush()
                sys.stderr.flush()
                time.sleep(0.1)  # 给日志落盘留一点时间
            except Exception:
                pass
            os._exit(0)

    def stop(self):
        self._stopped.set()


def _kill_stale_overlay():
    """清掉上一次残留的悬浮窗（手动起的、或上次被强杀没带走的），避免叠两个。"""
    try:
        import psutil
        for proc in psutil.process_iter(attrs=["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any("game_log_overlay.py" in str(part) for part in cmdline):
                    proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass


def start_log_overlay():
    """拉起游戏内日志悬浮窗（独立进程）。

    命令行跑任务时没有任何 GUI，日志只进控制台；游戏窗口一盖上来就看不到了。
    `tools/game_log_overlay.py` 复刻了 GUI 里那个贴在游戏左下角的只读日志窗，
    这里把它作为子进程带起来：主进程退出时一并结束，日志静默 10 分钟也会自退。
    配置项 `log_overlay_enable` 设为 false 可关掉。
    """
    global _LOG_OVERLAY_PROC
    if sys.platform != "win32" or _LOG_OVERLAY_PROC is not None:
        return _LOG_OVERLAY_PROC
    if not cfg.get_value("log_overlay_enable", True):
        return None
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "game_log_overlay.py")
    if not os.path.exists(script):
        log.warning(f"未找到日志悬浮窗脚本，跳过：{script}")
        return None
    exe = sys.executable
    if exe.endswith("python.exe"):  # 用 pythonw 才不会多弹一个控制台
        windowless = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(windowless):
            exe = windowless
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _kill_stale_overlay()
        time.sleep(0.3)  # 让旧进程让出 Ctrl+Alt+L 热键
        _LOG_OVERLAY_PROC = subprocess.Popen(
            [exe, "-X", "utf8", script, "--exit-when-idle", "600"],
            creationflags=flags,
        )
        log.info("日志悬浮窗已启动（Ctrl+Alt+L 切换显示/隐藏）")
    except Exception as e:
        log.warning(f"日志悬浮窗启动失败：{e}")
        _LOG_OVERLAY_PROC = None
    return _LOG_OVERLAY_PROC


def stop_log_overlay():
    """结束日志悬浮窗子进程（主进程收尾时调用）。"""
    global _LOG_OVERLAY_PROC
    if _LOG_OVERLAY_PROC is None:
        return
    try:
        if _LOG_OVERLAY_PROC.poll() is None:
            _LOG_OVERLAY_PROC.terminate()
    except Exception:
        pass
    _LOG_OVERLAY_PROC = None


def run_main_actions(no_run_immediately=False):
    is_first_run = no_run_immediately
    while True:
        if is_first_run:
            is_first_run = False
            game.after_finish_is_loop()
            continue
        if cfg.notify_merge:
            notif.start_batch()
        telemetry.track_task_start("main")
        main_start_time = time.time()
        try:
            version.start()
            game.start()
            Daily.start()
            reward.start()
            telemetry.track_task_complete("main", True, time.time() - main_start_time)
            game.stop(True)
        except Exception:
            telemetry.track_task_complete("main", False, time.time() - main_start_time)
            raise


def run_sub_task(action):
    if action not in ("currencywarstemp", "divergenttemp"):
        game.start()
    else:
        if cfg.cloud_game_enable:
            if not cloud_game.start_game_process():
                raise ConnectionError("启动或连接浏览器失败")
        game.switch_to_game()

    def _run_loop_task_with_telemetry(task_id, task_fn):
        while not _STOP_EVENT.is_set():
            telemetry.track_task_start(task_id)
            task_start_time = time.time()
            try:
                success = bool(task_fn())
                telemetry.track_task_complete(task_id, success, time.time() - task_start_time)
            except Exception:
                telemetry.track_task_complete(task_id, False, time.time() - task_start_time)
                raise
        log.info("已按停止指令结束循环")

    def currencywars(mode=None):
        global _CURRENT_WAR
        war = CurrencyWars()
        _CURRENT_WAR = war  # F10 停止时要能给这个实例打 need_exit
        # 不带 loop 也默认一局接一局地跑（currencywars_loop_enable 设为 false 可还原成单局）
        loop = mode == "loop" or (mode is None and cfg.get_value("currencywars_loop_enable", True))
        if loop:
            _run_loop_task_with_telemetry("currencywarsloop" if mode == "loop" else "currencywars", war.start)
        elif mode == "temp":
            war.loop()
        else:
            war.start()

    def divergent(mode=None):
        universe = DivergentUniverse()
        if mode == "loop":
            _run_loop_task_with_telemetry("divergentloop", universe.start)
        elif mode == "temp":
            universe.loop()
        else:
            universe.start()

    sub_tasks = {
        "routine": Daily.routine,
        "daily": lambda: (Daily.run(), reward.start()),
        "power": Power.run,
        "currencywars": lambda: currencywars(),
        "currencywarsloop": lambda: currencywars("loop"),
        "currencywarstemp": lambda: currencywars("temp"),
        "divergent": lambda: divergent(),
        "divergentloop": lambda: divergent("loop"),
        "divergenttemp": lambda: divergent("temp"),
        "fight": Fight.start,
        "screen_test": screen_test.run,
        "universe": lambda: Universe.start(category="universe"),
        "forgottenhall": lambda: challenge.start("memoryofchaos"),
        "purefiction": lambda: challenge.start("purefiction"),
        "apocalyptic": lambda: challenge.start("apocalyptic"),
        "redemption": Redemption.start
    }
    task = sub_tasks.get(action)
    if task:
        if action in {"currencywarsloop", "divergentloop"}:
            task()
        else:
            telemetry.track_task_start(action)
            task_start_time = time.time()
            try:
                task()
                telemetry.track_task_complete(action, True, time.time() - task_start_time)
            except Exception:
                telemetry.track_task_complete(action, False, time.time() - task_start_time)
                raise
    if action != "screen_test":
        game.stop(False)


def run_sub_task_gui(action):
    gui_tasks = {
        "universe_gui": Universe.gui,
        "fight_gui": Fight.gui
    }
    task = gui_tasks.get(action)
    if task and not task():
        pause_always()
    sys.exit(0)


def run_sub_task_update(action):
    update_tasks = {
        "universe_update": Universe.update,
        "fight_update": Fight.update,
        "mobileui_update": Genshin_StarRail_fps_unlocker.update
    }
    task = update_tasks.get(action)
    if task:
        task()
    pause_always()
    sys.exit(0)


def run_notify_action():
    notif.notify(content=cfg.notify_template['TestMessage'], image="./assets/app/images/March7th.jpg", level=NotificationLevel.ALL)
    pause_always()
    sys.exit(0)


def run_workflow_action(workflow_name: str, workflow_step_path=None):
    # 先校验用户输入（名称/步骤路径），再做游戏窗口检查与切换：
    # 名称错误不应要求游戏在运行才能得到反馈
    try:
        workflow = load_workflow_execution_payload(workflow_name, workflow_step_path)
    except ValueError:
        # 名称/步骤路径错误属于用户输入问题：给出可用流程清单，不走异常通知/截图
        step_hint = f"（步骤路径：{workflow_step_path}）" if workflow_step_path else ""
        log.error(
            f"未找到流程「{workflow_name}」或步骤路径无效{step_hint}"
            f"；可用流程：{describe_available_workflows()}（使用 --list-workflows 查看）"
        )
        sys.exit(1)
    # 与流程编排启动语义一致：游戏未启动或无法切换到游戏窗口时直接报错终止，
    # 避免按键/点击打进当前聚焦的其它窗口（定时任务、命令行直跑均在此收口）
    if not game.ensure_game_ready():
        sys.exit(1)
    runner = WorkflowRunner(
        log_callback=lambda message: print(message, flush=True),
        mirror_to_project_log=False,
    )
    return runner.run(workflow)


def main(action=None, no_run_immediately=False, workflow_name=None, workflow_step_path=None):
    first_run()
    # 启动暂停控制器：GUI 是用 MARCH7TH_CONTROL_FILE 注入路径的，命令行直跑时没有该变量，
    # 这里落到程序目录下的控制文件，F9 暂停/恢复在 CLI 场景才有效
    global _CONTROL_PATH
    _CONTROL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".m7a_control.json")
    reset_files(_CONTROL_PATH)  # 清掉上一次残留的 pause 指令，否则一启动就停着
    pause_ctl.start(control_path=_CONTROL_PATH)
    ControlHotkeys().start()
    start_log_overlay()
    telemetry.track_startup()

    if workflow_name:
        return run_workflow_action(workflow_name, workflow_step_path)

    # 完整运行
    if action is None or action == "main":
        run_main_actions(no_run_immediately)

    # 子任务
    elif action in ["routine", "daily", "power", "currencywars", "currencywarsloop", "currencywarstemp", "divergent", "divergentloop", "divergenttemp", "fight", "universe", "forgottenhall", "purefiction", "apocalyptic", "redemption", "screen_test"]:
        run_sub_task(action)

    # 子任务 原生图形界面
    elif action in ["universe_gui", "fight_gui"]:
        run_sub_task_gui(action)

    # 子任务 更新项目
    elif action in ["universe_update", "fight_update", "mobileui_update"]:
        run_sub_task_update(action)

    elif action == "game":
        game.start()

    elif action == "app_update":
        app_update_task.start()

    elif action == "game_update":
        game.update_via_launcher()

    elif action == "game_pre_download":
        game.pre_download_via_launcher()

    elif action == "notify":
        run_notify_action()

    else:
        log.error(f"未知任务: {action}")
        pause_on_error()
        sys.exit(1)


# 程序结束时的处理器
def exit_handler():
    """注册程序退出时的处理函数，用于清理OCR和调试资源."""
    telemetry.shutdown()
    ocr.exit_ocr()
    stop_log_overlay()
    # 清理调试叠加层
    try:
        from module.automation import auto
        auto.shutdown_debug()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        atexit.register(exit_handler)
        if args.workflow_name:
            result = main(
                no_run_immediately=args.no_run_immediately,
                workflow_name=args.workflow_name,
                workflow_step_path=args.workflow_step_path,
            )
        elif args.task:
            result = main(action=args.task, no_run_immediately=args.no_run_immediately)
        else:
            result = main(no_run_immediately=args.no_run_immediately)
        if result is False:
            sys.exit(1)
    except KeyboardInterrupt:
        log.error("发生错误: 手动强制停止")
        pause_on_error()
        sys.exit(1)
    except Exception as e:
        telemetry.track_error(type(e).__name__, str(e))
        log.error(cfg.notify_template['ErrorOccurred'].format(error=e))
        # 保存错误截图
        screenshot_path = save_error_screenshot(log)
        # 合并模式下先发送已收集的通知
        notif.flush_batch()
        # 发送通知，如果有截图则附带截图
        notify_kwargs = {
            'content': cfg.notify_template['ErrorOccurred'].format(error=e),
            'level': NotificationLevel.ERROR
        }
        if screenshot_path:
            notify_kwargs['image'] = screenshot_path
        notif.notify(**notify_kwargs)
        pause_on_error()
        sys.exit(1)
