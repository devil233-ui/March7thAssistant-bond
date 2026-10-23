# -*- coding: utf-8 -*-
"""羁绊链路策略（bond_chain）的单测：只测决策与买人顺序，不需要游戏、Qt 或 OCR。"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tasks.weekly import currency_wars_bond as m


class ShopFake:
    """假商店：模拟 CurrencyWars 的买人接口与刷新行为。"""

    def __init__(self, money=10, slots=(), refresh_slots=None):
        self.money = money
        self.slots = list(slots)
        self.refresh_slots = list(refresh_slots or [])
        self.character_flags = {}
        self.character_purchase_costs = dict(m.STAFF_ORIGINAL_COSTS) if hasattr(m, "STAFF_ORIGINAL_COSTS") else {}
        self.bought = []
        self.refreshes = 0
        self.scans = 0  # 商店重新识别的次数（try_buy_character 依赖它拿最新一屏）
        self.auto = self._Auto(self)

    class _Auto:
        def __init__(self, outer):
            self.outer = outer
            self.keys = []

        def click_element(self, target, kind=None, *a, **kw):
            return True

        def press_key(self, key):
            self.keys.append(key)
            outer = self.outer
            outer.refreshes += 1
            if outer.refresh_slots:
                outer.slots = list(outer.refresh_slots.pop(0))
            return True

    # --- 与 CurrencyWars 同名同义的接口 ---
    def check_money(self):
        return self.money

    def has_tracked_character(self, name):
        return self.character_flags.get(name, False)

    def get_character_purchase_cost(self, name):
        return self.character_purchase_costs.get(name)

    def can_afford_character(self, money, name):
        cost = self.get_character_purchase_cost(name)
        return cost is not None and money >= cost

    def spend_character_purchase_cost(self, money, name):
        cost = self.get_character_purchase_cost(name)
        return money if cost is None else money - cost

    def try_buy_character(self, name, wait_time=1.0):
        if name not in self.slots:
            return False
        self.slots.remove(name)
        self.character_flags[name] = True
        self.bought.append(name)
        return True

    def get_remembrance_trailblazer_name(self):
        return None

    def _get_ocr_texts_in_crop(self, crop):
        """模拟宿主重新识别商店五张卡（真机上 try_buy_character 靠它拿最新一屏）。"""
        self.scans += 1
        return list(self.slots)

    def check_character_status(self):
        """宿主买完人会重扫队伍；这里只记次数，供断言用。"""
        self.status_checks = getattr(self, "status_checks", 0) + 1
        return True


class TestData(unittest.TestCase):
    def test_every_bond_member_exists_in_staff(self):
        for bond, (_kind, _tiers, members) in m.BONDS.items():
            for key in members:
                self.assertIn(key, m.STAFF, "%s 的成员 %s 不在员工表" % (bond, key))

    def test_every_task_bond_can_reach_its_target(self):
        for bond, tiers in m.TASK_TIERS.items():
            target = max(tiers)
            members = m.bond_members(bond)
            self.assertGreaterEqual(len(members), target,
                                    "%s 只有 %d 名成员，凑不满 %d 档" % (bond, len(members), target))

    def test_huahuo_and_huohua_are_different_units(self):
        # 官方图鉴里「花火」「火花」是两个不同员工，不能混成一个
        self.assertIn("花火", m.STAFF)
        self.assertIn("火花", m.STAFF)
        self.assertIn("战技点", m.bonds_of("花火"))
        self.assertNotIn("星间旅人", m.bonds_of("花火"))
        self.assertIn("星间旅人", m.bonds_of("火花"))

    def test_cost_table_covers_every_staff(self):
        for key, (name, cost, _pos) in m.STAFF.items():
            self.assertIn(cost, (1, 2, 3, 4, 5), "%s 费用异常" % name)


class TestDecision(unittest.TestCase):
    def test_empty_roster_picks_cheapest_bond(self):
        cand = m.choose(m.default_tasks(), [])
        self.assertIsNotNone(cand)
        self.assertEqual(cand.bond, "持续伤害")
        self.assertEqual(cand.owned, 0)
        self.assertEqual(cand.need, 4)
        self.assertEqual(cand.cost, 5)

    def test_owned_members_reduce_need(self):
        cand = m.choose(m.default_tasks(), ["桑博", "椒丘"])
        self.assertEqual(cand.bond, "持续伤害")
        self.assertEqual(cand.owned, 2)
        self.assertEqual(cand.need, 2)
        self.assertEqual(len(cand.keys), 2)

    def test_full_lineup_needs_nothing(self):
        members = m.bond_members("持续伤害")
        cand = m.choose([m.BondTask("持续伤害", 4)], members)
        self.assertTrue(cand.finished)
        self.assertEqual(cand.need, 0)
        self.assertEqual(m.purchase_order(members, cand), [])

    def test_high_tier_bond_skipped_when_target_unreachable(self):
        # 任务档位高于官方成员数时不应产出候选
        cands = m.candidates([m.BondTask("公司", 9)], [])
        self.assertEqual(cands, [])

    def test_purchase_order_is_cheapest_first_and_only_bond_members(self):
        cand = m.choose([m.BondTask("持续伤害", 4)], [])
        order = m.purchase_order([], cand)
        self.assertEqual(len(order), 4)
        for name in order:
            self.assertIn(m.normalize(name), m.bond_members("持续伤害"))
        costs = [m.cost_of(m.normalize(n)) for n in order]
        self.assertEqual(costs, sorted(costs))

    def test_parse_tasks_takes_highest_tier(self):
        tasks = m.parse_tasks([
            {"bond": "夜之半神", "target": 4, "done": False},
            {"bond": "夜之半神", "target": 7, "done": False},
            {"bond": "护盾", "target": 4, "done": True},
        ])
        got = {t.bond: t for t in tasks}
        self.assertEqual(got["夜之半神"].target, 7)
        self.assertFalse(got["夜之半神"].done)
        self.assertTrue(got["护盾"].done)
        # 已完成的任务不再产出候选
        self.assertNotIn("护盾", [c.bond for c in m.candidates(tasks, [])])


class _MuteAuto:
    """单测用的哑自动化通道：click/press 什么都不做，让商店胶水不走真窗口。"""

    def click_element(self, *a, **k):
        return True

    def press_key(self, *a, **k):
        return True


class TestUiGlue(unittest.TestCase):
    def setUp(self):
        # buy_members 里为了等界面切换会 sleep(2)，单测里没必要真等
        patcher = patch.object(m.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 有 module.automation 的环境里，商店胶水会摸真窗口；单测里塞个不摸窗口的哑通道
        self._old_auto = getattr(m, "auto", None)
        m.auto = _MuteAuto()
        self.addCleanup(setattr, m, "auto", self._old_auto)

    def test_install_costs_keeps_existing_entries_and_skips_remembrance(self):
        ui = ShopFake(money=10)
        ui.character_purchase_costs = {"阿格莱雅": 1}
        added = m.install_costs(ui)
        self.assertGreater(added, 60)
        self.assertEqual(ui.character_purchase_costs["阿格莱雅"], 1)   # 原值不被覆盖
        self.assertEqual(ui.character_purchase_costs["瓦尔特"], 5)
        self.assertNotIn("开拓者·记忆", ui.character_purchase_costs)     # 走 remembrance 名字

    def test_buy_members_buys_in_priority_order(self):
        ui = ShopFake(money=10, slots=["椒丘", "桑博", "艾丝妲", "卡芙卡"])
        m.install_costs(ui)
        got = m.buy_members(ui, ["桑博", "椒丘", "艾丝妲", "卡芙卡"])
        self.assertEqual(got, 4)
        self.assertEqual(ui.bought, ["桑博", "椒丘", "艾丝妲", "卡芙卡"])  # 按传入优先级
        self.assertEqual(ui.refreshes, 0)  # 目标都在店里，不必刷新

    def test_buy_members_refreshes_when_targets_absent(self):
        ui = ShopFake(money=12, slots=["白厄"], refresh_slots=[["桑博", "椒丘"]])
        m.install_costs(ui)
        got = m.buy_members(ui, ["桑博", "椒丘"], max_refresh=2)
        self.assertEqual(got, 2)
        self.assertEqual(ui.refreshes, 1)

    def test_buy_members_stops_when_broke(self):
        ui = ShopFake(money=1, slots=["瓦尔特"])  # 5 费买不起，钱也不够刷新
        m.install_costs(ui)
        got = m.buy_members(ui, ["瓦尔特"], max_refresh=3)
        self.assertEqual(got, 0)
        self.assertEqual(ui.refreshes, 0)

    def test_buy_members_rescans_shop_after_every_refresh(self):
        """刷新/买走一张后必须重新识别商店，否则按名字点到的是上一屏的坐标。

        实机 2026-10-23 那局「尝试购买」日志一条都没有、47 分钟没买进一个人，
        根因就是这里漏掉了上游 buy_aglaea 每轮都做的 _get_ocr_texts_in_crop：
        try_buy_character 用 need_ocr=False，只认上一次 OCR 的结果。
        """
        ui = ShopFake(money=20, slots=["白厄"], refresh_slots=[["桑博"], ["椒丘"]])
        m.install_costs(ui)
        got = m.buy_members(ui, ["桑博", "椒丘"], max_refresh=3)
        self.assertEqual(got, 2)
        self.assertEqual(ui.refreshes, 2)
        self.assertGreaterEqual(ui.scans, 1 + ui.refreshes, "刷新后没有重新识别商店")

    def test_bond_chain_step_returns_run_plan_and_buys_its_lineup(self):
        # 首局按分局表是「贝洛伯格 4 ＋ 顺带治疗 4」，阵容 10 人
        ui = ShopFake(money=20, slots=["桑博", "娜塔莎", "杰帕德", "布洛妮娅"])
        ui.character_purchase_costs = {}
        with tempfile.TemporaryDirectory() as tmp:
            run = m.bond_chain_step(ui, progress_path=os.path.join(tmp, "p.json"))
        self.assertEqual(run.target, "贝洛伯格")
        self.assertIn("治疗", run.covered)
        self.assertEqual(len(ui.bought), 4)
        for name in ui.bought:
            self.assertIn(m.normalize(name), run.lineup)
        # 买完必须重扫队伍，否则本局买的成员进不了上阵位、羁绊不会激活
        self.assertEqual(getattr(ui, "status_checks", 0), 1)

    def test_bond_chain_step_uses_runtime_tasks(self):
        ui = ShopFake(money=20, slots=["三月七", "砂金", "杰帕德", "丹恒·腾荒"])
        ui.character_purchase_costs = {}
        ui.bond_tasks = [m.BondTask("护盾", 4), m.BondTask("持续伤害", 4, done=True)]
        with tempfile.TemporaryDirectory() as tmp:
            run = m.bond_chain_step(ui, progress_path=os.path.join(tmp, "p.json"))
        self.assertEqual(run.target, "护盾")
        self.assertNotIn("持续伤害", run.covered)


class TestSellOfflineup(unittest.TestCase):
    """备战席满了必须能自己清位置，否则买到一半就卡死（实机 2026-10-23 3-2 三轮没动）。"""

    class _Char:
        def __init__(self, name=None, pos=None):
            self.name = name
            self.pos = pos

    def _ui(self, names):
        ui = ShopFake(money=2)
        ui.prepare_characters = [self._Char(n) for n in names]
        ui.sold = []

        def _sell(idx, pos_list=None):
            ui.sold.append(ui.prepare_characters[idx].name)
            return True

        ui._sell_single_character = _sell
        for name in names:
            ui.character_flags[name] = True
        return ui

    def test_only_sells_members_outside_lineup(self):
        ui = self._ui(["阿格莱雅", "桑博", "黄泉", "娜塔莎"])
        sold = m.sell_offlineup(ui, ["桑博", "娜塔莎", "杰帕德"])
        self.assertEqual(sold, 2)
        self.assertEqual(ui.sold, ["阿格莱雅", "黄泉"], "只该卖阵容外的，阵容内的留着凑羁绊")
        self.assertFalse(ui.character_flags.get("阿格莱雅", False), "卖掉的人不能还算已拥有")
        self.assertTrue(ui.character_flags.get("桑博"), "阵容成员仍算已拥有")

    def test_sold_slot_is_cleared(self):
        ui = self._ui(["阿格莱雅", "桑博"])
        m.sell_offlineup(ui, ["桑博"])
        self.assertIsNone(ui.prepare_characters[0].name, "卖掉的格子要清空，后续才能填新人")
        self.assertEqual(ui.prepare_characters[1].name, "桑博")

    def test_keeps_any_member_of_target_bonds(self):
        """实际买到的可能是「希儿」而不是计划里的「桑博」，她照样算贝洛伯格人头，不能卖。"""
        ui = self._ui(["希儿", "黄泉"])
        sold = m.sell_offlineup(ui, ["桑博", "娜塔莎"], keep_bonds={"贝洛伯格", "治疗"})
        self.assertEqual(sold, 1)
        self.assertEqual(ui.sold, ["黄泉"], "希儿属于贝洛伯格，卖了羁绊人数就掉了")
        self.assertTrue(ui.character_flags.get("希儿"))

    def test_sells_battlefield_when_prepare_full_and_useless(self):
        """备战席全是要留的人、场上却站着用不上的：只卖场上那个，目标成员一个不碰。"""
        ui = ShopFake(money=2)
        ui.prepare_characters = [self._Char("桑博"), self._Char("娜塔莎")]  # 全要留，且没有空位
        ui.forward_characters = [self._Char("阿格莱雅"), self._Char("希儿")]
        ui.forward_pos = ["front0", "front1"]
        ui.backward_characters = []
        ui.backward_pos = []
        ui.sold = []
        ui._sell_single_character = lambda idx, pos_list=None: (ui.sold.append(idx), True)[1]
        for name in ["桑博", "娜塔莎", "阿格莱雅", "希儿"]:
            ui.character_flags[name] = True

        sold = m.sell_offlineup(ui, ["桑博", "娜塔莎"], keep_bonds={"贝洛伯格", "治疗"})
        self.assertEqual(sold, 1)
        self.assertEqual(ui.sold, [0], "只该卖阿格莱雅（前台第 0 格），希儿属于贝洛伯格必须留")
        self.assertFalse(ui.character_flags.get("阿格莱雅", False))
        self.assertTrue(ui.character_flags.get("希儿"))

    def test_does_not_touch_battlefield_when_prepare_has_free_slot(self):
        """备战席还有空位就没必要动场上的人（场上是出站人头，能不碰就不碰）。"""
        ui = ShopFake(money=2)
        ui.prepare_characters = [self._Char("桑博"), self._Char(None)]
        ui.forward_characters = [self._Char("阿格莱雅")]
        ui.forward_pos = ["front0"]
        ui.backward_characters = []
        ui.backward_pos = []
        ui.sold = []
        ui._sell_single_character = lambda idx, pos_list=None: (ui.sold.append(idx), True)[1]

        self.assertEqual(m.sell_offlineup(ui, ["桑博"], keep_bonds={"贝洛伯格"}), 0)
        self.assertEqual(ui.sold, [])

    def test_no_prepare_list_is_noop(self):
        self.assertEqual(m.sell_offlineup(ShopFake(money=2), ["桑博"]), 0)

    def test_sell_failure_keeps_slot(self):
        ui = self._ui(["阿格莱雅"])
        ui._sell_single_character = lambda idx, pos_list=None: False
        self.assertEqual(m.sell_offlineup(ui, []), 0)
        self.assertEqual(ui.prepare_characters[0].name, "阿格莱雅")
        self.assertTrue(ui.character_flags.get("阿格莱雅"), "没卖成不能清标记")


class TestShopBondMembers(unittest.TestCase):
    """商店刷出同羁绊成员时要顺手买：名单里挑的是符玄(4费)，先刷出灵砂(2费)照样能凑治疗。"""

    class _Auto:
        ocr_result = [
            [None, ("灵砂", 0.99)],
            [None, ("阿格莱雅", 0.98)],
            [None, ("桑博", 0.97)],
            [None, ("商店", 0.96)],
        ]

    def setUp(self):
        self._old_auto = m.auto
        m.auto = self._Auto()

    def tearDown(self):
        m.auto = self._old_auto

    def test_finds_bond_member_outside_lineup(self):
        self.assertEqual(m._shop_bond_members({"治疗"}, set()), ["灵砂"])

    def test_skips_roles_outside_target_bonds(self):
        self.assertEqual(m._shop_bond_members({"贝洛伯格"}, set()), ["桑博"])

    def test_respects_exclude(self):
        self.assertEqual(m._shop_bond_members({"治疗"}, {m.normalize("灵砂")}), [])

    def test_no_bonds_returns_empty(self):
        self.assertEqual(m._shop_bond_members(set(), set()), [])


class TestProgress(unittest.TestCase):
    """跨局推进会：做过的主目标要记下来，否则每局都重复第一局。"""

    def test_next_pending_run_skips_done_targets(self):
        runs = m.plan_runs(m.default_tasks())
        self.assertEqual(m.next_pending_run(runs, []).target, runs[0].target)
        self.assertEqual(m.next_pending_run(runs, [runs[0].target]).target, runs[1].target)
        self.assertIsNone(m.next_pending_run(runs, [r.target for r in runs]))

    def test_progress_round_trip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.json")
            self.assertEqual(m.load_progress(path), [])          # 文件不存在 → 空进度
            m.save_progress(["贝洛伯格", "治疗"], path)
            self.assertEqual(sorted(m.load_progress(path)), ["治疗", "贝洛伯格"])
            Path(path).write_text("{ 坏文件", encoding="utf-8")
            self.assertEqual(m.load_progress(path), [])          # 坏了也不让策略起不来

    def test_tasks_with_progress_marks_done(self):
        tasks = m.tasks_with_progress(m.default_tasks(), ["贝洛伯格"])
        by_bond = {t.bond: t for t in tasks}
        self.assertTrue(by_bond["贝洛伯格"].done)
        self.assertFalse(by_bond["能量"].done)

    def test_step_advances_to_second_run_after_winning(self):
        first = m.plan_runs(m.default_tasks())[0]
        names = [m.display_name(k) for k in first.lineup]

        class _Char:
            def __init__(self, n):
                self.name = n

        # 场上已站满本局目标：贝洛伯格 4 人（桑博/布洛妮娅/杰帕德/娜塔莎）
        # 治疗 4 人（娜塔莎/风堇/藿藿/符玄）——任务看的是场上人数，不是买过几张卡
        board = ["桑博", "布洛妮娅", "杰帕德", "娜塔莎", "风堇", "藿藿", "符玄"]

        def _ui(slots):
            ui = ShopFake(money=999, slots=list(slots))
            ui.character_purchase_costs = {}
            ui.forward_characters = [_Char(n) for n in board]
            ui.backward_characters = []
            return ui

        old_auto = getattr(m, "auto", None)
        m.auto = _MuteAuto()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "p.json")
                run1 = m.bond_chain_step(_ui(names), progress_path=path)
                self.assertEqual(run1.target, "贝洛伯格")
                self.assertEqual(m.load_progress(path), [])        # 只是凑齐，还没结算
                m.commit_bond_result(_ui([]), False, progress_path=path)
                self.assertEqual(m.load_progress(path), [])        # 打输了不记，要重来
                ui = _ui(names)
                m.bond_chain_step(ui, progress_path=path)
                m.commit_bond_result(ui, True, progress_path=path)  # 这局赢了
                done = m.load_progress(path)
                self.assertIn("贝洛伯格", done)
                self.assertIn("治疗", done)                        # 顺带完成的也要记
                run2 = m.bond_chain_step(_ui([]), progress_path=path)
                self.assertNotEqual(run2.target, "贝洛伯格")        # 换了下一局
        finally:
            m.auto = old_auto


class TestPlanRuns(unittest.TestCase):
    """分局规划：一局可同时完成多条任务，整局数要压到 14 局以内。"""

    def test_default_tasks_planned_within_fourteen_runs(self):
        runs = m.plan_runs(m.default_tasks())
        covered = set()
        for run in runs:
            covered.update(run.covered)
        self.assertEqual(len(covered), len(m.TASK_TIERS))   # 18 条任务全被覆盖
        self.assertLessEqual(len(runs), 14)                 # 与离线方案同量级（13 局）
        self.assertEqual(len(runs), 13)

    def test_first_run_covers_two_bonds(self):
        runs = m.plan_runs(m.default_tasks())
        self.assertEqual(runs[0].target, "贝洛伯格")
        self.assertEqual(set(runs[0].covered), {"贝洛伯格", "治疗"})
        # 主目标真凑够 4 人
        self.assertGreaterEqual(sum(1 for c in runs[0].lineup
                                    if "贝洛伯格" in m.CHAR_BONDS.get(c, ())), 4)

    def test_every_lineup_fits_slots(self):
        for run in m.plan_runs(m.default_tasks()):
            self.assertLessEqual(len(run.lineup), m.MAX_LINEUP)
            front, back, _both = m.Slots.counts(run.lineup)
            self.assertLessEqual(front, m.FRONT_SLOTS, "%s 前台超编" % run.target)
            self.assertLessEqual(back, m.BACK_SLOTS, "%s 后台超编" % run.target)
            self.assertLessEqual(_both, (m.FRONT_SLOTS - front) + (m.BACK_SLOTS - back),
                                 "%s 灵活角色没格子站" % run.target)

    def test_done_tasks_are_dropped_from_plan(self):
        tasks = [m.BondTask(t.bond, t.target, done=(t.bond == "贝洛伯格"))
                 for t in m.default_tasks()]
        runs = m.plan_runs(tasks)
        covered = {b for run in runs for b in run.covered}
        self.assertNotIn("贝洛伯格", covered)
        self.assertEqual(len(covered), len(m.TASK_TIERS) - 1)

    def test_run_order_puts_target_and_covered_before_fillers(self):
        """买人顺序必须「主目标 → 顺带羁绊 → 填充位」分段。

        只按费用排序时，便宜的单羁绊填充角色会插到主目标前面（实机 2026-10-23 那局
        就是这样：2 费的千冶·刃排在 4 费的杰帕德前面），钱一紧张主目标永远买不进来。
        """
        run = m.plan_runs(m.default_tasks())[0]
        order = [m.normalize(n) for n in m.run_order(run, [])]
        self.assertEqual(len(order), len(run.lineup))
        for key in order:
            self.assertIn(key, run.lineup)

        def tier(key):
            bonds = m.CHAR_BONDS.get(key, set())
            if run.target in bonds:
                return 0
            return 1 if any(b in run.covered for b in bonds) else 2

        tiers = [tier(k) for k in order]
        self.assertEqual(tiers, sorted(tiers), "主目标/顺带/填充必须分段，不能按费用插队")

        for tier_no in {0, 1, 2}:  # 同档内仍按费用从低到高
            costs = [m.cost_of(k) for k, t in zip(order, tiers) if t == tier_no]
            self.assertEqual(costs, sorted(costs), "第 %d 档内没有按费用排序" % tier_no)

    def test_run_order_skips_owned(self):
        run = m.plan_runs(m.default_tasks())[0]
        first = m.run_order(run, [])[0]
        rest = [m.normalize(n) for n in m.run_order(run, [first])]
        self.assertNotIn(m.normalize(first), rest)
        self.assertEqual(len(rest), len(run.lineup) - 1)

    def test_emblem_lets_bond_need_one_less(self):
        """拿到星徽＝该羁绊少凑一人；连「成员数不够」的档位也能靠星徽补齐。"""
        tasks = [m.BondTask("减益", 6)]
        plain = m.plan_runs(tasks)[0]
        self.assertEqual(sum(1 for c in plain.lineup if "减益" in m.CHAR_BONDS.get(c, ())), 6)
        with_emblem = m.plan_runs(tasks, emblems={"减益": 1})[0]
        self.assertEqual(sum(1 for c in with_emblem.lineup if "减益" in m.CHAR_BONDS.get(c, ())), 5)

        # 公司只有 3 个成员，任务要 4 人时原本无解，带 1 个公司星徽就能凑出
        self.assertEqual(m.plan_runs([m.BondTask("公司", 4)]), [])
        self.assertTrue(m.plan_runs([m.BondTask("公司", 4)], emblems={"公司": 1}))

    def test_unreachable_target_produces_no_run(self):
        self.assertEqual(m.plan_runs([m.BondTask("公司", 9)]), [])
        self.assertIsNone(m.next_run([m.BondTask("护盾", 4, done=True)]))


class TestSourceIntegration(unittest.TestCase):
    def test_currency_wars_dispatches_bond_chain(self):
        src = Path(__file__).resolve().parents[2] / "tasks" / "weekly" / "currency_wars.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("from tasks.weekly.currency_wars_bond import bond_chain_step", text)
        self.assertIn('cfg.currencywars_strategy == "bond_chain"', text)
        # 羁绊策略下不能把成员卖掉
        self.assertIn('if cfg.currencywars_strategy != "bond_chain":', text)


    def test_stuck_detection_ignores_battle(self):
        """长战斗里关卡编号不变，不能被判成卡死（曾因此放弃结算掉一局）。"""
        src = Path(__file__).resolve().parents[2] / "tasks" / "weekly" / "currency_wars.py"
        text = src.read_text(encoding="utf-8")
        self.assertIn("def _detect_battle(self)", text)
        stage_block = text.split("def identify_current_stage", 1)[1].split("def collect_reward", 1)[0]
        self.assertIn("_detect_battle()", stage_block)
        self.assertIn("_stage_unchanged_count = 0", stage_block)
        self.assertIn("900", stage_block)


class TestStarUpgrade(unittest.TestCase):
    """前台角色要能升星：光凑齐人头、全队一星，羁绊满了也打不过（实机 2026-10-23 3-7）。"""

    def setUp(self):
        patcher = patch.object(m.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._old_auto = getattr(m, "auto", None)
        m.auto = _MuteAuto()
        self.addCleanup(setattr, m, "auto", self._old_auto)

    def test_buys_duplicates_for_named_star_targets(self):
        ui = ShopFake(money=20, slots=["桑博", "桑博", "桑博"])
        ui.character_flags["桑博"] = True  # 已经拥有一张，缺的是升星
        m.install_costs(ui)
        got = m.buy_members(ui, [], star_targets=["桑博"])
        self.assertEqual(got, m.MAX_STAR_BUYS, "升星没买满，或者买过头了")
        self.assertEqual(ui.bought, ["桑博", "桑博"])

    def test_does_not_rebuy_when_not_asked_to_star(self):
        """不在 star_targets 里的已拥有角色照旧跳过，别把钱花在填位角色身上。"""
        ui = ShopFake(money=20, slots=["桑博", "桑博"])
        ui.character_flags["桑博"] = True
        m.install_costs(ui)
        self.assertEqual(m.buy_members(ui, [], star_targets=[]), 0)
        self.assertEqual(ui.bought, [])

    def test_three_star_target_is_not_put_back_in_candidates(self):
        """三星了就别再挂进候选表——候选是每轮重新扫描的对象，挂着只会白占位置。"""
        ui = ShopFake(money=20, slots=["桑博", "桑博"])
        ui.character_flags["桑博"] = True
        m._star_counts(ui)["桑博"] = m.MAX_STAR_BUYS
        m.install_costs(ui)
        self.assertEqual(m.buy_members(ui, [], star_targets=["桑博"]), 0)
        self.assertEqual(ui.bought, [])

    def test_star_counts_reset_when_stage_rolls_back(self):
        ui = ShopFake()
        m._reset_stars_on_new_run(ui, "3-7")
        m._star_counts(ui)["桑博"] = m.MAX_STAR_BUYS
        m._reset_stars_on_new_run(ui, "1-1")  # 阶段回退＝开了新一局
        self.assertEqual(m._star_counts(ui), {}, "新一局还留着上一局的升星计数")

    def test_star_counts_survive_same_stage(self):
        ui = ShopFake()
        m._reset_stars_on_new_run(ui, "2-3")
        m._star_counts(ui)["桑博"] = 1
        m._reset_stars_on_new_run(ui, "2-4")
        self.assertEqual(m._star_counts(ui).get("桑博"), 1)


class TestLateStageMoney(unittest.TestCase):
    """后期（3-6 起）钱不够时，备战席空着也要卖场上占位角色换钱。"""

    class _Char:
        def __init__(self, name=None, pos=None):
            self.name = name
            self.pos = pos

    def _ui(self, prepare, forward):
        ui = ShopFake(money=2)
        ui.prepare_characters = [self._Char(n) for n in prepare]
        ui.forward_characters = [self._Char(n) for n in forward]
        ui.forward_pos = ["front%d" % i for i in range(len(forward))]
        ui.backward_characters = []
        ui.backward_pos = []
        ui.sold = []
        ui._sell_single_character = lambda idx, pos_list=None: (ui.sold.append(idx), True)[1]
        return ui

    def test_stage_boundary(self):
        self.assertFalse(m._is_late_stage("3-5"))
        self.assertTrue(m._is_late_stage("3-6"))
        self.assertTrue(m._is_late_stage("3-7"))
        self.assertFalse(m._is_late_stage(""))
        self.assertFalse(m._is_late_stage("bad"))

    def test_cheapest_missing_cost(self):
        ui = ShopFake(money=2)
        ui.character_flags["桑博"] = True
        # 还缺灵砂(2)、符玄(4) → 最便宜 2
        self.assertEqual(m.missing_cheapest_cost(ui, ["桑博", "灵砂", "符玄"]), 2)
        ui.character_flags["灵砂"] = True
        ui.character_flags["符玄"] = True
        self.assertIsNone(m.missing_cheapest_cost(ui, ["桑博", "灵砂", "符玄"]))

    def test_force_battlefield_sells_even_with_free_slot(self):
        """实机 2026-10-23 3-6：备战席明明有空位，但货币不够买希儿，占位的千冶·刃始终没卖。"""
        ui = self._ui(["桑博", None], ["千冶·刃", "希儿"])
        sold = m.sell_offlineup(ui, ["桑博", "希儿"], keep_bonds={"贝洛伯格"},
                                force_battlefield=True)
        self.assertEqual(sold, 1)
        self.assertEqual(ui.sold, [0], "该卖前台的千冶·刃，希儿属于贝洛伯格不能动")

    def test_without_force_battlefield_still_skips_when_slot_free(self):
        ui = self._ui(["桑博", None], ["千冶·刃", "希儿"])
        self.assertEqual(m.sell_offlineup(ui, ["桑博", "希儿"], keep_bonds={"贝洛伯格"}), 0)
        self.assertEqual(ui.sold, [])


class TestBondTeamMembers(unittest.TestCase):
    """摆位要用的「本局羁绊成员」清单：三个区域都得扫。

    实机 2026-10-23 2-5：前台是黄泉、彦卿、不死途、缇宝这些高费无关角色，
    桑博（1 费，贝洛伯格）在备战席坐冷板凳——羁绊人数全场都算，但技能和
    升星只看前台，只按费用排前台就永远轮不到他。
    """

    @staticmethod
    def _ui(forward=(), backward=(), prepare=()):
        from types import SimpleNamespace

        def zone(names):
            return [SimpleNamespace(name=n) for n in names]

        return SimpleNamespace(forward_characters=zone(forward),
                               backward_characters=zone(backward),
                               prepare_characters=zone(prepare))

    def test_picks_bond_members_from_every_zone(self):
        ui = self._ui(forward=["黄泉"], backward=["千冶·刃"], prepare=["桑博", "娜塔莎"])
        names = m._bond_team_members(ui, {"贝洛伯格", "治疗"})
        self.assertEqual(set(names), {"桑博", "娜塔莎"}, "只收本局羁绊成员，且后台备战席都算")

    def test_keeps_same_name_only_once(self):
        ui = self._ui(forward=["桑博"], prepare=["桑博"])
        self.assertEqual(m._bond_team_members(ui, {"贝洛伯格"}), ["桑博"])

    def test_ignores_unrelated_high_cost(self):
        ui = self._ui(forward=["黄泉", "彦卿"])
        self.assertEqual(m._bond_team_members(ui, {"贝洛伯格", "治疗"}), [])

    def test_empty_bonds_returns_empty(self):
        ui = self._ui(forward=["桑博"])
        self.assertEqual(m._bond_team_members(ui, set()), [])

    def test_blank_names_skipped(self):
        ui = self._ui(forward=[None, "桑博"])
        self.assertEqual(m._bond_team_members(ui, {"贝洛伯格"}), ["桑博"])


class TestDeployWiring(unittest.TestCase):
    """摆位链路的接线检查：策略产出清单 → deploy_and_optimize 优先用它。

    真跑 deploy_and_optimize 需要整套 UI，这里只防「断链」与「优先级退化成纯费用」。
    """

    def test_strategy_exposes_push_list(self):
        src = Path("tasks/weekly/currency_wars_bond.py").read_text(encoding="utf-8")
        self.assertIn("ui.bond_push_forward = _bond_team_members", src,
                      "bond_chain_step 必须把本局羁绊成员挂到 ui 上")

    def test_deploy_consumes_push_list_with_priority(self):
        src = Path("tasks/weekly/currency_wars.py").read_text(encoding="utf-8")
        self.assertIn("bond_push_forward", src, "deploy_and_optimize 要读这份清单")
        self.assertIn("priority_forward=True", src,
                      "带优先级的排序必须打开，否则 1 费目标角色仍排在最后")


class TestBoardBondCounts(unittest.TestCase):
    """任务条件看场上人数：只算前台＋后台，备战席不贡献羁绊。"""

    class _Char:
        def __init__(self, name):
            self.name = name

    def _ui(self, forward=(), backward=(), prepare=()):
        ui = type("UI", (), {})()
        ui.forward_characters = [self._Char(n) for n in forward]
        ui.backward_characters = [self._Char(n) for n in backward]
        ui.prepare_characters = [self._Char(n) for n in prepare]
        return ui

    def test_counts_forward_and_backward(self):
        ui = self._ui(forward=["桑博", "娜塔莎"], backward=["杰帕德"])
        self.assertEqual(m._board_bond_counts(ui, {"贝洛伯格"}), {"贝洛伯格": 3})

    def test_prepare_not_counted(self):
        ui = self._ui(forward=["桑博"], prepare=["杰帕德", "娜塔莎"])
        self.assertEqual(m._board_bond_counts(ui, {"贝洛伯格"}), {"贝洛伯格": 1})

    def test_multi_bond_member_counted_per_bond(self):
        ui = self._ui(forward=["灵砂"])
        self.assertEqual(m._board_bond_counts(ui, {"治疗", "击破"}),
                         {"击破": 1, "治疗": 1})

    def test_unknown_bond_returns_empty(self):
        ui = self._ui(forward=["桑博"])
        self.assertEqual(m._board_bond_counts(ui, {"不存在的羁绊"}), {})

    def test_missing_zones_tolerated(self):
        ui = type("UI", (), {})()
        self.assertEqual(m._board_bond_counts(ui, {"贝洛伯格"}), {})

    def test_empty_forward_entry_skipped(self):
        ui = self._ui(forward=[None, "桑博"])
        self.assertEqual(m._board_bond_counts(ui, {"贝洛伯格"}), {"贝洛伯格": 1})



if __name__ == "__main__":
    unittest.main()
