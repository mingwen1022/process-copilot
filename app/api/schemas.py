from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateInstanceRequest(BaseModel):
    process_key: str
    initiator_user_id: str
    form_values: dict[str, Any] = Field(default_factory=dict)


class AvailablePathsRequest(BaseModel):
    instance_id: str
    task_id: str
    conclusive_opinion: str | None = None
    context_flags: dict[str, Any] = Field(default_factory=dict)


class CompleteTaskRequest(BaseModel):
    actor_user_id: str
    path_name: str
    conclusive_opinion: str | None = None
    form_updates: dict[str, Any] = Field(default_factory=dict)
    comment: str | None = None
    context_flags: dict[str, Any] = Field(default_factory=dict)


class MockSessionRequest(BaseModel):
    user_id: str


class LoginSessionRequest(BaseModel):
    account: str
    password: str


class CreateWorkflowCaseRequest(BaseModel):
    workflow_definition_id: str
    initiator_user_id: str
    form_values: dict[str, Any] = Field(default_factory=dict)


class UpdateWorkflowCaseFormRequest(BaseModel):
    actor_user_id: str
    form_values: dict[str, Any] = Field(default_factory=dict)


class SubmitWorkflowCaseRequest(BaseModel):
    actor_user_id: str
    form_values: dict[str, Any] = Field(default_factory=dict)
    selected_edge_id: str | None = None
    comment: str | None = None


class Slice1RouteOptionsRequest(BaseModel):
    actor_user_id: str
    decision: str | None = None


class Slice1CompleteWorkItemRequest(BaseModel):
    actor_user_id: str
    decision: str
    selected_edge_id: str | None = None
    path_name: str | None = None
    comment: str | None = None


class WorkflowDesignSessionRequest(BaseModel):
    workflow_definition_id: str
    created_by: str
    mode: str = "revise"


class WorkflowDesignInitializeSource(BaseModel):
    title: str
    content: str
    source_type: str = "text"


class WorkflowDesignInitializeRequest(BaseModel):
    created_by: str
    workflow_type: str = "approval"
    workflow_name: str = "未命名审批流程"
    category: str = "审批流程"
    instruction: str = ""
    sources: list[WorkflowDesignInitializeSource] = Field(default_factory=list)


class WorkflowDesignSourceRequest(BaseModel):
    title: str
    content: str
    created_by: str
    source_type: str = "text"


class WorkflowDesignMessageRequest(BaseModel):
    role: str = "user"
    content: str


class AnalyticsMessage(BaseModel):
    role: str = "user"
    content: str


class AnalyticsQuestionRequest(BaseModel):
    message: str
    history: list[AnalyticsMessage] = Field(default_factory=list)
