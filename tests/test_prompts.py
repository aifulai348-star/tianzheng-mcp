import unittest
from tz_mcp.prompts import classify, waiting, answer, summarize


class PromptTests(unittest.TestCase):
    """用例取自 intel/raw/天正交互提示语.txt 的真实行；构造用例单独标注（corpus 里没有）。"""

    # ---- 选择
    def test_select_prompt(self):
        i = classify('请选择对象<返回>:')
        self.assertEqual('select', i['kind'])
        self.assertEqual('返回', i['default'])
        self.assertTrue(i['waiting'])

    def test_type_limited_selection(self):
        i = classify('选择一曲线(LINE/ARC/PLINE):')
        self.assertEqual('select', i['kind'])
        self.assertEqual(['LINE', 'ARC', 'PLINE'], i['types'])

    def test_select_with_keyword_option(self):
        # 主干是「选择…」，方括号里只是选项，仍算选择
        i = classify('请选择需要尺寸标注的墙[带柱子(Y)]<退出>:')
        self.assertEqual('select', i['kind'])
        self.assertEqual(['Y'], [o['key'] for o in i['options']])
        self.assertEqual('退出', i['default'])

    # ---- 取点
    def test_point_prompt_with_option(self):
        i = classify('点取弧上一点或 [输入半径(R)]:')
        self.assertEqual('point', i['kind'])
        self.assertEqual([{'label': '输入半径', 'key': 'R'}], i['options'])

    def test_corner_point_prompt_is_point_not_keyword(self):
        # 回归：早期版本判成 keyword（主干里的「角点」不在关键词表）
        i = classify('另一个角点或 [直墙(L)/弧墙(A)]<取消>:')
        self.assertEqual('point', i['kind'])
        self.assertEqual(['L', 'A'], [o['key'] for o in i['options']])

    # ---- 纯选项菜单（构造用例：语料只有命令提示，不带这种 UIPanel 级菜单行）
    def test_keyword_menu_only_prompt(self):
        i = classify('[标准柱(S)/点选插入柱子(P)]:')
        self.assertEqual('keyword', i['kind'])
        self.assertEqual(['S', 'P'], [o['key'] for o in i['options']])
        self.assertIn('S/P', answer(i))

    def test_keyword_with_plain_residue(self):
        i = classify("'的门窗 [编辑属性(E)/列表查询(L)/返回(X)]<下一个>:")
        self.assertEqual('keyword', i['kind'])
        self.assertEqual(['E', 'L', 'X'], [o['key'] for o in i['options']])

    # ---- 数值
    def test_value_prompt(self):
        for line in ('文字高度:', '输入坐标值<', '剖切编号文字高度:'):
            self.assertEqual('value', classify(line)['kind'], line)

    # ---- 确认
    def test_confirm_prompt(self):
        i = classify('是否确定?[是(Y)/否(N)]<Y>:')
        self.assertEqual('confirm', i['kind'])
        self.assertEqual(['Y', 'N'], [o['key'] for o in i['options']])
        self.assertEqual('Y', i['default'])

    def test_confirm_with_placeholders(self):
        i = classify('其他%d个相同编号的门窗是否同时参与修改?[全部(A)/部分(S)/否(N)]<N>:')
        self.assertEqual('confirm', i['kind'])
        self.assertEqual('N', i['default'])

    def test_word_queding_is_not_confirm(self):
        # 回归：「确定」当子串会把这两条误判成 confirm
        self.assertNotEqual('confirm', classify('输入半径(光标位置确定弧的方向):')['kind'])
        self.assertEqual('select', classify('请选择可以确定分割平面的曲线:')['kind'])

    # ---- 错误 / 说明
    def test_validation_error_is_not_a_wait(self):
        for line in ('无效的选择!', '墙体交线有问题, 无法生成柱子!', '请正确点取!'):
            i = classify(line)
            self.assertEqual('error', i['kind'], line)
            self.assertFalse(waiting(line), line)

    def test_autocad_native_validation_error(self):
        # 这两条是 AutoCAD 自己的校验消息，不在天正语料里（prompts.json 实测 0 条），
        # 来自真机日志 diagnostics/completion-probes-20261117-192439 与 wall-call-raw-20261117-183021。
        # 回归：早先版本把它们判成 point（因为「角点」在取点词表里）。
        for line in ('需要二维角点或选项关键字。', '需要点或选项关键字。'):
            i = classify(line)
            self.assertEqual('error', i['kind'], line)
            self.assertFalse(waiting(line), line)

    def test_info_is_quiet(self):
        i = classify('指定的矩形区域内的轴线交点插入柱子')
        self.assertEqual('info', i['kind'])
        self.assertFalse(i['waiting'])

    # ---- 伪中文
    def test_mojibake_is_flagged_and_not_waiting(self):
        i = classify('譄请')
        self.assertTrue(i['suspect'])
        self.assertFalse(i['waiting'])

    def test_summarize_counts(self):
        s = summarize(['请选择对象<返回>:', '无效的选择!', '譄请'])
        self.assertEqual(3, s['total'])
        self.assertEqual(1, s['suspect'])
        self.assertEqual(1, s['kinds']['select'])
        self.assertEqual(1, s['kinds']['error'])


if __name__ == '__main__':
    unittest.main()
