# YunOS 文档索引

`docs/` 保存 YunOS 的权威设计、专题契约、安全边界、测试说明、节点关闭和发布报告。

## v10 — Bounded Cognitive Agency

- 总体设计与路线：
  [`design/yunos_v10_bounded_cognitive_agency_baseline.md`](design/yunos_v10_bounded_cognitive_agency_baseline.md)、
  [`design/yunos_authoritative_roadmap.md`](design/yunos_authoritative_roadmap.md)
- ADR、Threat Model 与生产迁移验收计划：
  [`adr/yunos_v10_adr_001_bounded_cognitive_agency.md`](adr/yunos_v10_adr_001_bounded_cognitive_agency.md)、
  [`security/yunos_v10_threat_model.md`](security/yunos_v10_threat_model.md)、
  [`design/yunos_v10_production_migration_acceptance_plan.md`](design/yunos_v10_production_migration_acceptance_plan.md)
- Objective and Intent：[`cognitive_objective_v10_a.md`](cognitive_objective_v10_a.md)
- Plan Graph 与 Capability Registry：
  [`cognitive_plan_graph_v10_b.md`](cognitive_plan_graph_v10_b.md)、
  [`cognitive_capability_registry_v10_b.md`](cognitive_capability_registry_v10_b.md)
- Provider 与 Cognitive Orchestration：
  [`cognitive_provider_registry_v10_c.md`](cognitive_provider_registry_v10_c.md)、
  [`cognitive_orchestration_v10_c.md`](cognitive_orchestration_v10_c.md)、
  [`cognitive_runtime_bridge_v10_c.md`](cognitive_runtime_bridge_v10_c.md)
- Bounded Execution、Authorization 与 Tool Gateway：
  [`bounded_execution_v10_d.md`](bounded_execution_v10_d.md)、
  [`cognitive_authorization_v10_d.md`](cognitive_authorization_v10_d.md)、
  [`tool_gateway_v10_d.md`](tool_gateway_v10_d.md)
- Supervision、Recovery、Agent Security 与 Codex Adapter：
  [`supervision_recovery_v10_d.md`](supervision_recovery_v10_d.md)、
  [`security/yunos_v10_agent_security_v10_d.md`](security/yunos_v10_agent_security_v10_d.md)、
  [`codex_adapter_v10_d.md`](codex_adapter_v10_d.md)
- 简体中文认知任务工作区与移动端只读边界：
  [`cognitive_workspace_v10_e.md`](cognitive_workspace_v10_e.md)
- JSON Schema 与 DESIGN ONLY SQL blueprint：
  [`schemas/yunos_v10_cognitive_agency.schema.json`](schemas/yunos_v10_cognitive_agency.schema.json)、
  [`sql/yunos_v10_cognitive_agency_blueprint.sql`](sql/yunos_v10_cognitive_agency_blueprint.sql)
- v10-F 生产迁移、真实验收与生产接纳关闭：
  [`version_reports/yunos_v10_f_production_migration.md`](version_reports/yunos_v10_f_production_migration.md)、
  [`version_reports/yunos_v10_f_real_acceptance.md`](version_reports/yunos_v10_f_real_acceptance.md)、
  [`version_reports/yunos_v10_f_production_adoption_closure.md`](version_reports/yunos_v10_f_production_adoption_closure.md)
- v10.0 最终发布：
  [`releases/yunos_v10_0_bounded_cognitive_agency.md`](releases/yunos_v10_0_bounded_cognitive_agency.md)

v10.0 至 v10-F 的节点关闭报告位于 `version_reports/`。最终 recovery package、独立恢复、hash
与 backup-set digest 必须进入正式 manifest/acceptance evidence；只有无法写入其自身 tracked
提交的最终 `main`、annotated tag object 等自引用封板值由 Git 忽略的 operator evidence 保存。

## v9 — Personal Operational State

- 设计基线：[`design/yunos_v9_operational_state_baseline.md`](design/yunos_v9_operational_state_baseline.md)
- Now/Resume 与只读 API：[`operational_state_context_v9_c.md`](operational_state_context_v9_c.md)
- Delegation 与 Outcome：[`operational_state_delegation_v9_d.md`](operational_state_delegation_v9_d.md)
- Operational Control Surface：[`operational_control_surface_v9_e.md`](operational_control_surface_v9_e.md)
- 生产接纳：[`yunos_v9_production_adoption_v9_f.md`](yunos_v9_production_adoption_v9_f.md)
- 最终节点关闭：[`version_reports/yunos_v9_f_final_closure.md`](version_reports/yunos_v9_f_final_closure.md)
- v9.0.1 起跑门禁：[`version_reports/yunos_v9_0_1_final_full_regression.md`](version_reports/yunos_v9_0_1_final_full_regression.md)

## v7/v8 基础层

- v7 Memory Package：[`memory_package_contract_v7_0.md`](memory_package_contract_v7_0.md)
- v7 Evidence Selection：[`evidence_selection_v7_1.md`](evidence_selection_v7_1.md)
- v7 Temporal Memory：[`temporal_memory_candidates_v7_2.md`](temporal_memory_candidates_v7_2.md)
- v7 Context Assembly：[`context_assembly_v7_3.md`](context_assembly_v7_3.md)
- v7 Transparent Context：[`transparent_context_delivery_v7_4.md`](transparent_context_delivery_v7_4.md)
- v8 Provider/Runtime 起点：[`deepseek_runtime_thin_slice_v8_0_bc.md`](deepseek_runtime_thin_slice_v8_0_bc.md)
- v8 Runtime 授权与恢复：
  [`production_authorization_fidelity_recovery_v8_0_d2.md`](production_authorization_fidelity_recovery_v8_0_d2.md)、
  [`fixed_production_authority_boundary_v8_0_d2_1.md`](fixed_production_authority_boundary_v8_0_d2_1.md)
- v8 最终发布：[`releases/yunos_v8_final_release.md`](releases/yunos_v8_final_release.md)

更早版本文档继续作为历史证据保留；它们不覆盖当前 v10 权威路线。
