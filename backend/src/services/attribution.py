"""签核快照归因：只使用两次签核当时冻结的口径与各班日报。

把“口径版本”和“每个班的日报数据”视为可替换因素，用 Shapley 值把两次
签核的指标差值分解到各因素。Shapley 值按所有替换顺序的边际影响取平均，
因此贡献不受替换顺序影响，各贡献之和恒等于两次签核的实际差值。

因素较多时用固定种子的置换抽样估算，并给出 95% 置信误差界；任何混搭
组合无法算出比值时整体失败，明确指出出错的组合，此时不给出任何贡献。
"""
import hashlib
import json
import math
import random

from src.services.analysis import ratio
from src.services.expression import execute, parse

VERSION = 'camp-attribution-shapley-v1'
# 因素（口径 + 变化班级）不超过该数量时穷举全部替换组合，逐个校验可计算性。
EXACT_MAX_PLAYERS = 12
DEFAULT_SAMPLES = 2000
# 班级很多时减少抽样次数以控制耗时；误差界会相应变宽并如实展示。
MIN_SAMPLES = 200
SAMPLE_WORK = 200000
Z_95 = 1.959963984540054
MAX_INVALID_REPORT = 50
MAX_CHANGE_DETAIL = 20
MAX_DENOMINATOR_STATES = 20000

DEFINITION_FIELDS = (
    'version',
    'name',
    'numerator',
    'denominator',
    'numerator_mode',
    'denominator_mode',
)
# 只有这些口径字段会改变指标结果；name/version 变化只作依据展示，不单独构成因素。
CALC_FIELDS = ('numerator', 'denominator', 'numerator_mode', 'denominator_mode')
# 只有这些字段会影响指标结果；id 与说明文字不参与比较。
FACT_FIELDS = ('day', 'mode', 'enrolled', 'attended', 'completed', 'refunds')

METHOD = (
    '只读取两次签核快照中冻结的口径与班级日报，不回读现行口径或现行事实。'
    '把口径与每个发生变化的班的日报作为可替换因素：因素取“当前”时用当前签核'
    '版本，取“基线”时用基线签核版本，其余因素保持对应版本，逐行套用白名单表达式'
    '后分别汇总分子/分母并相除。贡献是全部替换顺序下边际影响的平均（Shapley 值），'
    '所以与替换顺序无关；各贡献之和等于两次签核的实际比值差。未变化的班贡献为 0。'
)


class InvalidMix(Exception):
    """某个混搭组合无法得到比值。"""

    def __init__(self, reason, definition, current_classes, mask):
        super().__init__(reason)
        self.reason = reason
        self.definition = definition
        self.current_classes = list(current_classes)
        self.mask = mask

    def record(self):
        return {
            'mask': self.mask,
            'definition': self.definition,
            'current_classes': self.current_classes,
            'reason': self.reason,
            'label': _describe_invalid(
                self.definition, self.current_classes, self.reason
            ),
        }


def _describe_invalid(definition, current_classes, reason):
    definition_label = '当前' if definition == 'current' else '基线'
    classes = '、'.join(current_classes) if current_classes else '无'
    return (
        '口径=' + definition_label + '；日报换成当前版的班：' + classes
        + '；其余班用另一版日报。原因：' + reason
    )


def checksum(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def freeze_snapshot(snapshot):
    """提取签核快照内冻结的口径与日报；缺少冻结输入则拒绝归因。"""
    result = snapshot.result if isinstance(snapshot.result, dict) else {}
    frozen = result.get('snapshot')
    if not isinstance(frozen, dict):
        raise ValueError('快照缺少签核时冻结的口径与日报，不能归因')
    metric = frozen.get('metric')
    facts = frozen.get('facts')
    if not isinstance(metric, dict) or not isinstance(facts, list):
        raise ValueError('快照缺少签核时冻结的口径与日报，不能归因')
    date_from = frozen.get('date_from') or snapshot.date_from
    date_to = frozen.get('date_to') or snapshot.date_to
    if not date_from or not date_to:
        raise ValueError('快照缺少冻结日期窗，不能归因')
    # 深拷贝，避免调用方后续修改影响已冻结依据。
    metric = json.loads(json.dumps(metric, ensure_ascii=False))
    facts = json.loads(json.dumps(facts, ensure_ascii=False))
    return {
        'snapshot_id': snapshot.id,
        'metric': metric,
        'metric_code': metric.get('code'),
        'facts': facts,
        'date_from': str(date_from),
        'date_to': str(date_to),
        'value': result.get('value'),
        'facts_checksum': result.get('facts_checksum') or checksum(facts),
        'approved_by': getattr(snapshot, 'approved_by', '') or '',
    }


def _facts_by_class(frozen):
    grouped = {}
    for row in frozen['facts']:
        grouped.setdefault(row['class_code'], []).append(row)
    return grouped


def _signature(rows):
    return tuple(
        sorted(tuple(row.get(field) for field in FACT_FIELDS) for row in rows)
    )


def _day_map(rows):
    return {row.get('day'): row for row in rows}


def _class_change(rows_a, rows_b):
    """返回班级日报的变化摘要；只比较会影响指标的字段。"""
    if _signature(rows_a) == _signature(rows_b):
        return None
    days_a, days_b = _day_map(rows_a), _day_map(rows_b)
    details = []
    for day in sorted(set(days_a) | set(days_b), key=lambda item: str(item)):
        before, after = days_a.get(day), days_b.get(day)
        if before is None:
            details.append({
                'day': day,
                'change': 'added',
                'after': {field: after.get(field) for field in FACT_FIELDS},
            })
        elif after is None:
            details.append({
                'day': day,
                'change': 'removed',
                'before': {field: before.get(field) for field in FACT_FIELDS},
            })
        else:
            changed = [
                {'field': field, 'before': before.get(field), 'after': after.get(field)}
                for field in FACT_FIELDS
                if before.get(field) != after.get(field)
            ]
            if changed:
                details.append({'day': day, 'change': 'updated', 'fields': changed})
    return {
        'changed_days': len(details),
        'detail_truncated': len(details) > MAX_CHANGE_DETAIL,
        'details': details[:MAX_CHANGE_DETAIL],
    }


def _definition_changes(metric_a, metric_b):
    return [
        {'field': field, 'before': metric_a.get(field), 'after': metric_b.get(field)}
        for field in DEFINITION_FIELDS
        if metric_a.get(field) != metric_b.get(field)
    ]


def _calculation_changes(metric_a, metric_b):
    return [
        {'field': field, 'before': metric_a.get(field), 'after': metric_b.get(field)}
        for field in CALC_FIELDS
        if metric_a.get(field) != metric_b.get(field)
    ]


def _grouped_sums(metric, rows):
    """按分子/分母各自的授课方式过滤，逐行求值并分别求和。"""
    numerator_tree = parse(metric['numerator'])
    denominator_tree = parse(metric['denominator'])
    numerator_rows = [
        row for row in rows
        if metric['numerator_mode'] == '全部' or row['mode'] == metric['numerator_mode']
    ]
    denominator_rows = [
        row for row in rows
        if metric['denominator_mode'] == '全部' or row['mode'] == metric['denominator_mode']
    ]
    numerator = sum(execute(numerator_tree, row) for row in numerator_rows)
    denominator = sum(execute(denominator_tree, row) for row in denominator_rows)
    return numerator, denominator


def _failed_cell(sums):
    return isinstance(sums[0], str)


def _current_classes(game, mask):
    return [
        code for code in game.classes
        if 'class:' + code in game._player_index
        and mask & (1 << game._player_index['class:' + code])
    ]


class _Game:
    """口径 + 各班日报的替换博弈；按定义、班、日报版本预计算分子/分母。"""

    def __init__(self, baseline, current):
        self.baseline = baseline
        self.current = current
        self.metrics = {'baseline': baseline['metric'], 'current': current['metric']}
        self.facts = {
            'baseline': _facts_by_class(baseline),
            'current': _facts_by_class(current),
        }
        self.classes = sorted(set(self.facts['baseline']) | set(self.facts['current']))
        self.definition_changes = _definition_changes(
            baseline['metric'], current['metric']
        )
        self.calculation_changes = _calculation_changes(
            baseline['metric'], current['metric']
        )
        self.class_changes = {
            code: _class_change(
                self.facts['baseline'].get(code, []),
                self.facts['current'].get(code, []),
            )
            for code in self.classes
        }
        self.changed_classes = [
            code for code in self.classes if self.class_changes[code] is not None
        ]
        self.players = []
        # 只有会改变指标结果的口径字段才是因素；仅改名/改版本号不构成因素。
        if self.calculation_changes:
            self.players.append('definition')
        self.players.extend('class:' + code for code in self.changed_classes)
        self._player_index = {
            player: index for index, player in enumerate(self.players)
        }
        self._sums = {}
        self._precompute()

    def _precompute(self):
        for definition in ('baseline', 'current'):
            metric = self.metrics[definition]
            for side in ('baseline', 'current'):
                for code in self.classes:
                    try:
                        self._sums[(definition, side, code)] = _grouped_sums(
                            metric, self.facts[side].get(code, [])
                        )
                    except ValueError as exc:
                        self._sums[(definition, side, code)] = ('invalid', str(exc))

    def sides(self, code):
        player = 'class:' + code
        return ('baseline', 'current') if player in self._player_index else ('baseline',)

    def value(self, mask):
        definition = (
            'current'
            if 'definition' in self._player_index
            and mask & (1 << self._player_index['definition'])
            else 'baseline'
        )
        numerator = denominator = 0.0
        current_classes = []
        for code in self.classes:
            side = 'baseline'
            player = 'class:' + code
            if player in self._player_index and mask & (1 << self._player_index[player]):
                side = 'current'
                current_classes.append(code)
            sums = self._sums[(definition, side, code)]
            if _failed_cell(sums):
                raise InvalidMix(sums[1], definition, current_classes, mask)
            numerator += sums[0]
            denominator += sums[1]
        if denominator == 0:
            raise InvalidMix(
                '外层聚合分母为零，无法计算比值', definition, current_classes, mask
            )
        try:
            value = ratio(numerator, denominator)
        except ValueError as exc:
            raise InvalidMix(str(exc), definition, current_classes, mask) from exc
        if value is None:
            raise InvalidMix(
                '外层聚合分母为零，无法计算比值', definition, current_classes, mask
            )
        return value

    def structural_errors(self):
        """逐行求值即失败的组合：分子/分母表达式在某个版本日报上无法执行。

        返回示例记录与可计算组合覆盖情况。行级失败与具体班级无关，只要某个班
        使用了失败的日报版本，任何包含它的组合都会失败；用它精确统计受影响的
        组合数量。
        """
        records = []
        for (definition, side, code), sums in sorted(self._sums.items()):
            if not _failed_cell(sums):
                continue
            if definition == 'current' and 'definition' not in self._player_index:
                continue
            if side == 'current' and 'class:' + code not in self._player_index:
                continue
            mask = 0
            if definition == 'current':
                mask |= 1 << self._player_index['definition']
            if side == 'current':
                mask |= 1 << self._player_index['class:' + code]
            current_classes = [code] if side == 'current' else []
            records.append(InvalidMix(
                sums[1], definition, current_classes, mask
            ))
            if len(records) >= MAX_INVALID_REPORT:
                break
        count = 0
        class_players = [p for p in self.players if p.startswith('class:')]
        per_definition_total = 1 << len(class_players)
        definitions = ['baseline']
        if 'definition' in self._player_index:
            definitions.append('current')
        for definition in definitions:
            valid_choices = 1
            for code in self.classes:
                allowed = 0
                for side in self.sides(code):
                    if not _failed_cell(self._sums[(definition, side, code)]):
                        allowed += 1
                valid_choices *= allowed
            count += per_definition_total - valid_choices
        return records, count

    def mixture_witness(self):
        """枚举所有可达的“口径 + 各班日报”混搭，找出分母为零或比值超限的组合。

        对每个可达口径，按班做精确 DP：维护每个可达外层分母下可达到的最大与
        最小分子（以及对应组合），同时累计到达该分母的组合数。分母为 0，或
        |分子/分母|>1e15，都说明存在无法算出比值的混搭，并保留一组具体组合。

        返回 (错误或 None, 是否完整校验, 分母为零的组合数或 None)。分母状态数
        超过上限时无法穷举，返回 checked=False 且计数为 None，调用方需如实标注。
        """
        definitions = ['baseline']
        if 'definition' in self._player_index:
            definitions.append('current')
        checked = True
        zero_count = 0
        for definition in definitions:
            base_mask = 0
            if definition == 'current':
                base_mask |= 1 << self._player_index['definition']
            # denominator -> [max_num, max_mask, min_num, min_mask, count]
            states = {0.0: [0.0, base_mask, 0.0, base_mask, 1]}
            complete = True
            for code in self.classes:
                choices = []
                for side in self.sides(code):
                    cell = self._sums[(definition, side, code)]
                    if _failed_cell(cell):
                        continue
                    bit = (
                        1 << self._player_index['class:' + code]
                        if side == 'current'
                        else 0
                    )
                    choices.append((cell[1], cell[0], bit))
                merged = {}
                for denominator, (max_num, max_mask, min_num, min_mask, count) in states.items():
                    for cell_denominator, cell_numerator, bit in choices:
                        new_denominator = denominator + cell_denominator
                        new_max = max_num + cell_numerator
                        new_min = min_num + cell_numerator
                        new_mask = max_mask | bit
                        known = merged.get(new_denominator)
                        if known is None:
                            merged[new_denominator] = [
                                new_max, new_mask, new_min, new_mask, count
                            ]
                        else:
                            if new_max > known[0]:
                                known[0] = new_max;known[1] = new_mask
                            if new_min < known[2]:
                                known[2] = new_min;known[3] = new_mask
                            known[4] += count
                if len(merged) > MAX_DENOMINATOR_STATES:
                    complete = False
                    break
                states = merged
            if not complete:
                checked = False
                zero_count = None
                continue
            zero_states = [
                (denominator, value) for denominator, value in states.items()
                if denominator == 0
            ]
            if zero_count is not None:
                zero_count += sum(value[4] for _, value in zero_states)
            if zero_states:
                _, (_, mask, _, _, _) = zero_states[0]
                return InvalidMix(
                    '外层聚合分母为零，无法计算比值',
                    definition,
                    _current_classes(self, mask),
                    mask,
                ), checked, zero_count
            for denominator, (max_num, max_mask, min_num, min_mask, _) in states.items():
                if abs(max_num / denominator) > 1e15:
                    return InvalidMix(
                        '指标比值超出允许范围，请检查极小分母与口径',
                        definition,
                        _current_classes(self, max_mask),
                        max_mask,
                    ), checked, zero_count
                if abs(min_num / denominator) > 1e15:
                    return InvalidMix(
                        '指标比值超出允许范围，请检查极小分母与口径',
                        definition,
                        _current_classes(self, min_mask),
                        min_mask,
                    ), checked, zero_count
        return None, checked, zero_count


def _full_contributions(game, values, errors):
    rows = []
    for index, player in enumerate(game.players):
        if player == 'definition':
            rows.append({
                'key': 'definition',
                'kind': 'definition',
                'label': '口径 v%s → v%s' % (
                    game.baseline['metric'].get('version'),
                    game.current['metric'].get('version'),
                ),
                'changed': True,
                'changes': game.calculation_changes,
                'all_definition_changes': game.definition_changes,
                'contribution': values[index],
                'error_bound': errors[index],
                'lower': values[index] - errors[index],
                'upper': values[index] + errors[index],
            })
        else:
            code = player.split(':', 1)[1]
            rows.append({
                'key': player,
                'kind': 'class',
                'label': code,
                'changed': True,
                'change': game.class_changes[code],
                'contribution': values[index],
                'error_bound': errors[index],
                'lower': values[index] - errors[index],
                'upper': values[index] + errors[index],
            })
    for code in game.classes:
        if game.class_changes[code] is None:
            rows.append({
                'key': 'class:' + code,
                'kind': 'class',
                'label': code,
                'changed': False,
                'change': None,
                'contribution': 0.0,
                'error_bound': 0.0,
                'lower': 0.0,
                'upper': 0.0,
            })
    return rows


def _basis(game):
    def side(frozen):
        return {
            'snapshot_id': frozen['snapshot_id'],
            'metric_id': frozen['metric'].get('id'),
            'metric_version': frozen['metric'].get('version'),
            'metric': frozen['metric'],
            'date_from': frozen['date_from'],
            'date_to': frozen['date_to'],
            'facts_rows': len(frozen['facts']),
            'facts_checksum': frozen['facts_checksum'],
            'approved_by': frozen['approved_by'],
            'value': frozen['value'],
        }

    return {
        'frozen_only': True,
        'metric_code': game.current['metric_code'],
        'date_from': game.baseline['date_from'],
        'date_to': game.baseline['date_to'],
        'baseline': side(game.baseline),
        'current': side(game.current),
        'changed_definition_fields': game.definition_changes,
        'calculation_definition_fields': game.calculation_changes,
        'classes': [
            {
                'class_code': code,
                'changed': game.class_changes[code] is not None,
                'change': game.class_changes[code],
            }
            for code in game.classes
        ],
        'players': list(game.players),
        'method': METHOD,
    }


def _base_result(game, delta):
    baseline, current = game.baseline, game.current
    return {
        'version': VERSION,
        'metric_code': current['metric_code'],
        'date_from': baseline['date_from'],
        'date_to': baseline['date_to'],
        'baseline': {
            'snapshot_id': baseline['snapshot_id'],
            'version': baseline['metric'].get('version'),
            'value': baseline['value'],
            'facts_checksum': baseline['facts_checksum'],
            'facts_rows': len(baseline['facts']),
            'approved_by': baseline['approved_by'],
        },
        'current': {
            'snapshot_id': current['snapshot_id'],
            'version': current['metric'].get('version'),
            'value': current['value'],
            'facts_checksum': current['facts_checksum'],
            'facts_rows': len(current['facts']),
            'approved_by': current['approved_by'],
        },
        'delta': delta,
        'basis': _basis(game),
        'method': METHOD,
    }


def _invalid_outcome(game, invalid, count, exact, samples, seed, scanned, total, scope):
    result = _base_result(game, None)
    result.update({
        'outcome': 'invalid_mix',
        'exact': exact,
        'samples': samples,
        'seed': seed,
        'coalition_count': scanned,
        'scanned_coalitions': scanned,
        'total_coalitions': total,
        'validity_scope': scope,
        'confidence': 0.95,
        'contributions': [],
        'sum_contributions': None,
        'residual': None,
        'max_error_bound': None,
        'invalid_coalition_count': count,
        'invalid_report_truncated': count > len(invalid),
        'invalid_coalitions': [error.record() for error in invalid],
        'message': (
            '存在混搭组合无法算出比值，本次不给出任何贡献；'
            '下方列出的是其中一组（或若干组）具体组合，不是全部。'
            '请先修正对应班级日报或口径、重新签核后再发起归因。'
            if scope != 'all_coalitions' else
            '存在混搭组合无法算出比值，本次不给出任何贡献。'
            '请先修正对应班级日报或口径、重新签核后再发起归因。'
        ),
    })
    return result


def _stored_value(frozen):
    return frozen['value']


def _verify_endpoint(game, mask, side):
    frozen = game.baseline if side == 'baseline' else game.current
    value = game.value(mask)
    stored = _stored_value(frozen)
    if stored is None or not math.isclose(
        value, float(stored), rel_tol=1e-9, abs_tol=1e-12
    ):
        raise ValueError(
            '冻结口径与日报重算出的比值与签核快照不一致，'
            '无法保证贡献之和等于实际差值，请重新试算并签核'
        )
    return value


def _computed(game, contributions, delta, extra):
    result = _base_result(game, delta)
    result.update({
        'outcome': 'computed',
        'confidence': 0.95,
        'contributions': contributions,
        'sum_contributions': sum(
            row['contribution'] for row in contributions
        ),
        'residual': sum(row['contribution'] for row in contributions) - delta,
        'invalid_coalition_count': 0,
        'invalid_report_truncated': False,
        'invalid_coalitions': [],
    })
    result.update(extra)
    return result


def attribute(baseline, current):
    """对两次冻结签核做归因，返回可序列化结果。"""
    if baseline['value'] is None or current['value'] is None:
        raise ValueError('签核快照指标为 undefined（外层分母为零），无法归因')
    game = _Game(baseline, current)
    seed = int(
        checksum([checksum(baseline), checksum(current), VERSION])[:16], 16
    ) % (2 ** 63)
    n = len(game.players)
    total = 1 << n

    if n == 0:
        try:
            value = game.value(0)
        except InvalidMix as exc:
            return _invalid_outcome(
                game, [exc], 1, True, 0, seed, 1, 1, 'all_coalitions'
            )
        if not (
            math.isclose(value, float(baseline['value']), rel_tol=1e-9, abs_tol=1e-12)
            and math.isclose(value, float(current['value']), rel_tol=1e-9, abs_tol=1e-12)
        ):
            raise ValueError('未发现口径或日报变化，但两次签核比值不一致，无法归因')
        return _computed(
            game, _full_contributions(game, [], []), 0.0,
            {
                'exact': True, 'samples': 0, 'seed': seed,
                'coalition_count': 1, 'scanned_coalitions': 1,
                'total_coalitions': 1, 'validity_scope': 'all_coalitions',
                'max_error_bound': 0.0,
            },
        )

    if n <= EXACT_MAX_PLAYERS:
        values = []
        invalid = []
        for mask in range(total):
            try:
                values.append(game.value(mask))
            except InvalidMix as exc:
                values.append(None)
                if len(invalid) < MAX_INVALID_REPORT:
                    invalid.append(exc)
        if invalid:
            return _invalid_outcome(
                game, invalid, total - values.count(None), True, 0, seed,
                total, total, 'all_coalitions',
            )
        start = _verify_endpoint(game, 0, 'baseline')
        end = _verify_endpoint(game, total - 1, 'current')
        if not (math.isclose(values[0], start) and math.isclose(values[-1], end)):
            raise ValueError('穷举替换端点与签核快照不一致，无法归因')
        phi = _exact_shapley(values, n)
        return _computed(
            game, _full_contributions(game, phi, [0.0] * n), end - start,
            {
                'exact': True, 'samples': 0, 'seed': seed,
                'coalition_count': total, 'scanned_coalitions': total,
                'total_coalitions': total, 'validity_scope': 'all_coalitions',
                'max_error_bound': 0.0,
            },
        )

    # 因素很多：先用结构错误和混搭见证覆盖全部无法计算的组合，再抽样估算。
    structural, structural_count = game.structural_errors()
    if structural:
        return _invalid_outcome(
            game, structural, structural_count, False, DEFAULT_SAMPLES, seed,
            0, total, 'structural_cells',
        )
    witness, mixture_checked, zero_count = game.mixture_witness()
    if witness is not None:
        scope = 'mixture_witness' if mixture_checked else 'mixture_witness_partial'
        count = zero_count if zero_count is not None else 1
        return _invalid_outcome(
            game, [witness], max(1, count), False, DEFAULT_SAMPLES, seed, 1,
            total, scope,
        )
    start = _verify_endpoint(game, 0, 'baseline')
    end = _verify_endpoint(game, total - 1, 'current')
    samples = _sample_count(n)
    invalid = []
    try:
        phi, errors, scanned = _sample_shapley(
            game, n, samples, seed, invalid
        )
    except InvalidMix as exc:
        # 抽样访问到的混搭组合无法算出比值：整体失败，指出该组合。
        if not invalid:
            invalid.append(exc)
        return _invalid_outcome(
            game, invalid, max(1, len(invalid)), False, samples, seed,
            getattr(game, 'sampled', 0), total, 'sampled_coalitions',
        )
    if invalid:
        return _invalid_outcome(
            game, invalid, len(invalid), False, samples, seed,
            scanned, total, 'sampled_coalitions',
        )
    return _computed(
        game, _full_contributions(game, phi, errors), end - start,
        {
            'exact': False, 'samples': samples, 'seed': seed,
            'coalition_count': scanned, 'scanned_coalitions': scanned,
            'total_coalitions': total, 'validity_scope': 'sampled_coalitions',
            'max_error_bound': max(errors) if errors else 0.0,
        },
    )


def _sample_count(n):
    """因素越多，单次置换越长；按工作量上限收缩抽样次数，保持响应可接受。"""
    return int(max(MIN_SAMPLES, min(DEFAULT_SAMPLES, SAMPLE_WORK // max(1, n))))


def _exact_shapley(values, n):
    factorials = [math.factorial(i) for i in range(n + 1)]
    total_weight = factorials[n]
    phi = [0.0] * n
    for index in range(n):
        bit = 1 << index
        for mask in range(len(values)):
            if mask & bit:
                continue
            size = bin(mask).count('1')
            weight = factorials[size] * factorials[n - size - 1] / total_weight
            phi[index] += weight * (values[mask | bit] - values[mask])
    return phi


def _sample_shapley(game, n, samples, seed, invalid):
    """固定种子的置换抽样；每条置换的边际和恰好等于端点差。"""
    rng = random.Random(seed)
    cache = {}
    visited = set()

    def value(mask):
        if mask not in cache:
            try:
                cache[mask] = game.value(mask)
            except InvalidMix as exc:
                if len(invalid) < MAX_INVALID_REPORT:
                    invalid.append(exc)
                raise
        visited.add(mask)
        # 中途失败时调用方也能读到真实已访问数量，不伪装成 0。
        game.sampled = len(visited)
        return cache[mask]

    totals = [0.0] * n
    squares = [0.0] * n
    for _ in range(samples):
        order = list(range(n))
        rng.shuffle(order)
        mask = 0
        previous = value(0)
        for index in order:
            mask |= 1 << index
            current_value = value(mask)
            marginal = current_value - previous
            totals[index] += marginal
            squares[index] += marginal * marginal
            previous = current_value
    game.sampled = len(visited)
    means = [total / samples for total in totals]
    errors = []
    for index in range(n):
        mean = means[index]
        variance = max(0.0, squares[index] / samples - mean * mean)
        if samples > 1:
            variance *= samples / (samples - 1)
        standard_error = math.sqrt(variance / samples) if samples > 1 else 0.0
        errors.append(Z_95 * standard_error)
    return means, errors, len(visited)
