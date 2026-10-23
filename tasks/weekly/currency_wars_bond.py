# -*- coding: utf-8 -*-
"""羁绊链路策略（bond_chain）：按「未完成的羁绊链路任务」组织买人与上阵。

与 `aglaea` / `seele` 两套预设策略的区别：那两套是固定名单，本策略的数据来自官方
图鉴（见 `currency_wars_bond_data.py`），目标由游戏内「羁绊链路」页面的任务决定：
凑够某羁绊的 N 名成员通关即可完成该羁绊所有档位的任务（最高档覆盖低档）。

本模块只做**决策**，不碰截图与坐标；界面动作全部通过传入的 CurrencyWars 实例
（鸭子类型）完成，因此可以在没有游戏、没有 Qt 的环境里单测。

用法（在 check_main_screen 的策略分派处）：

    elif cfg.currencywars_strategy == "bond_chain":
        bond_chain_step(self)
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, List, Mapping, Optional, Sequence

from tasks.weekly.currency_wars_bond_data import BONDS, STAFF, TASK_TIERS

try:  # 宿主环境才有 module.automation；单测/离线规划没有 Qt/OCR 也能跑
    from module.automation import auto
except ImportError:  # pragma: no cover
    auto = None

log = logging.getLogger(__name__)

try:  # 宿主里把日志转发到小助手的 Logger（否则 __name__ logger 没配 handler，info 会被吞）
    from module.logger import log as _host_log

    class _HostForward:
        def __init__(self, host):
            self._host = host

        def info(self, msg, *a):
            self._host.info(msg % a if a else msg)

        def warning(self, msg, *a):
            self._host.warning(msg % a if a else msg)

        def error(self, msg, *a):
            self._host.error(msg % a if a else msg)

    log = _HostForward(_host_log)
except ImportError:  # pragma: no cover
    pass

MIDDLES = "•·・‧∙⋅-－—–"
ALIAS = {"阮梅": "阮·梅", "彦青": "彦卿"}
GENERIC = {"开拓者": "开拓者·记忆"}
REMEMBRANCE_KEY = "开拓者·记忆"
REFRESH_COST = 2          # 刷新商店固定扣 2 货币
MIN_MONEY_TO_REFRESH = 4  # 刷新后仍要留钱买人
# 商店五张卡的区域（与 currency_wars.buy_aglaea 的 shop_crop 一致）
SHOP_CROP = (344 / 1920, 19 / 1080, 1370 / 1920, 526 / 1080)
PROGRESS_FILE = "bond_chain_state.json"  # 跨局进度：做过哪几局（相对进程工作目录）
MAX_STAR_BUYS = 2         # 已拥有的前台角色还可再买几张（凑 2 星、3 星）
LATE_STAGE_MIN = (3, 6)   # 第三章第 6 关起算后期：钱不够时允许卖场上占位角色换钱
# OCR 对角色名的常见误读（实机 2026-10-23 第一局发现「千冶·刃」读成「干冶·刃」）
OCR_NAME_FIX = {"干冶·刃": "千冶·刃"}


def fix_ocr_name(name: str) -> str:
    """OCR 名字误读修正；识别正确时原样返回。"""
    return OCR_NAME_FIX.get(name, name)


def normalize(name: Optional[str]) -> str:
    """角色名归一：去括号与性别后缀、统一中点、转小写，与数据模块的 key 对齐。"""
    n = re.sub(r"[（(][^）)]*[）)]", "", name or "").strip()
    for ch in MIDDLES:
        n = n.replace(ch, "·")
    n = re.sub(r"·+", "·", n).strip("·").lower()
    n = ALIAS.get(n, n)
    return GENERIC.get(n, n)


def display_name(key: str, remembrance_trailblazer_name: Optional[str] = None) -> str:
    """数据 key → 游戏里显示的名字。「开拓者·记忆」在游戏内显示为玩家自己的名字。"""
    if key == REMEMBRANCE_KEY and remembrance_trailblazer_name:
        return remembrance_trailblazer_name
    return STAFF.get(key, (key,))[0]


def cost_of(key: str) -> Optional[int]:
    got = STAFF.get(key)
    return got[1] if got else None


def bond_members(bond: str) -> List[str]:
    return list(BONDS.get(bond, ("", [], []))[2])


def bonds_of(character: str) -> List[str]:
    """某角色所属的羁绊（归一后比对）。"""
    key = normalize(character)
    return [b for b, (_, _, members) in BONDS.items() if key in members]


@dataclass(frozen=True)
class BondTask:
    """一条羁绊链路任务：某羁绊要凑够 target 人。"""
    bond: str
    target: int
    done: bool = False


@dataclass(frozen=True)
class Candidate:
    bond: str
    target: int
    owned: int            # 已拥有的成员数
    need: int             # 还差几人
    cost: int             # 还差的人里最便宜的 need 人合计费用
    keys: tuple           # 还差的成员 key（按费用升序）

    @property
    def finished(self) -> bool:
        return self.need == 0


def default_tasks() -> List[BondTask]:
    """默认任务表：每个有任务的羁绊取最高档（最高档覆盖低档）。"""
    return [BondTask(b, max(tiers)) for b, tiers in sorted(TASK_TIERS.items())]


def parse_tasks(items: Iterable) -> List[BondTask]:
    """把（从羁绊链路页面读来的）任务列表折成每个羁绊一条最高档任务。

    items 每项形如 {"bond": "夜之半神", "target": 7, "done": False}；
    已全部完成的羁绊会被标记为 done。
    """
    best = {}
    for it in items:
        bond = it["bond"]
        target = int(it["target"])
        done = bool(it.get("done"))
        cur = best.get(bond)
        if cur is None:
            best[bond] = BondTask(bond, target, done)
        else:
            # 同一羁绊取最高档；只有所有条目都完成才算完成
            best[bond] = BondTask(bond, max(cur.target, target), cur.done and done)
    return [best[b] for b in sorted(best)]


def candidates(tasks: Sequence[BondTask], owned: Iterable[str]) -> List[Candidate]:
    """算出每个未完成任务的缺口与最低花费，按「好凑」排序。

    排序键：①已凑满的排最前（只需保持）；②还差的钱；③还差的人；④任务档位低；
    ⑤羁绊名字典序（保证结果可复现）。
    """
    owned_keys = {normalize(x) for x in owned if x}
    out = []
    for task in tasks:
        if task.done:
            continue
        members = bond_members(task.bond)
        if not members:
            continue
        have = [k for k in members if k in owned_keys]
        missing = [k for k in members if k not in owned_keys]
        missing.sort(key=lambda k: (cost_of(k) is None, cost_of(k) or 0, k))
        need = max(0, task.target - len(have))
        picked = tuple(missing[:need])
        if len(picked) < need:  # 官方成员数不足以凑到该档
            continue
        out.append(Candidate(task.bond, task.target, len(have), need,
                             sum(cost_of(k) or 0 for k in picked), picked))
    out.sort(key=lambda c: (not c.finished, c.cost, c.need, c.target, c.bond))
    return out


def choose(tasks: Sequence[BondTask], owned: Iterable[str]) -> Optional[Candidate]:
    got = candidates(tasks, owned)
    return got[0] if got else None


def purchase_order(owned: Iterable[str], cand: Candidate) -> List[str]:
    """本局买人顺序：先把已有成员补齐到目标档，其余成员按费用从低到高。"""
    owned_keys = {normalize(x) for x in owned if x}
    members = bond_members(cand.bond)
    missing = [k for k in members if k not in owned_keys]
    missing.sort(key=lambda k: (cost_of(k) is None, cost_of(k) or 0, k))
    return [display_name(k) for k in missing[:max(cand.need, 0)]]


# ---------------------------------------------------------------- 分局规划

FRONT_SLOTS = 4    # 前台格固定 4
BACK_SLOTS = 6     # 后台格最少 6（佩佩和财富宝钻各再 +1）
MAX_LINEUP = 10    # 单局可部署人数上限
POS_MAP = {"前台": "front", "后台": "back", "前后台": "both"}

# 角色 → 所属羁绊（一个角色可以同时属于多个羁绊，所以一局能推进多条任务）
CHAR_BONDS = {}
for _bond, (_type, _tiers, _members) in BONDS.items():
    for _m in _members:
        CHAR_BONDS.setdefault(_m, set()).add(_bond)


def position_of(key: str) -> str:
    """官方员工表的站位；查不到按「前后台」处理。"""
    got = STAFF.get(key)
    return POS_MAP.get(got[2] if got and len(got) > 2 else "", "both")


class Slots:
    """一局的站位账本：前台 4 格、后台 6 格，「前后台」角色哪边空去哪边。"""

    def __init__(self, front: int = FRONT_SLOTS, back: int = BACK_SLOTS):
        self.front = front
        self.back = back

    @staticmethod
    def counts(lineup: Sequence[str]):
        front = sum(1 for c in lineup if position_of(c) == "front")
        back = sum(1 for c in lineup if position_of(c) == "back")
        return front, back, len(lineup) - front - back

    def fits(self, lineup: Sequence[str], cand: str) -> bool:
        pos = position_of(cand)
        front, back, both = self.counts(lineup)
        if pos == "front":
            front += 1
        elif pos == "back":
            back += 1
        else:
            both += 1
        if front > self.front or back > self.back:
            return False
        if front + back + both > MAX_LINEUP:
            return False
        return both <= (self.front - front) + (self.back - back)


@dataclass(frozen=True)
class RunPlan:
    """一局的计划：主目标羁绊 + 一屏阵容 + 顺带完成的羁绊。"""
    target: str
    need: int
    lineup: tuple
    covered: tuple        # 本局能完成的羁绊名（含主目标）

    def __str__(self) -> str:
        return "%s（%d 人）＋顺带 %s" % (
            self.target, self.need,
            "、".join(b for b in self.covered if b != self.target) or "无")


def plan_runs(tasks: Sequence[BondTask], emblems: Optional[Mapping[str, int]] = None,
              max_runs: int = 40) -> List[RunPlan]:
    """贪心分局：每局挑一个未完成任务当主目标，用共享成员顺带覆盖其它任务。

    一个角色可同时属于多个羁绊，所以一局能完成多条任务（例如贝洛伯格 4 与治疗 4
    共用杰帕德/娜塔莎）。站位同时受两重约束：前台 4 格、后台 6 格、总人数 10。
    拿到某羁绊的星徽（`emblems`）时，该羁绊少凑一个人——星徽挂在任意角色身上，
    那个角色就额外算作该羁绊成员。结果是「这么多局够用」的贪心解，不是最小局数的证明。
    """
    own_emblems = {k: int(v) for k, v in (emblems or {}).items() if v}

    def need_of(bond: str, target: int) -> int:
        """算上星徽后的实际人数需求：一个星徽抵一个人，最少也要 1 人。"""
        return max(int(target) - own_emblems.get(bond, 0), 1)

    pending = {t.bond: need_of(t.bond, t.target) for t in tasks
               if not t.done and len(set(bond_members(t.bond))) + own_emblems.get(t.bond, 0) >= t.target}
    runs: List[RunPlan] = []
    slots = Slots()
    while pending:
        best = None
        for target, need in pending.items():
            cands = list(dict.fromkeys(bond_members(target)))
            # 主目标成员里，优先留同时属于其它待办羁绊的人；交集同数时贵者优先，
            # 这样前排不会被便宜的单羁绊前台占满，贵的前台(如瓦尔特/黄泉)能进来
            cands.sort(key=lambda c: (-len(CHAR_BONDS.get(c, set()) & set(pending)), -(cost_of(c) or 0)))
            lineup: List[str] = []
            for c in cands:
                if sum(1 for x in lineup if target in CHAR_BONDS.get(x, ())) >= need:
                    break
                if slots.fits(lineup, c):
                    lineup.append(c)
            pool = [c for c in CHAR_BONDS if c not in lineup]
            while len(lineup) < MAX_LINEUP:
                scored = []
                for c in pool:
                    if not slots.fits(lineup, c):
                        continue
                    gain = 0
                    for b in CHAR_BONDS[c]:
                        if b in pending:
                            have = sum(1 for x in lineup if b in CHAR_BONDS.get(x, ()))
                            if have < pending[b]:
                                gain += 100 if have + 1 >= pending[b] else 1
                    if gain > 0:
                        scored.append((gain, len(CHAR_BONDS[c]), c))
                if not scored:
                    break
                scored.sort(reverse=True)
                pick = scored[0][2]
                lineup.append(pick)
                pool.remove(pick)
            covered = tuple(sorted(
                b for b, need2 in pending.items()
                if sum(1 for x in lineup if b in CHAR_BONDS.get(x, ())) >= need2))
            if target not in covered:   # 主目标自己都没凑齐，这局白跑
                continue
            # 同分时按羁绊名字典序取较大者，保证结果与迭代顺序无关、可复现
            score = (len(covered), -len(lineup), -need, target)
            if best is None or score > best[0]:
                best = (score, target, lineup, covered)
        if best is None:
            break
        _, target, lineup, covered = best
        runs.append(RunPlan(target, pending[target], tuple(lineup), covered))
        for b in covered:
            pending.pop(b, None)
        if len(runs) >= max_runs:
            break
    return runs


def next_run(tasks: Sequence[BondTask]) -> Optional[RunPlan]:
    """下一局要执行的计划：分局表的第一局（不考虑已经有哪几局做过）。"""
    runs = plan_runs(tasks)
    return runs[0] if runs else None


def next_pending_run(runs: Sequence[RunPlan],
                     done_targets: Iterable[str]) -> Optional[RunPlan]:
    """分局表里第一个「主目标还没做过」的局。

    没有它，策略每局都会拿第一局，永远推进不到第 2 局之后。
    """
    done = {str(x) for x in done_targets}
    for run in runs:
        if run.target not in done:
            return run
    return None


# ---------------------------------------------------------------- 进度文件

def load_progress(path: Optional[str] = PROGRESS_FILE) -> List[str]:
    """读已做过的主目标列表；文件不存在或坏了都当空进度，不让策略起不来。"""
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        log.warning("读不了进度文件 %s，按空进度处理", path)
        return []
    done = data.get("done_targets") if isinstance(data, dict) else None
    return [str(x) for x in (done or [])]


def save_progress(done_targets: Iterable[str], path: Optional[str] = PROGRESS_FILE) -> None:
    if not path:
        return
    data = {"done_targets": sorted({str(x) for x in done_targets}),
            "updated_by": "currency_wars_bond"}
    tmp = "%s.tmp" % path
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        log.warning("进度文件写不进去：%s", path)


def tasks_with_progress(tasks: Sequence[BondTask], done_targets: Iterable[str]) -> List[BondTask]:
    """把已经做过主目标的羁绊标成完成，让分局表自然跳过它们。"""
    done = {str(x) for x in done_targets}
    return [BondTask(t.bond, t.target, done=t.done or t.bond in done) for t in tasks]


def run_order(run: RunPlan, owned: Iterable[str]) -> List[str]:
    """本局买人顺序：主目标成员最先，其次顺带羁绊成员，最后才是凑格子的填充位。

    只按费用排序会让便宜的单羁绊填充角色插到主目标前面，钱一紧张主目标永远买不进；
    同一档内仍按费用从低到高，便宜的先到手以减少刷新。
    """
    owned_keys = {normalize(x) for x in owned if x}
    missing = [k for k in run.lineup if k not in owned_keys]

    def rank(key: str):
        bonds = CHAR_BONDS.get(key, set())
        if run.target in bonds:
            tier = 0
        elif any(b in run.covered for b in bonds):
            tier = 1
        else:
            tier = 2
        return (tier, cost_of(key) is None, cost_of(key) or 0, key)

    missing.sort(key=rank)
    return [display_name(k) for k in missing]


def _stage_key(stage: Optional[str]):
    """把 "3-7" 解析成可比较的元组，认不出就返回 None。"""
    try:
        major, _, minor = (stage or "").partition("-")
        return int(major), int(minor)
    except (TypeError, ValueError):
        return None


def _is_late_stage(stage: Optional[str]) -> bool:
    """是不是「后期」——最后一两关买不进人基本就没机会了，得敢卖占位的换钱。"""
    key = _stage_key(stage)
    return key is not None and key >= LATE_STAGE_MIN


def _star_counts(ui) -> dict:
    """本局每个角色为升星多买了几张；挂在宿主实例上，一局内有效。"""
    counts = getattr(ui, "_bond_star_counts", None)
    if not isinstance(counts, dict):
        counts = {}
        try:
            ui._bond_star_counts = counts
        except AttributeError:  # pragma: no cover - 假 ui 不允许挂属性
            pass
    return counts


def _front_names(ui) -> List[str]:
    """前台正在出战的角色名——升星只对他们做，后台和备战席先不烧这份钱。"""
    return [c.name for c in (getattr(ui, "forward_characters", None) or [])
            if getattr(c, "name", None)]


def _board_bond_counts(ui, bonds) -> dict:
    """场上（前台＋后台）每个羁绊已经站了几个人。

    任务条件看的是这个，不是「买过几张卡」：备战席不上场不贡献羁绊，
    而买过但被挤到备战席的人也确实没在算人数。
    """
    bonds = set(bonds)
    counts = {}
    if not bonds:
        return counts
    for zone in ("forward_characters", "backward_characters"):
        for char in getattr(ui, zone, None) or []:
            name = getattr(char, "name", None)
            if not name:
                continue
            for bond in CHAR_BONDS.get(normalize(name), ()):
                if bond in bonds:
                    counts[bond] = counts.get(bond, 0) + 1
    return counts


def _bond_team_members(ui, bonds) -> List[str]:
    """当前队伍里属于本局羁绊的角色（前台、后台、备战席都算）。

    摆位要把这些人推到前台：羁绊人数虽然三个区都算，但技能和升星只看前台。
    1 费的桑博按费用排永远排不过黄泉、彦卿这些高费无关角色，买了也是冷板凳。
    """
    bonds = set(bonds)
    if not bonds:
        return []
    found: List[str] = []
    seen = set()
    for zone in ("forward_characters", "backward_characters", "prepare_characters"):
        for char in getattr(ui, zone, None) or []:
            name = getattr(char, "name", None)
            if not name:
                continue
            key = normalize(name)
            if key in seen or not (CHAR_BONDS.get(key, set()) & bonds):
                continue
            seen.add(key)
            found.append(display_name(key))
    return found


def _reset_stars_on_new_run(ui, stage: Optional[str]) -> None:
    """阶段号回退说明开了新一局，把上一局的升星计数清掉。"""
    key_now = _stage_key(stage)
    key_prev = _stage_key(getattr(ui, "_bond_last_stage", "") or "")
    if key_now and key_prev and key_now < key_prev:
        _star_counts(ui).clear()
    if key_now:
        try:
            ui._bond_last_stage = stage
        except AttributeError:  # pragma: no cover - 假 ui 不允许挂属性
            pass


def missing_cheapest_cost(ui, order: Sequence[str]) -> Optional[int]:
    """还缺的人里最便宜那张要多少金币；全到手了返回 None。"""
    costs = []
    for name in order:
        if ui.has_tracked_character(name):
            continue
        cost = cost_of(normalize(name))
        if cost is not None:
            costs.append(cost)
    return min(costs) if costs else None


# ---------------------------------------------------------------- 界面胶水

def install_costs(ui) -> int:
    """把官方费用表补进实例（原有 13 条不动），返回新增条数。

    预设策略只登记了自己关心的角色，凑其它羁绊时必须补齐整本费用表，
    否则 `can_afford_character` 永远返回 False。CurrencyWars 的费用表按
    **游戏内显示名**索引（`_normalize_tracked_character_name` 只去空白），
    所以这里同时登记显示名与归一 key 两种写法。「开拓者·记忆」不登记，
    它走实例上的 remembrance 名字与固定费用。
    """
    table = getattr(ui, "character_purchase_costs", None)
    if table is None:
        return 0
    added = 0
    for key, (name, cost, _pos) in STAFF.items():
        if key == REMEMBRANCE_KEY:
            continue
        for alias in {name, key}:
            if alias not in table:
                table[alias] = cost
                added += 1
    return added


def current_owned(ui) -> List[str]:
    """当前已拥有的角色名（用 currency_wars.py 自己的 tracked 标记）。"""
    flags = getattr(ui, "character_flags", {}) or {}
    return [name for name, flag in flags.items() if flag]


def _refresh_shop_scan(ui) -> None:
    """重新识别商店五张卡，让随后的按名字购买能看到当前这一屏。

    `try_buy_character` 内部是 `click_element(name, "text", need_ocr=False)`：
    它直接复用上一次 OCR 的结果，自己不再截图。上游 `buy_aglaea` 因此每次购买/刷新
    后都会用 `_get_ocr_texts_in_crop(shop_crop)` 重新扫一遍；漏掉这一步就会拿着上一屏
    的坐标去点新一屏 —— 点击要么落空、要么点到别的角色身上。
    """
    try:
        if hasattr(ui, "_get_ocr_texts_in_crop"):
            ui._get_ocr_texts_in_crop(SHOP_CROP)
            return
        if auto is not None and hasattr(auto, "perform_ocr"):
            auto.take_screenshot(SHOP_CROP)
            auto.perform_ocr()
    except Exception as e:  # 画面未就绪/离线环境：这一轮按「什么都没看到」继续，下一轮会重试
        log.debug("商店识别失败，本轮跳过：%s", e)


def _shop_texts() -> List[str]:
    """当前这一屏商店 OCR 出来的文本。"""
    result = getattr(auto, "ocr_result", None) or []
    texts = []
    for item in result:
        try:
            payload = item[1]
        except (TypeError, IndexError):
            continue
        if isinstance(payload, (tuple, list)) and payload:
            payload = payload[0]
        if isinstance(payload, str) and payload.strip():
            texts.append(payload.strip())
    return texts


def _shop_bond_members(keep_bonds, exclude) -> List[str]:
    """商店里出现、属于本局羁绊、且不在排除名单里的角色（显示名）。

    名单是离线规划的结果，但商店刷出谁没法预先知道：名单里挑的是符玄（4 费），
    这局先刷出灵砂（2 费治疗）——两个人都能凑「治疗 4」，死等符玄就是白等。
    """
    bonds = set(keep_bonds)
    if not bonds:
        return []
    found = []
    for text in _shop_texts():
        key = normalize(text)
        if not key or key in exclude:
            continue
        member_of = CHAR_BONDS.get(key)
        if member_of and (member_of & bonds):
            found.append(display_name(key))
    return found


def buy_members(ui, names: Sequence[str], keep_bonds=(), max_refresh: int = 6,
                star_targets: Sequence[str] = ()) -> int:
    """打开商店，按给定顺序买人；买不动时刷新，返回买到的个数。

    复用 CurrencyWars 自己的 check_money / can_afford_character / try_buy_character /
    spend_character_purchase_cost，因此费用、OCR、点击全走既有实现。
    除名单里的人，也会顺手买走商店里出现的「同一羁绊」成员（同羁绊人数只增不减）。

    `star_targets` 点名要继续买到三星的角色（前台那些）。原本「已拥有就跳过」让
    每个人只买一张、永远一星，羁绊凑满也打不过——实机 2026-10-23 3-7 就是这样输的。
    """
    if not names and not star_targets:
        return 0
    if auto is None:  # 单测/离线环境没有自动化通道，什么也买不了
        return 0
    shop_ui = getattr(ui, "auto", None)  # 宿主实例自带 auto；测试假商店用自带的哑通道
    click, press = (shop_ui or auto).click_element, (shop_ui or auto).press_key
    shop_button_crop = (1591 / 1920, 958 / 1080, 66 / 1920, 46 / 1080)
    click(shop_button_crop, "crop")
    time.sleep(2)
    money = ui.check_money()
    stars = _star_counts(ui)
    base = list(names)
    # 已经到手、但还没升满星的前台角色也要留在候选里——否则「已拥有就跳过」让全队永远一星。
    # 三星的就不再挂进来了：候选表是每轮扫描的对象，挂满了也没用，只会白占位置。
    known = {normalize(n) for n in base}
    for n in star_targets:
        key = normalize(n)
        if not key or key in known:
            continue
        if ui.has_tracked_character(n) and stars.get(key, 0) >= MAX_STAR_BUYS:
            continue
        base.append(n)
        known.add(key)
    exclude = {normalize(n) for n in base}
    star_keys = {normalize(n) for n in star_targets if normalize(n)}
    extras: List[str] = []
    bought, refresh_left = 0, max_refresh
    scan_dirty = True  # 刚打开商店，这一屏还没识别

    def still_wanted(name: str) -> bool:
        """这个人还值得买吗：没到手，或者被点名升星但还没升满。"""
        if not ui.has_tracked_character(name):
            return True
        key = normalize(name)
        return key in star_keys and stars.get(key, 0) < MAX_STAR_BUYS

    try:
        while True:
            if scan_dirty:
                _refresh_shop_scan(ui)
                scan_dirty = False
                extras = _shop_bond_members(keep_bonds, exclude)
            if not any(still_wanted(name) for name in base):
                break  # 名单上的人都到手（且该升星的升满了），不再刷新浪费钱
            progress = False
            for name in base + extras:
                if not still_wanted(name):
                    continue
                owned_before = ui.has_tracked_character(name)
                if not ui.can_afford_character(money, name):
                    continue
                if ui.try_buy_character(name):
                    money = ui.spend_character_purchase_cost(money, name)
                    bought += 1
                    progress = True
                    scan_dirty = True  # 买走一张，位置变了，重新识别
                    if owned_before:  # 买的是同名卡，算一次升星
                        key = normalize(name)
                        stars[key] = stars.get(key, 0) + 1
                    break  # 重新从头按优先级扫，避免重复买同一个人
            if progress:
                continue
            if refresh_left > 0 and money >= MIN_MONEY_TO_REFRESH:
                press("d")
                money -= REFRESH_COST
                refresh_left -= 1
                scan_dirty = True
                time.sleep(1)  # 等刷新动画，否则截到的是旧一屏
                continue
            break
    finally:
        click(shop_button_crop, "crop")
    return bought


def _is_keepable(name: str, keep, bonds) -> bool:
    """这个角色要不要留：在本局名单里，或属于本局要凑的任何羁绊。"""
    key = normalize(name)
    if key in keep:
        return True
    return bool(bonds and (CHAR_BONDS.get(key, set()) & bonds))


def sell_offlineup(ui, lineup_keys, keep_bonds=(), allow_battlefield: bool = True,
                   force_battlefield: bool = False) -> int:
    """卖掉用不上的角色，给目标成员腾位置、顺便换回金币。

    宿主原策略靠 `sell_characters()` 清备战席，但它只看「前台/后台格子是否满」，
    不认本局阵容；bond_chain 因此一直完全禁用卖人，结果备战席一满就再也买不进人
    （实机 2026-10-23 3-2 卡死三轮：画面提示「备战席已满，请出售角色或提升等级」，
    货币只剩 2，目标还差 2 人）。

    两步，风险从低到高：
    1. 先清**备战席**——备战席不计入羁绊人数，卖了零风险；
    2. 备战席没得卖了、场上却还站着用不上的人时，再卖**一个**场上的。
       场上每个人都是出站人头，但这里只挑「不属于本局任何目标羁绊」的，所以
       羁绊人数不会掉；一次只卖一个，卖完让 `deploy_and_optimize` 补位，
       避免一口气把阵容拆散、把这一局打崩。
    """
    prepare = getattr(ui, "prepare_characters", None) or []
    keep = {normalize(k) for k in lineup_keys}
    bonds = set(keep_bonds)
    flags = getattr(ui, "character_flags", None)
    sold = 0

    for idx, char in enumerate(prepare):
        name = getattr(char, "name", None)
        if not name or _is_keepable(name, keep, bonds):
            continue
        try:
            ok = ui._sell_single_character(idx)
        except Exception as e:  # 卖人接口缺失/点歪：跳过这一个，不影响其余步骤
            log.debug("出售 %s 失败：%s", name, e)
            continue
        if not ok:
            continue
        prepare[idx] = type(char)(None, None)
        if isinstance(flags, dict):  # 卖掉的人不能再算「已拥有」，否则「还缺 N」永远不减
            flags.pop(name, None)
        sold += 1
        log.info("卖掉备战席的 %s（用不上），腾位置换金币", name)

    if sold or not allow_battlefield:
        return sold
    # 备战席还有空位就不必动场上的人——但「后期钱不够、专门来换钱」这一路是例外，
    # 那时备战席空着也买不进人（缺的是金币不是格子），所以 force_battlefield 直接下场
    if not force_battlefield and (not prepare or any(not getattr(c, "name", None) for c in prepare)):
        return 0
    return _sell_one_battlefield(ui, keep, bonds)


def _sell_one_battlefield(ui, keep, bonds) -> int:
    """卖掉场上一个不属于本局羁绊的角色；找不到可卖的就返回 0。

    按费用从高到低挑：同样用不上，先卖贵的换钱（5 费角色卖掉能换回 5 金币）。
    """
    groups = (
        (getattr(ui, "forward_characters", None), getattr(ui, "forward_pos", None), "前台"),
        (getattr(ui, "backward_characters", None), getattr(ui, "backward_pos", None), "后台"),
    )
    flags = getattr(ui, "character_flags", None)
    candidates = []
    for characters, positions, zone in groups:
        if not characters or not positions:
            continue
        for idx, char in enumerate(list(characters)):
            name = getattr(char, "name", None)
            if not name or _is_keepable(name, keep, bonds):
                continue
            candidates.append((cost_of(normalize(name)) or 0, characters, positions, idx, char, name, zone))
    if not candidates:
        return 0
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _cost, characters, positions, idx, char, name, zone in candidates:
        try:
            ok = ui._sell_single_character(idx, positions)
        except Exception as e:
            log.debug("出售%s的 %s 失败：%s", zone, name, e)
            continue
        if not ok:
            continue
        characters[idx] = type(char)(None, None)
        if isinstance(flags, dict):
            flags.pop(name, None)
        log.info("卖掉%s的 %s（不属于本局羁绊），腾格子", zone, name)
        return 1
    return 0


def bond_chain_step(ui, progress_path: Optional[str] = PROGRESS_FILE) -> Optional[RunPlan]:
    """check_main_screen 里的一步：读任务 → 取分局表里第一个没做过的局 → 买齐阵容。

    分局表由 `plan_runs()` 从未完成任务算出，一局可以同时完成多条任务
    （一个角色同时属于多个羁绊），所以整局数与离线方案一致，而不是一局一条。
    已做过的主目标记在 `bond_chain_state.json`，否则每局都会重复第一局。
    """
    install_costs(ui)
    if hasattr(ui, "character_info_cache"):
        # 宿主读名走 OCR，个别角色常被误读；把修正函数挂到实例上供其调用
        ui.fix_ocr_name = fix_ocr_name
    _reset_stars_on_new_run(ui, getattr(ui, "current_stage", "") or "")
    remembrance = (ui.get_remembrance_trailblazer_name()
                   if hasattr(ui, "get_remembrance_trailblazer_name") else None)
    tasks = getattr(ui, "bond_tasks", None) or default_tasks()
    done_targets = load_progress(progress_path)
    if getattr(ui, "bond_tasks", None):
        # 游戏里读到的真实任务清单自带完成态，优先用它
        done_targets = [t.bond for t in tasks if t.done] or done_targets
    tasks = tasks_with_progress(tasks, done_targets)
    owned = current_owned(ui)
    emblems = getattr(ui, "bond_emblems", None) or {}
    runs = plan_runs(tasks, emblems=emblems)
    run = next_pending_run(runs, done_targets)
    if run is None:
        log.info("羁绊任务已全部完成")
        return None
    order = run_order(run, owned)
    # 让宿主的选择逻辑知道本局主目标：投资环境会优先拿该羁绊的概念股/邀请/星徽
    try:
        ui.bond_target = run.target
    except AttributeError:  # pragma: no cover - 假 ui 只读属性
        pass
    target_bonds = set(run.covered) | {run.target}
    fronts = _front_names(ui)
    # 任务条件看场上（前台＋后台）各羁绊站了几个人，不是「名单买齐没有」：
    # 买过卡却挤在备战席的人不贡献人数，名单外补买的灵砂反而在算人数。
    bonds_in_play = sorted(set(run.covered) | {run.target})
    counts = _board_bond_counts(ui, target_bonds)
    need_map = {t.bond: t.target for t in tasks}
    emblems_now = getattr(ui, "bond_emblems", None) or {}

    def need_of(bond: str) -> int:
        base = need_map.get(bond, 0)
        return max(base - int(emblems_now.get(bond, 0) or 0), 1)

    gap = {b: max(need_of(b) - counts.get(b, 0), 0) for b in bonds_in_play}
    all_met = all(v == 0 for v in gap.values())
    log.info("本局目标 %s｜场上 %s%s｜%s",
             run,
             "、".join("%s %d/%d" % (b, counts.get(b, 0), need_of(b)) for b in bonds_in_play),
             "" if remembrance is None else "｜记忆开拓者名字：%s" % remembrance,
             "已达标，只加经验/升星" if all_met else
             "还差 " + "、".join("%s %d 人" % (b, v) for b, v in gap.items() if v))
    if order and not all_met:
        # 备战席满了就买不进人：先清掉用不上的（只动该动的，见 sell_offlineup）
        sell_offlineup(ui, run.lineup, keep_bonds=target_bonds)
        # 后期光靠准备席腾位置不够：备战席明明有空位、钱却差一口气时，
        # 把场上不贡献羁绊的占位角色卖掉换金币（不这么做会一直干等到时间耗尽）
        stage = getattr(ui, "current_stage", "") or ""
        if _is_late_stage(stage):
            cheapest = missing_cheapest_cost(ui, order)
            money = ui.check_money() if hasattr(ui, "check_money") else None
            if cheapest is not None and money is not None and money < cheapest:
                if sell_offlineup(ui, run.lineup, keep_bonds=target_bonds, allow_battlefield=True,
                                  force_battlefield=True):
                    log.info("后期钱不够（%s < %s），卖掉场上占位角色换金币", money, cheapest)
        # 名单之外、但属于本局羁绊的角色也一起买（商店刷出灵砂时不该死等符玄）
        # 前台角色额外买到三星：光凑人数、全队一星，羁绊满了也打不过
        buy_members(ui, order, keep_bonds=target_bonds, star_targets=fronts)
        # 买完必须重扫一次队伍：新买的人要先解析进 forward/backward/prepare，
        # 后面的 deploy_and_optimize 才会把他们摆上场（宿主 buy_aglaea/buy_seele 同样这么做）
        if hasattr(ui, "check_character_status"):
            ui.check_character_status()
    elif fronts:
        # 阵容已经齐了，但前台还没升满星——这时候不再挑人，只补同名卡。
        # 实机 2026-10-23 3-7 就是阵齐、全队一星、羁绊也满了，硬是打不过。
        stars = _star_counts(ui)
        need_star = [n for n in fronts if stars.get(normalize(n), 0) < MAX_STAR_BUYS]
        if need_star:
            buy_members(ui, [], keep_bonds=target_bonds, max_refresh=2, star_targets=need_star)
            if hasattr(ui, "check_character_status"):
                ui.check_character_status()
    # 摆位：把本局羁绊成员推到前台。deploy_and_optimize 在 check_main_screen 里
    # 紧接着就调，默认只按费用排前台，桑博这类 1 费目标角色永远排不进去。
    try:
        ui.bond_push_forward = _bond_team_members(ui, target_bonds)
    except AttributeError:  # pragma: no cover - 假 ui 不允许挂属性
        pass
    # 本局羁绊凑齐了就先挂起，等对局结算时按输赢决定记不记（见 commit_bond_result）。
    # 这里不能直接落盘：凑齐人 ≠ 通关成功，输了这局还得重来。
    try:
        ui._bond_pending = list(bonds_in_play) if all_met else None
    except AttributeError:  # pragma: no cover - 假 ui 不允许挂属性
        pass
    return run


def commit_bond_result(ui, win: bool, progress_path: Optional[str] = PROGRESS_FILE) -> None:
    """对局结束才落盘：只有通关成功、且当时场上羁绊已凑够人，才算这些羁绊完成。

    由 `currency_wars.loop()` 在拿到结算结果后调用——策略模块自己看不到结算界面。
    """
    pending = getattr(ui, "_bond_pending", None)
    if not pending:
        return
    try:
        ui._bond_pending = None
    except AttributeError:  # pragma: no cover
        pass
    if not win:
        log.info("本局羁绊已凑齐但没通关（失败/放弃），进度不记录，下一局重来")
        return
    done = load_progress(progress_path)
    newly = [b for b in pending if b not in done]
    if not newly:
        return
    save_progress(list(done) + newly, progress_path)
    log.info("本局通关，记录羁绊完成：%s｜下次换下一局", "、".join(newly))
