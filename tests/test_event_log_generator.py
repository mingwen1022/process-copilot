from __future__ import annotations

from data.schema import ArrivalConfig, CaseStatus, DefectSpec, DefectType, DwellParam, ScenarioConfig
from app.generation.event_log_generator import generate_event_log, load_process_definition

PROCESS_PATH = "data/cases/leave_request/standard/target.json"


def _base_scenario(**overrides) -> ScenarioConfig:
    base = dict(
        scenario_id="test_leave",
        process_target_path=PROCESS_PATH,
        case_count=40,
        start_date="2026-01-01",
        end_date="2026-01-31",
        arrival=ArrivalConfig(rate_per_day=3.0, peak_multiplier={"month_end": 1.5}),
        node_dwell_params={
            "dept_supervisor": DwellParam(mu=0.5, sigma=0.4),
            "dept_gm": DwellParam(mu=0.8, sigma=0.4),
            "line_leader": DwellParam(mu=0.8, sigma=0.4),
        },
        decision_probabilities={"dept_supervisor": 0.9, "dept_gm": 0.9, "line_leader": 0.9},
        assumed_sla_days={"dept_supervisor": 1.0, "dept_gm": 2.0, "line_leader": 3.0},
        leave_type_weights={"年假": 0.4, "事假": 0.3, "病假": 0.2, "婚假": 0.1},
        leave_days_range={"年假": (1, 10), "事假": (1, 5), "病假": (1, 7), "婚假": (3, 10)},
        injected_defects=[],
        random_seed=7,
    )
    base.update(overrides)
    return ScenarioConfig(**base)


def test_generation_is_deterministic_given_same_seed() -> None:
    scenario = _base_scenario()
    cases1 = generate_event_log(scenario)
    cases2 = generate_event_log(scenario)
    assert [c.model_dump_json() for c in cases1] == [c.model_dump_json() for c in cases2]


def test_zero_defect_scenario_has_no_injected_labels() -> None:
    cases = generate_event_log(_base_scenario())
    for case in cases:
        assert case.injected_defect_ids == []
        for event in case.events:
            assert event.injected_defect_labels == []


def test_case_count_matches_and_events_are_nonempty() -> None:
    cases = generate_event_log(_base_scenario(case_count=25))
    assert len(cases) == 25
    for case in cases:
        assert len(case.events) >= 1
        assert case.events[0].node_id == "draft"


def test_trajectory_is_reconstructable() -> None:
    """每条实例的事件链能重建成合法轨迹：enter 衔接上一条 leave，
    node_id 衔接上一条的 target_node_id（DRAFT 特例映射到 draft 节点）。"""
    process = load_process_definition(PROCESS_PATH)
    cases = generate_event_log(_base_scenario(case_count=30))
    valid_node_ids = {node.node_id for node in process.flow_nodes} | {"ops_intervention"}

    for case in cases:
        prev_leave = None
        for event in case.events:
            assert event.node_id in valid_node_ids
            if prev_leave is not None:
                assert event.enter_time == prev_leave
            prev_leave = event.leave_time
            if event.target_node_id not in (None, "END", "DRAFT"):
                assert event.target_node_id in valid_node_ids

        if case.case_status == CaseStatus.COMPLETED:
            assert case.events[-1].target_node_id == "END"
            assert case.closed_at == case.events[-1].leave_time
        if case.case_status == CaseStatus.IN_PROGRESS:
            assert case.events[-1].leave_time is None
            assert case.closed_at is None


def test_slow_node_defect_inflates_dwell_and_labels_events() -> None:
    baseline_cases = generate_event_log(_base_scenario(case_count=150, random_seed=99))
    slow_cases = generate_event_log(
        _base_scenario(
            case_count=150,
            random_seed=99,
            injected_defects=[
                DefectSpec(
                    defect_id="slow_line_leader",
                    type=DefectType.SLOW_NODE,
                    target_node_id="line_leader",
                    params={"dwell_multiplier": 20.0},
                    affected_case_ratio=1.0,
                )
            ],
        )
    )

    def line_leader_dwells(cases):
        return [e.dwell_seconds for c in cases for e in c.events if e.node_id == "line_leader"]

    baseline_dwells = line_leader_dwells(baseline_cases)
    slow_dwells = line_leader_dwells(slow_cases)
    assert baseline_dwells and slow_dwells, "场景配置里请假天数分布应至少让部分案例升级到 line_leader"
    assert (sum(slow_dwells) / len(slow_dwells)) > 10 * (sum(baseline_dwells) / len(baseline_dwells))

    hit_events = [
        event
        for case in slow_cases
        for event in case.events
        if "slow_line_leader" in event.injected_defect_labels
    ]
    assert hit_events
    for event in hit_events:
        assert event.node_id == "line_leader"


def test_high_return_rate_defect_increases_return_actions() -> None:
    low_return = generate_event_log(_base_scenario(case_count=80, random_seed=11))
    high_return = generate_event_log(
        _base_scenario(
            case_count=80,
            random_seed=11,
            injected_defects=[
                DefectSpec(
                    defect_id="high_return",
                    type=DefectType.HIGH_RETURN_RATE,
                    target_node_id="dept_supervisor",
                    params={"return_probability": 0.9},
                    affected_case_ratio=1.0,
                )
            ],
        )
    )

    def count_returns(cases):
        return sum(1 for c in cases for e in c.events if e.action_category.value == "退回")

    assert count_returns(high_return) > count_returns(low_return)
    hit_labels = {
        defect_id
        for case in high_return
        for event in case.events
        for defect_id in event.injected_defect_labels
    }
    assert "high_return" in hit_labels


def test_zombie_instance_defect_leaves_case_in_progress() -> None:
    scenario = _base_scenario(
        case_count=100,
        injected_defects=[
            DefectSpec(defect_id="zombie", type=DefectType.ZOMBIE_INSTANCE, affected_case_ratio=1.0)
        ],
    )
    cases = generate_event_log(scenario)
    assert all(c.case_status == CaseStatus.IN_PROGRESS for c in cases)
    assert all(c.injected_defect_ids == ["zombie"] for c in cases)
    assert all(c.events[-1].leave_time is None for c in cases)


def test_manual_intervention_defect_inserts_independent_ops_pseudo_node() -> None:
    """镜像真实 OA 看板：人工介入不是给正常环节打标，而是插入一条独立的
    "后台运维操作"伪环节任务行，处理人是合成运维账号，紧跟在被介入的真实
    环节之后（用于按事件相邻关系推导"运维前环节"，不需要额外 schema 字段）。"""
    scenario = _base_scenario(
        case_count=60,
        injected_defects=[
            DefectSpec(defect_id="manual", type=DefectType.MANUAL_INTERVENTION, affected_case_ratio=1.0)
        ],
    )
    cases = generate_event_log(scenario)
    for case in cases:
        for index, event in enumerate(case.events):
            if "manual" not in event.injected_defect_labels:
                continue
            assert event.action_category.value == "人工介入"
            assert event.node_id == "ops_intervention"
            assert event.node_name == "后台运维操作"
            assert event.resource_user_id not in {None, ""}
            assert event.resource_user_id.startswith("ops_")
            # 紧邻的前一条事件就是被介入的真实环节，且真实环节本身不被打标/改写
            prior_event = case.events[index - 1]
            assert prior_event.node_id != "ops_intervention"
            assert "manual" not in prior_event.injected_defect_labels
            assert event.enter_time == prior_event.leave_time
    manual_events = [
        event for case in cases for event in case.events if "manual" in event.injected_defect_labels
    ]
    assert manual_events, "affected_case_ratio=1.0 时应至少有部分案例命中人工介入"


def test_illegal_skip_defect_forces_route_override() -> None:
    scenario = _base_scenario(
        case_count=120,
        leave_type_weights={"婚假": 1.0},
        leave_days_range={"婚假": (10, 10)},
        decision_probabilities={"dept_supervisor": 1.0, "dept_gm": 1.0, "line_leader": 1.0},
        injected_defects=[
            DefectSpec(
                defect_id="illegal_skip",
                type=DefectType.ILLEGAL_SKIP,
                target_node_id="dept_gm",
                params={"force_target_node_id": "END"},
                affected_case_ratio=1.0,
            )
        ],
    )
    cases = generate_event_log(scenario)
    # 婚假 10 天必然触发 dept_gm -> line_leader 的升级条件；违规跳级病灶应强制其提前在 dept_gm 结束
    skip_events = [
        event
        for case in cases
        for event in case.events
        if "illegal_skip" in event.injected_defect_labels
    ]
    assert skip_events
    for event in skip_events:
        assert event.node_id == "dept_gm"
        assert event.target_node_id == "END"


def test_illegal_skip_defect_never_overrides_a_genuine_return_decision() -> None:
    """回归：违规跳级病灶曾经不分青红皂白地覆盖 target_node_id，只要不是
    None/END 就强制改成 END——结果把"审批人不同意、退回起草"这个合法决策
    也篡改成了提前结束。这个 bug 是靠跟 conformance 指标交叉核对真实生成
    数据才发现的，不是靠猜。这里固定 agree 概率为 0，确保只会触发退回，
    验证违规跳级病灶在这种情况下绝不生效。"""
    scenario = _base_scenario(
        case_count=60,
        leave_type_weights={"婚假": 1.0},
        leave_days_range={"婚假": (10, 10)},
        decision_probabilities={"dept_supervisor": 1.0, "dept_gm": 0.0, "line_leader": 1.0},
        max_resubmit_loops=1,
        injected_defects=[
            DefectSpec(
                defect_id="illegal_skip",
                type=DefectType.ILLEGAL_SKIP,
                target_node_id="dept_gm",
                params={"force_target_node_id": "END"},
                affected_case_ratio=1.0,
            )
        ],
    )
    cases = generate_event_log(scenario)
    dept_gm_events = [event for case in cases for event in case.events if event.node_id == "dept_gm"]
    assert dept_gm_events
    for event in dept_gm_events:
        assert "illegal_skip" not in event.injected_defect_labels
        assert event.action_category.value in {"退回", "系统终止"}
        assert event.target_node_id != "END"


def test_resource_resolution_is_stable_for_same_initiator_across_visits() -> None:
    """同一申请人在同一环节多次经过（退回重提后再次到达）应解析到同一处理人，
    不应每次重新随机——这是组织角色解析的确定性保证，不是流程采样的随机性。"""
    scenario = _base_scenario(
        case_count=50,
        injected_defects=[
            DefectSpec(
                defect_id="high_return",
                type=DefectType.HIGH_RETURN_RATE,
                target_node_id="dept_supervisor",
                params={"return_probability": 0.6},
                affected_case_ratio=1.0,
            )
        ],
    )
    cases = generate_event_log(scenario)
    for case in cases:
        visits = [e for e in case.events if e.node_id == "dept_supervisor"]
        if len(visits) > 1:
            assert len({v.resource_user_id for v in visits}) == 1
