from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.api.analytics_service import AnalyticsService
from app.api.demo_state import DemoStateService
from app.api.knowledge_service import KnowledgeService
from app.api.runtime_service import RuntimeService
from app.copilots.launch_service import LaunchCopilotService
from app.copilots.ops_service import OpsCopilotService
from app.api.schemas import (
    AnalyticsQuestionRequest,
    AvailablePathsRequest,
    CompleteTaskRequest,
    CreateInstanceRequest,
    CreateWorkflowCaseRequest,
    LoginSessionRequest,
    MockSessionRequest,
    Slice1CompleteWorkItemRequest,
    Slice1RouteOptionsRequest,
    SubmitWorkflowCaseRequest,
    UpdateWorkflowCaseFormRequest,
    WorkflowDesignMessageRequest,
    WorkflowDesignInitializeRequest,
    WorkflowDesignSessionRequest,
    WorkflowDesignSourceRequest,
)
from app.api.slice1_service import Slice1Service
from app.api.workflow_design_service import WorkflowDesignService
from data.schema import ProcessDefinition


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = APP_ROOT / "static"
DOC_ROOT = PROJECT_ROOT / "doc"


def _seed_ops_demo(ops_copilot: OpsCopilotService) -> None:
    """运维副驾的出厂演示数据：启动时播一次，演示"切回起始态"时重播同一套。"""
    ops_copilot.seed_reback_history()  # inst_gap：近30天4单卡覆盖漏洞 → coverage_gap（设计渠道）
    ops_copilot.seed_reback_history("inst_org", times=3)  # inst_org：近30天3单选不到审批人 → org_gap（组织渠道）
    ops_copilot.seed_authorization()  # 预置一张待授权工单，授权台一进来就有内容


def create_app(
    service: RuntimeService | None = None,
    slice1_service: Slice1Service | None = None,
    workflow_design_service: WorkflowDesignService | None = None,
    analytics_service: AnalyticsService | None = None,
    knowledge_service: KnowledgeService | None = None,
    ops_copilot_service: OpsCopilotService | None = None,
    launch_copilot_service: LaunchCopilotService | None = None,
) -> FastAPI:
    runtime = service or RuntimeService()
    slice1 = slice1_service or Slice1Service(runtime.store.db_path)
    # 发布到飞书：把 ProcessDefinition 翻译成飞书审批定义并（可选）真发
    from app.api.feishu_service import FeishuPublishService

    feishu_publish = FeishuPublishService(slice1)
    # 反哺闭环：共享的运行洞察存储（运维臂写、设计/流程管理读）
    from app.insights import InsightStore

    insight_store = InsightStore()
    workflow_design = workflow_design_service or WorkflowDesignService(
        runtime.store.db_path, insight_store=insight_store
    )
    analytics = analytics_service or AnalyticsService(insight_store=insight_store)
    # P3 报销全套：第二个流程的分析实例，指向自己的合成数据目录，与请假的 AnalyticsService 互不干扰
    expense_analytics = AnalyticsService(PROJECT_ROOT / "data/analytics/expense_reimbursement", insight_store=insight_store)
    knowledge = knowledge_service or KnowledgeService()
    if ops_copilot_service is None:
        def _process_lookup(workflow_definition_id: str) -> ProcessDefinition:
            # 给"待办队列"里没有 InstanceContext 的单据（如审批协助的差旅报销）现建一份
            # 用——读的是已提交/已上架的 definition_json，跟真实在跑的版本一致。
            row = slice1.workflow_definition(workflow_definition_id)
            return ProcessDefinition.model_validate(json.loads(row["definition_json"]))

        ops_copilot = OpsCopilotService(insight_store=insight_store, process_lookup=_process_lookup)
        _seed_ops_demo(ops_copilot)  # demo：近30天4单同因卡住 + 预置一张待授权工单
        # demo：把报销合成数据里确定性检出的效能问题沉淀进洞察层，起始态设计侧就有真实运行
        # 反馈可处理，不是凭空造的问题。请假流程故意不预置分析侧洞察（只留运维反哺的覆盖
        # 漏洞/组织缺口）——留给演示现场走一遍"效能分析对话反哺 + 生成诊断报告"，让「流程
        # 管理」待处理列表在演示过程中真实地多出新条目，而不是一进来就已经铺满。
        expense_analytics.seed_insights()
    else:
        ops_copilot = ops_copilot_service
    launch_copilot = launch_copilot_service or LaunchCopilotService()
    # 流程总览副驾（流程管理页）：路由 + 编排，不重开一套业务能力——批量修复交前端复用单
    # 流程链路；问结构/问效能直接调用已有服务并透传结果
    from app.copilots.manage_service import ManageCopilotService

    manage_copilot = ManageCopilotService(
        slice1_service=slice1,
        workflow_design_service=workflow_design,
        analytics_by_workflow_id={"LEAVE-001": analytics, "EXPENSE-001": expense_analytics},
        launch_service=launch_copilot,
    )
    # 设计评测运行服务：评测模式（选 gold + 载源消融 → 真跑抽取 → 打分入历史）
    from app.eval.eval_run_service import EvalRunService

    eval_runs = EvalRunService()
    # 流程助手（待办副驾）：复用 ops 实例做进度追踪
    from app.copilots.todo_service import TodoCopilotService

    todo_copilot = TodoCopilotService(ops_service=ops_copilot)

    # 演示"起始态"快照 / 一键切回：文件态（runtime.db + analytics 可变 json + 评测历史）
    # 由 DemoStateService 快照还原；内存态（InsightStore + ops 在办实例）由下面这个回调
    # 重建——顺序与上面的启动播种一致，等价于进程刚起来的样子。
    def _rebuild_demo_in_memory() -> None:
        insight_store.clear()
        ops_copilot.reset_demo_state()
        _seed_ops_demo(ops_copilot)
        expense_analytics.seed_insights()

    demo_state = DemoStateService(
        project_root=PROJECT_ROOT,
        db_path=Path(runtime.store.db_path),
        analytics_dirs=[analytics.analytics_dir, expense_analytics.analytics_dir],
        eval_runs_dir=PROJECT_ROOT / "runs/eval_runs",
        rebuild_in_memory=_rebuild_demo_in_memory,
    )

    app = FastAPI(title="Process Management Agent API", version="0.2.3")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if STATIC_ROOT.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")
    if DOC_ROOT.exists():
        app.mount("/doc", StaticFiles(directory=str(DOC_ROOT)), name="doc")

    @app.get("/")
    def index() -> FileResponse:
        # 新前端（以设计稿 copilot-shell 为底 + 数据绑定）现为默认首页
        path = STATIC_ROOT / "app.html"
        if not path.exists():
            raise HTTPException(status_code=404, detail="static app.html not found")
        return FileResponse(path)

    # 别名：/app 仍指向新前端
    @app.get("/app")
    def app_v2() -> FileResponse:
        return index()

    @app.get("/legacy")
    def legacy_index() -> FileResponse:
        # 旧 index.html（换皮版），保留以备对照
        index_path = STATIC_ROOT / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="static index.html not found")
        return FileResponse(index_path)

    @app.post("/api/processes/import-standard")
    def import_standard_processes() -> dict[str, Any]:
        return {"items": runtime.ensure_standard_processes()}

    @app.get("/api/processes")
    def list_processes() -> dict[str, Any]:
        return {"items": runtime.list_processes()}

    @app.get("/api/processes/{process_key}")
    def get_process(process_key: str) -> dict[str, Any]:
        return _handle(lambda: runtime.get_process(process_key))

    @app.get("/api/users")
    def list_users() -> dict[str, Any]:
        return {"items": runtime.list_users()}

    @app.get("/api/org")
    def get_org() -> dict[str, Any]:
        return runtime.get_org()

    @app.post("/api/instances")
    def create_instance(request: CreateInstanceRequest) -> dict[str, Any]:
        return _handle(
            lambda: runtime.create_instance(
                process_key=request.process_key,
                initiator_user_id=request.initiator_user_id,
                form_values=request.form_values,
            )
        )

    @app.get("/api/instances")
    def list_instances() -> dict[str, Any]:
        return {"items": runtime.list_instances()}

    @app.get("/api/instances/{instance_id}")
    def get_instance(instance_id: str) -> dict[str, Any]:
        return _handle(lambda: runtime.get_instance(instance_id))

    @app.get("/api/tasks")
    def list_tasks(assignee_id: str | None = Query(default=None)) -> dict[str, Any]:
        return {"items": runtime.tasks(assignee_id=assignee_id)}

    @app.post("/api/available-paths")
    def available_paths(request: AvailablePathsRequest) -> dict[str, Any]:
        return _handle(
            lambda: {
                "items": runtime.available_paths(
                    instance_id=request.instance_id,
                    task_id=request.task_id,
                    conclusive_opinion=request.conclusive_opinion,
                    context_flags=request.context_flags,
                )
            }
        )

    @app.post("/api/tasks/{task_id}/complete")
    def complete_task(task_id: str, request: CompleteTaskRequest) -> dict[str, Any]:
        return _handle(
            lambda: runtime.complete_task(
                task_id=task_id,
                actor_user_id=request.actor_user_id,
                path_name=request.path_name,
                conclusive_opinion=request.conclusive_opinion,
                form_updates=request.form_updates,
                comment=request.comment,
                context_flags=request.context_flags,
            )
        )

    @app.get("/api/v1/users")
    def v1_list_users() -> dict[str, Any]:
        return {"items": slice1.list_users()}

    @app.post("/api/v1/session/mock")
    def v1_mock_session(request: MockSessionRequest) -> dict[str, Any]:
        return _handle(lambda: slice1.create_mock_session(request.user_id))

    @app.post("/api/v1/session/login")
    def v1_login_session(request: LoginSessionRequest) -> dict[str, Any]:
        return _handle(lambda: slice1.login_session(account=request.account, password=request.password))

    @app.get("/api/v1/workbench/summary")
    def v1_workbench_summary(user_id: str = Query(...)) -> dict[str, Any]:
        return _handle(lambda: slice1.workbench_summary(user_id))

    @app.get("/api/v1/workbench/items")
    def v1_workbench_items(user_id: str = Query(...), bucket: str = Query(...)) -> dict[str, Any]:
        return _handle(lambda: {"items": slice1.workbench_items(user_id=user_id, bucket=bucket)})

    @app.get("/api/v1/workbench/initiated")
    def v1_workbench_initiated(user_id: str = Query(...)) -> dict[str, Any]:
        return _handle(lambda: {"items": slice1.initiated_cases(user_id)})

    @app.get("/api/v1/workflows/catalog")
    def v1_workflow_catalog(user_id: str | None = Query(default=None)) -> dict[str, Any]:
        return _handle(lambda: {"items": slice1.workflow_catalog(user_id=user_id)})

    @app.get("/api/v1/workflow-definitions")
    def v1_workflow_definitions(user_id: str | None = Query(default=None)) -> dict[str, Any]:
        return _handle(lambda: {"items": _with_design_overlays(slice1.workflow_definitions(user_id=user_id), workflow_design)})

    @app.get("/api/v1/workflow-definitions/{workflow_definition_id}")
    def v1_workflow_definition_detail(
        workflow_definition_id: str,
        user_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        return _handle(
            lambda: _with_design_overlay(
                slice1.workflow_definition_detail(workflow_definition_id, user_id=user_id),
                workflow_design,
            )
        )

    @app.delete("/api/v1/workflow-definitions/{workflow_definition_id}")
    def v1_delete_workflow_definition(workflow_definition_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.delete_workflow_definition(workflow_definition_id=workflow_definition_id))

    @app.post("/api/v1/workflow-definitions/{workflow_definition_id}/publish")
    def v1_publish_workflow_definition(workflow_definition_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.publish_workflow_definition(workflow_definition_id=workflow_definition_id))

    @app.post("/api/v1/workflow-definitions/{workflow_definition_id}/unpublish")
    def v1_unpublish_workflow_definition(workflow_definition_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.unpublish_workflow_definition(workflow_definition_id=workflow_definition_id))

    # 发布到飞书：翻译预览（免费、无副作用）+ 真发（创建飞书审批定义）
    @app.get("/api/v1/workflow-definitions/{workflow_definition_id}/feishu/preview")
    def v1_feishu_preview(workflow_definition_id: str) -> dict[str, Any]:
        return _handle(lambda: feishu_publish.preview(workflow_definition_id))

    @app.post("/api/v1/workflow-definitions/{workflow_definition_id}/feishu/push")
    def v1_feishu_push(workflow_definition_id: str) -> dict[str, Any]:
        return _handle(lambda: feishu_publish.push(workflow_definition_id))

    @app.post("/api/v1/workflow-design/sessions")
    def v1_workflow_design_session(request: WorkflowDesignSessionRequest) -> dict[str, Any]:
        return _handle(
            lambda: workflow_design.create_or_open_session(
                workflow_definition_id=request.workflow_definition_id,
                created_by=request.created_by,
                mode=request.mode,
            )
        )

    @app.post("/api/v1/workflow-design/initialize")
    def v1_workflow_design_initialize(request: WorkflowDesignInitializeRequest) -> dict[str, Any]:
        return _handle(
            lambda: workflow_design.initialize_new_session(
                created_by=request.created_by,
                workflow_type=request.workflow_type,
                workflow_name=request.workflow_name,
                category=request.category,
                instruction=request.instruction,
                sources=[source.model_dump() for source in request.sources],
            )
        )

    @app.post("/api/v1/workflow-design/initialization-jobs")
    def v1_workflow_design_initialization_job(request: WorkflowDesignInitializeRequest) -> dict[str, Any]:
        return _handle(
            lambda: workflow_design.create_initialization_job(
                created_by=request.created_by,
                workflow_type=request.workflow_type,
                workflow_name=request.workflow_name,
                category=request.category,
                instruction=request.instruction,
                sources=[source.model_dump() for source in request.sources],
            )
        )

    @app.post("/api/v1/workflow-design/initialize-files")
    async def v1_workflow_design_initialize_files(request: Request) -> dict[str, Any]:
        payload = await _parse_workflow_design_multipart(request)
        return _handle(
            lambda: workflow_design.initialize_new_session(
                created_by=payload["created_by"],
                workflow_type=payload["workflow_type"],
                workflow_name=payload["workflow_name"],
                category=payload["category"],
                instruction=payload["instruction"],
                sources=payload["sources"],
                uploaded_files=payload["files"],
            )
        )

    @app.post("/api/v1/workflow-design/initialization-file-jobs")
    async def v1_workflow_design_initialization_file_job(request: Request) -> dict[str, Any]:
        payload = await _parse_workflow_design_multipart(request)
        return _handle(
            lambda: workflow_design.create_initialization_job(
                created_by=payload["created_by"],
                workflow_type=payload["workflow_type"],
                workflow_name=payload["workflow_name"],
                category=payload["category"],
                instruction=payload["instruction"],
                sources=payload["sources"],
                uploaded_files=payload["files"],
            )
        )

    @app.get("/api/v1/workflow-design/initialization-jobs/{job_id}")
    def v1_workflow_design_initialization_job_detail(job_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.initialization_job_payload(job_id))

    @app.get("/api/v1/workflow-design/initialization-jobs/{job_id}/events")
    def v1_workflow_design_initialization_job_events(job_id: str) -> StreamingResponse:
        _handle(lambda: workflow_design.initialization_job_payload(job_id))
        return StreamingResponse(
            workflow_design.stream_initialization_job_events(job_id),
            media_type="text/event-stream",
        )

    @app.get("/api/v1/workflow-design/sessions/{session_id}")
    def v1_workflow_design_session_detail(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.session_payload(session_id))

    @app.delete("/api/v1/workflow-design/sessions/{session_id}")
    def v1_workflow_design_delete_session(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.delete_session(session_id=session_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/sources")
    def v1_workflow_design_add_source(session_id: str, request: WorkflowDesignSourceRequest) -> dict[str, Any]:
        return _handle(
            lambda: workflow_design.add_source(
                session_id=session_id,
                title=request.title,
                content=request.content,
                created_by=request.created_by,
                source_type=request.source_type,
            )
        )

    @app.post("/api/v1/workflow-design/sessions/{session_id}/source-files")
    async def v1_workflow_design_add_source_files(session_id: str, request: Request) -> dict[str, Any]:
        payload = await _parse_workflow_design_multipart(request)
        return _handle(
            lambda: workflow_design.add_source_files(
                session_id=session_id,
                files=payload["files"],
                created_by=payload["created_by"],
            )
        )

    @app.delete("/api/v1/workflow-design/sessions/{session_id}/sources/{source_id}")
    def v1_workflow_design_delete_source(session_id: str, source_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.delete_source(session_id=session_id, source_id=source_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/messages")
    def v1_workflow_design_add_message(session_id: str, request: WorkflowDesignMessageRequest) -> dict[str, Any]:
        return _handle(lambda: workflow_design.add_message(session_id=session_id, role=request.role, content=request.content))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/generate-draft")
    def v1_workflow_design_generate_draft(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.generate_draft(session_id=session_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/undo")
    def v1_workflow_design_undo(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.undo_last_change(session_id=session_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/confirm-pending-edit")
    def v1_workflow_design_confirm_pending_edit(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.confirm_pending_edit(session_id=session_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/discard-pending-edit")
    def v1_workflow_design_discard_pending_edit(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.discard_pending_edit(session_id=session_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/validate")
    def v1_workflow_design_validate(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.validate_session(session_id=session_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/compliance")
    def v1_workflow_design_compliance(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.compliance_report(session_id=session_id))

    @app.post("/api/v1/workflow-design/sessions/{session_id}/save")
    def v1_workflow_design_save(session_id: str) -> dict[str, Any]:
        return _handle(lambda: workflow_design.save_session(session_id=session_id))

    @app.get("/api/v1/workflows/{workflow_definition_id}/launch-form")
    def v1_launch_form(workflow_definition_id: str, user_id: str | None = Query(default=None)) -> dict[str, Any]:
        return _handle(lambda: slice1.launch_form(workflow_definition_id, user_id=user_id))

    @app.post("/api/v1/cases")
    def v1_create_case(request: CreateWorkflowCaseRequest) -> dict[str, Any]:
        return _handle(
            lambda: slice1.create_case(
                workflow_definition_id=request.workflow_definition_id,
                initiator_user_id=request.initiator_user_id,
                form_values=request.form_values,
            )
        )

    @app.get("/api/v1/cases/{case_id}")
    def v1_case_detail(case_id: str, user_id: str | None = Query(default=None)) -> dict[str, Any]:
        return _handle(lambda: slice1.case_detail(case_id, user_id=user_id))

    @app.patch("/api/v1/cases/{case_id}/form")
    def v1_update_case_form(case_id: str, request: UpdateWorkflowCaseFormRequest) -> dict[str, Any]:
        return _handle(
            lambda: slice1.update_case_form(
                case_id=case_id,
                actor_user_id=request.actor_user_id,
                form_values=request.form_values,
            )
        )

    @app.post("/api/v1/cases/{case_id}/submit")
    def v1_submit_case(case_id: str, request: SubmitWorkflowCaseRequest) -> dict[str, Any]:
        return _handle(
            lambda: slice1.submit_case(
                case_id=case_id,
                actor_user_id=request.actor_user_id,
                form_values=request.form_values,
                selected_edge_id=request.selected_edge_id,
                comment=request.comment,
            )
        )

    @app.post("/api/v1/work-items/{work_item_id}/route-options")
    def v1_route_options(work_item_id: str, request: Slice1RouteOptionsRequest) -> dict[str, Any]:
        return _handle(
            lambda: {
                "items": slice1.route_options(
                    work_item_id=work_item_id,
                    actor_user_id=request.actor_user_id,
                    decision=request.decision,
                )
            }
        )

    @app.post("/api/v1/work-items/{work_item_id}/complete")
    def v1_complete_work_item(work_item_id: str, request: Slice1CompleteWorkItemRequest) -> dict[str, Any]:
        return _handle(
            lambda: slice1.complete_work_item(
                work_item_id=work_item_id,
                actor_user_id=request.actor_user_id,
                decision=request.decision,
                selected_edge_id=request.selected_edge_id,
                path_name=request.path_name,
                comment=request.comment,
            )
        )

    @app.get("/api/v1/analytics/leave-request/metrics")
    def v1_analytics_leave_request_metrics() -> dict[str, Any]:
        return _handle(analytics.dashboard_metrics)

    @app.get("/api/v1/analytics/leave-request/diagnosis")
    def v1_analytics_leave_request_diagnosis_overview() -> dict[str, Any]:
        return _handle(analytics.diagnosis_overview)

    @app.get("/api/v1/analytics/leave-request/diagnosis/{report_id}")
    def v1_analytics_leave_request_diagnosis_detail(report_id: str) -> dict[str, Any]:
        return _handle(lambda: {"report": analytics.get_diagnosis(report_id)})

    @app.post("/api/v1/analytics/leave-request/diagnosis")
    def v1_analytics_leave_request_diagnosis() -> dict[str, Any]:
        return _handle(analytics.diagnosis_report)

    @app.get("/api/v1/analytics/leave-request/version-comparison")
    def v1_analytics_leave_request_version_comparison() -> dict[str, Any]:
        return _handle(analytics.version_comparison)

    @app.get("/api/v1/analytics/expense-reimbursement/metrics")
    def v1_analytics_expense_reimbursement_metrics() -> dict[str, Any]:
        return _handle(expense_analytics.dashboard_metrics)

    # ——— 设计评测（只读：读 run_eval_summary.py 上次产出的 runs/eval_summary 快照）———
    @app.get("/api/v1/eval/summary")
    def v1_eval_summary() -> dict[str, Any]:
        def _read() -> dict[str, Any]:
            path = PROJECT_ROOT / "runs/eval_summary/eval_summary.json"
            if not path.exists():
                return {"available": False, "cases": []}
            data = json.loads(path.read_text(encoding="utf-8"))
            data["available"] = True
            return data
        return _handle(_read)

    # ——— 评测模式：案例/源清单、运行历史、评测运行任务（选 gold + 载源消融 → 真跑 → 打分入历史） ———
    @app.get("/api/v1/eval/run-cases")
    def v1_eval_run_cases() -> dict[str, Any]:
        return _handle(eval_runs.list_cases)

    @app.get("/api/v1/eval/cases/{case_id}/sources")
    def v1_eval_case_sources(case_id: str) -> dict[str, Any]:
        return _handle(lambda: eval_runs.list_case_sources(case_id))

    @app.get("/api/v1/eval/cases/{case_id}/history")
    def v1_eval_case_history(case_id: str) -> dict[str, Any]:
        return _handle(lambda: eval_runs.list_runs(case_id))

    @app.post("/api/v1/eval/run")
    async def v1_eval_run(request: Request) -> dict[str, Any]:
        # 评测模式 = 正常设计初始化 + 额外打分。走同一套 workflow-design 初始化任务（照建卡片、
        # 同一个 SSE 进度端点），完成后对 gold 打分入历史；卡片和评测运行共享流程定义 id。
        payload = await _parse_eval_run_multipart(request)
        from app.eval import eval_run_service as _eval_mod

        meta = _eval_mod.case_meta(payload["case_id"]) or {}
        case_name = meta.get("name") or payload["case_id"]
        # 名字不加「· 评测」后缀——这个流程可能被上架到发起申请，名字要跟正常流程一样干净；
        # 评测来源改用 launch_scope.source="eval_run" 标记，前端渲染成名字右侧的小标签。
        return _handle(
            lambda: workflow_design.create_initialization_job(
                created_by=payload["created_by"] or "u_it_app_staff",
                workflow_type="approval",
                workflow_name=case_name,
                category="审批流程",
                instruction=payload["instruction"],
                sources=[],
                uploaded_files=payload["files"],
                eval_case_id=payload["case_id"],
                excluded_case_sources=payload["excluded_sources"],
            )
        )

    def _eval_report_base(safe: str, run_id: str | None) -> Path:
        """定位某次评测报告目录：baseline→eval_summary 快照，否则→eval_runs 运行历史。"""
        if not run_id or run_id == "baseline":
            return PROJECT_ROOT / "runs/eval_summary" / safe
        safe_run = "".join(c for c in run_id if c.isalnum() or c in {"_", "-"})
        return PROJECT_ROOT / "runs/eval_runs" / safe / safe_run

    @app.get("/api/v1/eval/cases/{case_id}")
    def v1_eval_case(case_id: str, run_id: str | None = None) -> dict[str, Any]:
        # 防目录穿越：case_id 只允许字母数字下划线
        safe = "".join(c for c in case_id if c.isalnum() or c == "_")
        def _read() -> dict[str, Any]:
            base = _eval_report_base(safe, run_id)
            path = base / "evaluation_report.json"
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"评测报告不存在：{safe}")
            report = json.loads(path.read_text(encoding="utf-8"))
            # 附上完整的金标准 + 模型抽取定义，供前端做两块属性明细对照 + node_id→中文名解析
            def _def(fname: str) -> dict[str, Any]:
                fp = base / fname
                if not fp.exists():
                    return {}
                data = json.loads(fp.read_text(encoding="utf-8"))
                return data.get("deterministic_process_definition", data) if isinstance(data, dict) else {}
            report["gold_definition"] = _def("gold_target_snapshot.json")
            report["actual_definition"] = _def("actual_eval_target.json")
            return report
        return _handle(_read)

    @app.post("/api/v1/eval/cases/{case_id}/semantic-judge")
    def v1_eval_semantic_judge(case_id: str, run_id: str | None = None) -> dict[str, Any]:
        # 语义复评（可选 LLM 层）：把每处字面差异交 LLM 判"语义是否等价"，据此估算语义校正分。
        # 确定性主分不变，这里只叠加一个二次视图。
        safe = "".join(c for c in case_id if c.isalnum() or c == "_")

        def _run() -> dict[str, Any]:
            from app.eval.semantic_judge import judge_diffs

            path = _eval_report_base(safe, run_id) / "evaluation_report.json"
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"评测报告不存在：{safe}")
            report = json.loads(path.read_text(encoding="utf-8"))
            cr = report.get("comparison_report", {})
            det = (report.get("deterministic_eval") or {}).get("details", {})

            # 展平所有属性差异，带 dim 归属
            diffs: list[dict[str, Any]] = []
            i = 0
            for fd in cr.get("field_differences", []):
                for d in fd.get("differences", []):
                    diffs.append({"id": i, "dim": "form_fields", "location": f"字段「{fd.get('field_name')}」",
                                  "attribute": d.get("attribute"), "expected": d.get("expected"), "actual": d.get("actual")})
                    i += 1
            for nd in cr.get("node_differences", []):
                for d in nd.get("differences", []):
                    diffs.append({"id": i, "dim": "flow_nodes", "location": f"环节「{nd.get('node_id')}」",
                                  "attribute": d.get("attribute"), "expected": d.get("expected"), "actual": d.get("actual")})
                    i += 1
            for pd in cr.get("path_condition_differences", []):
                diffs.append({"id": i, "dim": "submit_paths", "location": f"路径「{pd.get('path_name')}」",
                              "attribute": "condition", "expected": pd.get("expected_condition"), "actual": pd.get("actual_condition")})
                i += 1

            verdicts = judge_diffs(diffs)
            if verdicts is None:
                return {"available": False, "reason": "语义裁判模型未就绪"}

            judged = []
            equiv_by_dim: dict[str, int] = {}
            for d in diffs:
                v = verdicts.get(d["id"])
                eq = bool(v.equivalent) if v else False
                if eq:
                    equiv_by_dim[d["dim"]] = equiv_by_dim.get(d["dim"], 0) + 1
                judged.append({**d, "equivalent": eq, "reason": (v.reason if v else "")})

            # 语义校正分（估算）：把判为等价的差异当作抽对，重算各维度属性准确 → 重算维度分
            diff_by_dim: dict[str, int] = {}
            for d in diffs:
                diff_by_dim[d["dim"]] = diff_by_dim.get(d["dim"], 0) + 1
            raw_det_total = sum(v.get("score", 0) for v in det.values())
            adjusted_det_total = 0.0
            dim_adjust: dict[str, dict[str, float]] = {}
            for dim, dd in det.items():
                score = dd.get("score", 0.0)
                weight = dd.get("weight", 0.0)
                a = dd.get("attribute_accuracy")
                D = diff_by_dim.get(dim, 0)
                K = equiv_by_dim.get(dim, 0)
                new_score = score
                if a is not None and a < 1.0 and D > 0 and K > 0:
                    total_attrs = D / (1.0 - a)  # 由 attr_acc 反推该维度总属性点
                    new_a = 1.0 - max(0.0, (D - K)) / total_attrs if total_attrs else a
                    recall = dd.get("recall", 1.0)
                    precision = dd.get("precision", 1.0)
                    new_score = weight * (0.60 * recall + 0.30 * new_a + 0.10 * precision)
                    dim_adjust[dim] = {"raw": round(score, 2), "adjusted": round(new_score, 2), "equivalent": K, "diff": D}
                adjusted_det_total += new_score

            other = (report.get("auto_score", {}).get("total", 0)) - raw_det_total  # clarification + guardrail 不变
            return {
                "available": True,
                "case_id": safe,
                "total_diffs": len(diffs),
                "equivalent_count": sum(1 for j in judged if j["equivalent"]),
                "judged": judged,
                "raw_total": round(report.get("auto_score", {}).get("total", 0), 2),
                "adjusted_total": round(adjusted_det_total + other, 2),
                "dim_adjust": dim_adjust,
            }

        return _handle(_run)

    @app.get("/api/v1/knowledge/rules")
    def v1_knowledge_rules() -> dict[str, Any]:
        return _handle(knowledge.rules_overview)

    @app.get("/api/v1/knowledge/documents")
    def v1_knowledge_documents() -> dict[str, Any]:
        return _handle(knowledge.documents_overview)

    @app.post("/api/v1/knowledge/search")
    def v1_knowledge_search(request: AnalyticsQuestionRequest) -> dict[str, Any]:
        return _handle(lambda: knowledge.search(request.message))

    @app.get("/api/v1/knowledge/documents/{doc_id}")
    def v1_knowledge_document(doc_id: str) -> dict[str, Any]:
        return _handle(lambda: knowledge.get_document(doc_id))

    # ——— 反哺闭环：运行洞察读取（供流程管理「运行反馈」+ 设计工作台 banner）———
    @app.get("/api/v1/insights")
    def v1_insights(workflow_definition_id: str) -> dict[str, Any]:
        return _handle(lambda: {
            "workflow_definition_id": workflow_definition_id,
            "items": [i.model_dump() for i in ops_copilot.insights_for(workflow_definition_id)],
            "channel_counts": ops_copilot.insight_channel_counts(workflow_definition_id),
        })

    # ——— 流程助手（待办副驾）———
    @app.get("/api/v1/copilot/todo")
    def v1_todo_worklist() -> dict[str, Any]:
        return _handle(todo_copilot.worklist)

    @app.post("/api/v1/copilot/todo/ask")
    def v1_todo_ask(request: AnalyticsQuestionRequest) -> dict[str, Any]:
        # 列表级导航/问答：基于确定性排好的待办清单回答（哪些急/几单卡住/某单情况），或识别
        # "打开某一单"的意图返回 target_instance_id 供前端跳转。具体办理/排障点进单据交给在办副驾。
        history = [item.model_dump() for item in request.history]
        return _handle(lambda: todo_copilot.ask(request.message, history=history))

    @app.get("/api/v1/copilot/todo/{item_id}/assist")
    def v1_todo_assist(item_id: str) -> dict[str, Any]:
        return _handle(lambda: todo_copilot.approval_assist(item_id))

    @app.post("/api/v1/copilot/todo/{item_id}/ensure_instance")
    def v1_todo_ensure_instance(item_id: str) -> dict[str, Any]:
        # 打开一条待办队列条目时先调这个：把它桥接成真正的 ops 实例（InstanceContext），
        # 之后就能直接用 item_id 当 instance_id 调 /copilot/instances/{id}/* 那一整套
        # （inspect/diagnose/propose/confirm/discard）——办理和排障共用同一套 harness。
        return _handle(lambda: {"diagnosable": todo_copilot.ensure_ops_instance(item_id)})

    @app.get("/api/v1/copilot/todo/{instance_id}/progress")
    def v1_todo_progress(instance_id: str) -> dict[str, Any]:
        return _handle(lambda: todo_copilot.progress(instance_id))

    @app.post("/api/v1/copilot/todo/{item_id}/reset")
    def v1_todo_reset(item_id: str) -> dict[str, Any]:
        # 演示用：撤销运维/在办副驾对这条单据做过的任何修改，回到最初模拟状态——
        # 供反复演示"agent 帮我修复"时，不用重启进程就能把单据打回原样再试一遍。
        return _handle(lambda: {"diagnosable": todo_copilot.reset_item(item_id)})

    @app.post("/api/v1/copilot/todo/reset_all")
    def v1_todo_reset_all() -> dict[str, Any]:
        # 演示用：一键把「我的流程」列表里所有单据都撤销修改、回到最初模拟状态。
        return _handle(lambda: {"reset_count": todo_copilot.reset_all()})

    # ——— 演示"起始态"：全局快照 / 一键切回（比上面按流程/按单据的重置更广，
    #      覆盖设计会话/流程发布状态/分析诊断历史/评测历史 + 全部内存态）———
    @app.get("/api/v1/demo/state")
    def v1_demo_state() -> dict[str, Any]:
        return _handle(demo_state.status)

    @app.post("/api/v1/demo/freeze")
    def v1_demo_freeze() -> dict[str, Any]:
        # 把"当前现场"定为演示起始态（覆盖旧基线）——想让演示从一个精心摆好的状态开始时点。
        return _handle(demo_state.freeze)

    @app.post("/api/v1/demo/restore")
    def v1_demo_restore() -> dict[str, Any]:
        # 一键切回起始态：文件回滚 + 内存态重建，可反复；供演示间隙把现场清回起点再演一遍。
        return _handle(demo_state.restore)

    @app.get("/api/v1/copilot/instances")
    def v1_copilot_instances() -> dict[str, Any]:
        return _handle(ops_copilot.list_view)

    @app.get("/api/v1/copilot/instances/{instance_id}")
    def v1_copilot_instance(instance_id: str) -> dict[str, Any]:
        return _handle(lambda: ops_copilot.instance_view(instance_id))

    @app.get("/api/v1/copilot/instances/{instance_id}/inspect")
    def v1_copilot_inspect(instance_id: str) -> dict[str, Any]:
        return _handle(lambda: ops_copilot.inspect(instance_id))

    @app.get("/api/v1/copilot/instances/{instance_id}/diagnose")
    def v1_copilot_diagnose(instance_id: str) -> dict[str, Any]:
        return _handle(lambda: ops_copilot.diagnose(instance_id))

    @app.post("/api/v1/copilot/instances/{instance_id}/propose")
    def v1_copilot_propose(instance_id: str, request: AnalyticsQuestionRequest) -> dict[str, Any]:
        history = [item.model_dump() for item in request.history]
        return _handle(lambda: ops_copilot.propose(instance_id, request.message, history=history).model_dump())

    @app.post("/api/v1/copilot/instances/{instance_id}/confirm")
    def v1_copilot_confirm(instance_id: str) -> dict[str, Any]:
        return _handle(lambda: ops_copilot.confirm(instance_id).model_dump())

    @app.post("/api/v1/copilot/instances/{instance_id}/discard")
    def v1_copilot_discard(instance_id: str) -> dict[str, Any]:
        return _handle(lambda: {"discarded": ops_copilot.discard(instance_id) or True})

    @app.get("/api/v1/copilot/authorizations")
    def v1_copilot_authorizations() -> dict[str, Any]:
        return _handle(ops_copilot.list_authorizations)

    @app.post("/api/v1/copilot/authorizations/{request_id}/approve")
    def v1_copilot_authorize_approve(request_id: str) -> dict[str, Any]:
        return _handle(lambda: ops_copilot.authorize(request_id, approve=True).model_dump())

    @app.post("/api/v1/copilot/authorizations/{request_id}/reject")
    def v1_copilot_authorize_reject(request_id: str) -> dict[str, Any]:
        return _handle(lambda: ops_copilot.authorize(request_id, approve=False).model_dump())

    @app.get("/api/v1/copilot/launch/catalog")
    def v1_copilot_launch_catalog() -> dict[str, Any]:
        return _handle(launch_copilot.catalog_view)

    @app.post("/api/v1/copilot/launch/ask")
    def v1_copilot_launch_ask(request: AnalyticsQuestionRequest) -> dict[str, Any]:
        return _handle(lambda: launch_copilot.ask(request.message).model_dump())

    @app.post("/api/v1/copilot/manage/ask")
    def v1_copilot_manage_ask(request: AnalyticsQuestionRequest) -> dict[str, Any]:
        history = [item.model_dump() for item in request.history]
        return _handle(lambda: manage_copilot.ask(request.message, history=history))

    @app.post("/api/v1/analytics/leave-request/ask")
    def v1_analytics_leave_request_ask(request: AnalyticsQuestionRequest) -> dict[str, Any]:
        return _handle(
            lambda: analytics.answer_question(
                message=request.message,
                history=[item.model_dump() for item in request.history],
            )
        )

    return app


def _handle(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _parse_workflow_design_multipart(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    boundary = _multipart_boundary(content_type)
    if not boundary:
        raise HTTPException(status_code=400, detail="multipart/form-data boundary missing")
    body = await request.body()
    fields: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    delimiter = b"--" + boundary
    for raw_part in body.split(delimiter):
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        header_blob, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = _multipart_headers(header_blob)
        disposition = headers.get("content-disposition", "")
        params = _content_disposition_params(disposition)
        name = params.get("name")
        filename = params.get("filename")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if filename:
            files.append(
                {
                    "filename": filename,
                    "content_type": headers.get("content-type"),
                    "size_bytes": len(data),
                    "content": data,
                }
            )
        elif name:
            fields[name] = data.decode("utf-8", errors="replace")

    sources = _json_field(fields.get("sources_json"), [])
    text_sources = _json_field(fields.get("text_sources_json"), [])
    if isinstance(text_sources, list):
        sources.extend(text_sources)
    return {
        "created_by": fields.get("created_by", ""),
        "workflow_type": fields.get("workflow_type", "approval"),
        "workflow_name": fields.get("workflow_name", ""),
        "category": fields.get("category", "审批流程"),
        "instruction": fields.get("instruction", ""),
        "sources": sources,
        "files": files,
    }


async def _parse_eval_run_multipart(request: Request) -> dict[str, Any]:
    """评测运行入参：case_id + excluded_sources(JSON数组) + 可选补料文件 + instruction。"""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        # 允许纯 JSON（无补料文件时）
        body = await request.json()
        return {
            "created_by": body.get("created_by", ""),
            "case_id": body.get("case_id", ""),
            "excluded_sources": list(body.get("excluded_sources", []) or []),
            "instruction": body.get("instruction", ""),
            "files": [],
        }
    boundary = _multipart_boundary(content_type)
    if not boundary:
        raise HTTPException(status_code=400, detail="multipart/form-data boundary missing")
    body = await request.body()
    fields: dict[str, str] = {}
    files: list[dict[str, Any]] = []
    delimiter = b"--" + boundary
    for raw_part in body.split(delimiter):
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        header_blob, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = _multipart_headers(header_blob)
        params = _content_disposition_params(headers.get("content-disposition", ""))
        name = params.get("name")
        filename = params.get("filename")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if filename:
            files.append({"filename": filename, "content_type": headers.get("content-type"),
                          "size_bytes": len(data), "content": data})
        elif name:
            fields[name] = data.decode("utf-8", errors="replace")
    return {
        "created_by": fields.get("created_by", ""),
        "case_id": fields.get("case_id", ""),
        "excluded_sources": _json_field(fields.get("excluded_sources_json"), []),
        "instruction": fields.get("instruction", ""),
        "files": files,
    }


def _multipart_boundary(content_type: str) -> bytes | None:
    for part in content_type.split(";"):
        item = part.strip()
        if item.startswith("boundary="):
            value = item.removeprefix("boundary=").strip().strip('"')
            return value.encode("utf-8")
    return None


def _multipart_headers(header_blob: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
        name, separator, value = line.partition(":")
        if separator:
            headers[name.strip().lower()] = value.strip()
    return headers


def _content_disposition_params(value: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in value.split(";"):
        item = part.strip()
        if "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        params[key.strip().lower()] = raw_value.strip().strip('"')
    return params


def _json_field(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON multipart field: {exc}") from exc


def _with_design_overlays(items: list[dict[str, Any]], workflow_design: WorkflowDesignService) -> list[dict[str, Any]]:
    return [_with_design_overlay(item, workflow_design) for item in items]


def _with_design_overlay(item: dict[str, Any], workflow_design: WorkflowDesignService) -> dict[str, Any]:
    summary = workflow_design.design_summary_for_workflow(item["workflow_definition_id"])
    if not summary:
        return item
    payload = {**item}
    management = {**payload.get("management", {})}
    management.update(
        {
            "draft_state": summary["draft_state"],
            "draft_version": summary["draft_version"],
            "draft_progress": summary["draft_progress"],
            "issue_count": summary["issue_count"],
            "health": summary["health"],
            "health_tone": summary["health_tone"],
            "next_action": summary["next_action"],
            "updated_at": summary["updated_at"],
            "design_session_id": summary["session_id"],
            "design_session_mode": summary["mode"],
            "can_delete_draft": summary["can_delete_draft"],
            "is_new_draft": summary["is_new_draft"],
            "base_version": summary["base_version"],
            "missing_time_limit_nodes": summary["missing_time_limit_nodes"],
            "non_time_limit_issue_count": summary["non_time_limit_issue_count"],
        }
    )
    payload["management"] = management
    return payload


app = create_app()
