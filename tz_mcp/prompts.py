"""天正命令行提示语的判定与分类。

不需要 AutoCAD：输入是一行提示文本，输出是"它在等什么"。
数据来源：intel/prompts.json（由 tools/build_intel.py 从 天正交互提示语.txt 生成）。

为什么要有它：现在的恢复逻辑只看窗口 IsWindowEnabled（recovery.modal），
把"天正在等输入"和"天正弹了模态框"混成一件事。有了分类之后，
可以先把提示语读出来、判类型、按类型应答；只有判不出来才 ESC。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 结构模式
_OPTION_GROUP = re.compile(r'[\[［]([^\]］]+)[\]］]')          # [输入半径(R)]
_OPTION_ITEM = re.compile(r'([^/／、，,\s]{1,10})\s*[（(]\s*([A-Za-z])\s*[)）]')
_DEFAULT = re.compile(r'<([^<>]+)>')                             # <退出>
_TYPE_LIST = re.compile(r'[（(]\s*([A-Z][A-Z/]{2,})\s*[)）]')   # (LINE/ARC/PLINE)

# 伪中文：字节错位产生的字，静态扫描里约占 5–8%（已知样本）
_MOJIBAKE = frozenset('譄甀倂蘀')

_SELECT = ('选择', '拾取', '选取', '框选')
_POINT = ('插入点', '参考点', '基点', '第一点', '第二点', '第三点', '下一点', '角点',
          '起点', '终点', '中点', '端点', '圆心', '点取', '指定点', '输入点', '点选',
          '拾取点', '点出', '位置')
_VALUE = ('输入', '半径', '长度', '角度', '距离', '宽度', '高度', '厚度', '个数', '比例',
          '字高', '标高', '数值')
# 「确定」是常用词，不能当子串匹配（'输入半径(光标位置确定弧的方向)' 会被误判成确认）
_CONFIRM_STRICT = re.compile(r'^(确定|确认)[?？:：]?$')
# 「选项关键字」是 AutoCAD 原生输入校验的固定尾巴（需要点或选项关键字。/ 需要二维角点或选项关键字。），
# 天正自己的提示语语料里一条都没有（intel/prompts.json 实测 0 条），所以只能靠这条兜住。
_ERROR = ('！', '!', '无效', '错误', '不能', '失败', '重新选择', '超出', '未找到', '选项关键字')


def _is_confirm(text: str) -> bool:
    return '是否' in text or '要不要' in text or bool(_CONFIRM_STRICT.match(text.strip()))

# waiting=False 的类型
QUIET_KINDS = ('error', 'info')

KINDS = ('error', 'confirm', 'select', 'point', 'value', 'keyword', 'default_wait', 'info')


def _options(text: str) -> list:
    """[输入半径(R)/两点(P)] -> [{'label': '输入半径', 'key': 'R'}]"""
    out = []
    for group in _OPTION_GROUP.findall(text):
        for label, key in _OPTION_ITEM.findall(group):
            out.append({'label': label, 'key': key.upper()})
    return out


def _types(text: str) -> list:
    m = _TYPE_LIST.search(text)
    return m.group(1).split('/') if m else []


def _residue(text: str) -> str:
    """去掉所有 [选项组] 和尾部冒号之后剩下的主干文字。

    用来区分两种都带选项的提示：
      '点取弧上一点或 [输入半径(R)]:'     主干='点取弧上一点或' → 在等一个点
      '[标准柱(S)/点选插入柱子(P)]:'      主干=''               → 在等一个关键字
    只看有没有选项会把后者误判成 point（选项标签里就带「点选」两个字）。
    """
    body = _OPTION_GROUP.sub('', text)
    return re.sub(r'[:：>＞\s]+$', '', body).strip()


def classify(text: str) -> dict:
    """把一行命令行提示判成一种类型，并抽出选项、默认值、允许的实体类型。"""
    raw = (text or '').strip()
    default = None
    m = _DEFAULT.search(raw)
    if m:
        default = m.group(1).strip()
    options = _options(raw)
    types = _types(raw)

    kind = 'info'
    if raw.startswith('需要') or any(tok in raw for tok in _ERROR):
        kind = 'error'
    elif _is_confirm(raw):
        kind = 'confirm'
    elif options and len(_residue(raw)) <= 3:
        # 整行就是一个选项菜单：[标准柱(S)/点选插入柱子(P)]: —— 选项文字里的动词不作数
        kind = 'keyword'
    elif any(tok in raw for tok in _SELECT):
        kind = 'select'
    elif any(tok in raw for tok in _POINT):
        kind = 'point'
    elif any(tok in raw for tok in _VALUE):
        kind = 'value'
    elif options:
        kind = 'keyword'
    elif default is not None:
        kind = 'default_wait'
    elif raw.startswith('请') and re.search(r'[:：>]\s*$', raw):
        # 「请正确点取!」之外的「请…：」——多数是等待，但标记弱，单独记
        kind = 'default_wait'

    suspect = any(ch in _MOJIBAKE for ch in raw) or not raw
    return {
        'text': raw,
        'kind': kind,
        'waiting': kind not in QUIET_KINDS and not suspect,
        'options': options,
        'default': default,
        'types': types,
        'suspect': suspect,
    }


def waiting(text: str) -> bool:
    """这一行是不是「正在等输入」。error 类里也有「请正确点取!」这种——它也是等输入。"""
    info = classify(text)
    if info['suspect']:
        return False
    if info['kind'] == 'error':
        # 校验失败后命令还在跑，通常紧接着重新要输入；判定交给调用方，这里只回答字面
        return False
    return info['kind'] != 'info'


def answer(info: dict) -> str:
    """按类型给出「下一步该喂什么」的说明，供人读日志、或给 input_bridge 排脚本用。"""
    kind = info['kind']
    if kind == 'select':
        return '等一个选择集：回车结束/输入实体或空集'
    if kind == 'point':
        return '等一个点坐标'
    if kind == 'value':
        return '等一个数值'
    if kind == 'keyword':
        return '等一个关键字：' + '/'.join(o['key'] for o in info['options'])
    if kind == 'confirm':
        return '等确认：Y/N 或回车取默认'
    if kind == 'default_wait':
        return '等输入，默认值=%s（回车即取默认）' % (info['default'] or '无')
    if kind == 'error':
        return '提示文本是校验失败，命令仍在运行'
    return '不是等待，是说明/标签文本'


def summarize(items) -> dict:
    """统计一个提示语集合的类型分布（给 build_intel 用）。"""
    counts = {k: 0 for k in KINDS}
    suspect = 0
    for it in items:
        info = classify(it['text'] if isinstance(it, dict) else it)
        counts[info['kind']] += 1
        suspect += 1 if info['suspect'] else 0
    return {'kinds': counts, 'suspect': suspect, 'total': len(items)}
