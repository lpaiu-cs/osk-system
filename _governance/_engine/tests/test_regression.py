"""osk 회귀 수트 — 검토 세션(2026-08-02)의 적대 시나리오를 영속 고정.

격리 원칙 (검토 3차 지적 4): 수트 전체가 임시 mini-vault를 OSK_VAULT_ROOT로
가리키는 **자기 프로세스** 안에서 돈다 — 실 vault는 읽지도 쓰지도 않고,
전역(VAULT.signatures 등)의 재대입·모듈 reload도 하지 않는다. sync 시험은
별도 임시 git 저장소, 보호영역 생애 fixture는 별도 subprocess에서 돈다.

실행: cd <vault> && .venv/bin/python _engine/tests/test_regression.py
"""
from __future__ import annotations
import hashlib, json, sys, traceback
from pathlib import Path

from regression.support import check, FAIL, _LEAK_BEFORE, MINI, PASS, REAL_TMP, ROOT, RUN_TMP, SKIP, _TMP
from regression.ledger import (
    test_anchor_no_order_fallback, test_conflict_candidates, test_conflicts_open_case_path, test_conflicts_semantics,
    test_cycle_normalization, test_fingerprint_move, test_fork_failclosed_and_reseal,
    test_jsonl_readers_line_separators, test_ledger_corruption_resilience, test_ledger_damage_is_a_kind,
    test_ledger_line_boundaries, test_ledger_row_shape, test_ledger_schema_segment, test_ledger_separators_candidate,
    test_ledger_separators_move, test_path_reuse, test_rid_monotone, test_ridless_unsign_not_swallowed,
    test_root_confinement_and_kst, test_same_ms_chain_signed, test_structural_damage
)
from regression.sync import (
    test_daemon_no_bare_git_spawn, test_local_lock_path_git_shapes, test_sync, test_sync_git_unknown_is_not_absent,
    test_sync_graph_scale, test_sync_pending_git_operations, test_sync_pins_main
)
from regression.approvals import (
    test_approval_baseline_blobs_present, test_approval_lifecycle, test_approval_serialized_with_writes,
    test_approve_precondition_under_lock, test_approve_requires_expect_work, test_atomic_write_waits_out_open_reader,
    test_authority_hold, test_baseline_bound_to_region, test_broken_delegation_isolated,
    test_changeset_lists_difference, test_delegation_protection_scope, test_dir_entries_durable_before_ledger,
    test_index_node_not_delegation, test_nested_regions_all_checked, test_protect_concurrent_write_becomes_pending,
    test_protect_precondition_rejects_stale, test_stale_changeset_before_confirmation,
    test_stale_region_not_unprotected, test_stale_sealed_by_approve, test_store_content_verified,
    test_store_digest_confined, test_unprotect_precondition_under_lock
)
from regression.reverts import (
    test_move_chain_reverted_no_duplicate, test_move_cutoff_causal_not_clock, test_move_lifecycle_cutoff,
    test_move_phantom_tail_row_harmless, test_move_recorded_and_reverted_in, test_move_reentry_single_plan,
    test_move_return_blocked_origin_occupied, test_move_reverted_out_no_duplicate,
    test_move_unrecorded_outside_protection, test_refused_revert_leaves_region_absent,
    test_region_replaced_by_file_is_pending, test_region_root_is_not_an_excluded_compartment,
    test_revert_binds_reviewed_changeset, test_revert_confirms_before_destroying, test_revert_incomplete_no_record,
    test_revert_path_identity, test_revert_recreates_deleted_region, test_revert_structure_conflict_no_partial
)
from regression.validator import (
    test_baseline_pass, test_cli_delegation, test_cli_emit_utf8, test_documented_one_liners,
    test_every_cli_command_has_its_handler, test_mcp_hardening_fail_closed, test_mcp_transport, test_overview,
    test_review_packet_sets_aside_a_mistyped_entry, test_self_referencing_edge, test_setup_doc_drift,
    test_surface_contract, test_surface_lint, test_surface_name_roundtrip, test_surface_smoke,
    test_validate_at_reads_only_a_report, test_validate_at_uses_snapshot_engine, test_validate_global_invariance,
    test_validate_suite_survives_a_crashing_check
)
from regression.write import (
    test_alias_cycle_returns_input, test_anchor_edit, test_bind_after_write_receipt, test_bound_scope_honest,
    test_candidate_needs_distinct, test_contract_values, test_derived_from_once_per_round,
    test_dup_stem_write_refused, test_edge_delta_is_cumulative, test_edge_single_list_roundtrip,
    test_edge_target_normalization, test_edge_value_type_refused, test_extra_field_preserved, test_full_body_dropped,
    test_id_as_handle, test_id_width_and_discriminator, test_name_is_the_handle, test_node_heading_edge_coordinates,
    test_project_bindings, test_refusal_teaches_address, test_render_roundtrip, test_routing_not_bricked,
    test_topology_rejects_wiki_node_derived_from, test_update_no_change, test_validate_uses_destination_path,
    test_write_candidate_basis, test_write_cas_body_bound, test_write_contract, test_write_edge_coordinates,
    test_write_move_and_pin, test_write_pin_subtree_and_fork, test_write_routing, test_write_serialized,
    test_write_session_alias
)
from regression.publish import (
    test_current_version_equivalent_heads, test_fresh_install_protects_governance, test_publish_binds_checked_bytes,
    test_publish_external_engine_preserves_manifest, test_publish_guards, test_publish_manifest,
    test_publish_preserves_mapped_dot_directories, test_publish_validator_uses_snapshot
)
from regression.release_update import test_release_and_update
from regression.raw import (
    test_external_roots, test_non_node_evidence_is_an_evidence_line, test_raw_append, test_raw_binding_confines_scope,
    test_raw_read, test_raw_replay_rejected, test_raw_space_misdiagnosis
)
from regression.prose import (
    test_code_region_block_boundaries, test_code_region_commonmark_rules, test_code_regions_are_not_prose,
    test_format_alignment, test_node_parse_raises_only_its_kinds, test_obsidian_tag_defense, test_parse_guards,
    test_portable_title, test_posix_rel_is_os_independent
)
from regression.session_keys import (
    test_derived_session_key_is_stable, test_ephemeral_session_key, test_session_key_identity_ignores_checkout,
    test_session_key_repo_identity
)
from regression.scope_memory import (
    test_cadence_hook, test_scope_memory, test_scope_memory_cli, test_scope_memory_edits, test_scope_recovery_handoff,
    test_workbench_state_not_evidence
)
from regression.evictions import test_eviction_preservation_mcp, test_evictions
from regression.clusters import (
    test_cluster_overview, test_move_nodes_and_cluster, test_move_topology_refused, test_nested_clusters,
    test_new_cluster_two_phase, test_node_place_rule, test_plain_write_placement, test_upgrading_protected_scope,
    test_upgrading_underscore_scope
)
from regression.index import (
    test_broken_is_reported_not_gated, test_duplicate_id_by_name_refused, test_duplicate_id_refused,
    test_engine_epoch_fence, test_fingerprint_scope_and_racy, test_incomplete_scan_refuses_writes, test_index_split,
    test_one_index_per_write, test_read_is_bound_to_bytes, test_same_file_link_is_duplicate,
    test_scan_confinement_and_case, test_traversal_deterministic
)
from regression.rechecks import (
    test_overview_rechecks_cache, test_read_cache_dependencies, test_rechecks, test_reparse_cache_membership,
    test_review_empty_memory_and_incomplete_region, test_review_root_reparse_and_lazy_search
)
from regression.audits import test_audit_fixes_2026_09_02, test_governance_amend_secrets_and_region
from regression.turn_ledger import test_turn_ledger
from regression.suites import (
    test_architecture_subprocess, test_growth_loop_subprocesses, test_release_workflow_subprocess,
    test_suite_skip_is_not_pass, test_suite_timeout_is_isolated, test_surface_snapshot_subprocess,
    test_sync_network_subprocess, test_upgrade_matrix_subprocess, test_vault_subprocess
)


if __name__ == "__main__":
    for fn in [test_posix_rel_is_os_independent, test_portable_title,
               test_cli_delegation, test_rid_monotone, test_same_ms_chain_signed,
               test_fork_failclosed_and_reseal, test_anchor_no_order_fallback,
               test_cycle_normalization, test_structural_damage,
               test_ridless_unsign_not_swallowed, test_root_confinement_and_kst,
               test_approval_lifecycle,
               test_path_reuse, test_fingerprint_move,
               test_sync, test_sync_graph_scale, test_sync_network_subprocess, test_conflicts_semantics,
               test_ledger_corruption_resilience, test_ledger_line_boundaries,
               test_ledger_separators_candidate, test_ledger_separators_move,
               test_jsonl_readers_line_separators,
               test_ledger_schema_segment,
               test_validate_global_invariance, test_authority_hold,
               test_self_referencing_edge, test_surface_contract,
               test_ledger_row_shape,
               test_broken_delegation_isolated, test_write_contract,
               test_contract_values, test_bind_after_write_receipt,
               test_mcp_hardening_fail_closed,
               test_write_cas_body_bound, test_anchor_edit, test_full_body_dropped,
               test_edge_single_list_roundtrip,
               test_write_move_and_pin,
               test_write_routing, test_write_session_alias,
               test_write_candidate_basis,
               test_write_serialized, test_surface_smoke,
               test_render_roundtrip, test_conflicts_open_case_path,
               test_routing_not_bricked, test_extra_field_preserved,
               test_alias_cycle_returns_input, test_candidate_needs_distinct,
               test_edge_target_normalization, test_update_no_change,
               test_bound_scope_honest, test_project_bindings, test_mcp_transport,
               test_surface_lint, test_overview, test_edge_value_type_refused,
               test_refusal_teaches_address, test_id_as_handle,
               test_surface_name_roundtrip, test_validate_uses_destination_path,
               test_dup_stem_write_refused,
               test_sync_pins_main, test_daemon_no_bare_git_spawn,
               test_publish_manifest, test_publish_guards,
               test_conflict_candidates,
               test_current_version_equivalent_heads, test_release_and_update,
               test_fresh_install_protects_governance,
               test_store_digest_confined, test_delegation_protection_scope,
               test_approve_requires_expect_work,
               test_approval_baseline_blobs_present,
               test_store_content_verified,
               test_dir_entries_durable_before_ledger,
               test_atomic_write_waits_out_open_reader,
               test_revert_incomplete_no_record,
               test_revert_path_identity,
               test_approve_precondition_under_lock,
               test_unprotect_precondition_under_lock,
               test_protect_precondition_rejects_stale,
               test_protect_concurrent_write_becomes_pending,
               test_revert_confirms_before_destroying,
               test_revert_recreates_deleted_region,
               test_revert_binds_reviewed_changeset,
               test_move_recorded_and_reverted_in,
               test_move_reverted_out_no_duplicate,
               test_move_return_blocked_origin_occupied,
               test_move_chain_reverted_no_duplicate,
               test_move_lifecycle_cutoff, test_move_reentry_single_plan,
               test_move_cutoff_causal_not_clock,
               test_move_phantom_tail_row_harmless,
               test_move_unrecorded_outside_protection,
               test_changeset_lists_difference, test_stale_sealed_by_approve,
               test_stale_changeset_before_confirmation,
               test_topology_rejects_wiki_node_derived_from,
               test_local_lock_path_git_shapes,
               test_revert_structure_conflict_no_partial,
               test_approval_serialized_with_writes,
               test_region_replaced_by_file_is_pending,
               test_refused_revert_leaves_region_absent,
               test_stale_region_not_unprotected,
               test_nested_regions_all_checked,
               test_baseline_bound_to_region,
               test_baseline_pass, test_raw_append, test_cli_emit_utf8,
               test_raw_read, test_external_roots, test_non_node_evidence_is_an_evidence_line,
               test_raw_space_misdiagnosis,
               test_raw_binding_confines_scope, test_raw_replay_rejected,
               test_scope_memory, test_workbench_state_not_evidence,
               test_scope_memory_edits, test_cadence_hook, test_scope_recovery_handoff,
               test_scope_memory_cli, test_new_cluster_two_phase,
               test_ephemeral_session_key, test_session_key_repo_identity,
               test_session_key_identity_ignores_checkout,
               test_derived_session_key_is_stable,
               test_cluster_overview,
               test_obsidian_tag_defense, test_code_regions_are_not_prose,
               test_code_region_block_boundaries, test_code_region_commonmark_rules,
               test_index_node_not_delegation,
               test_read_is_bound_to_bytes,
               test_incomplete_scan_refuses_writes,
               test_broken_is_reported_not_gated,
               test_id_width_and_discriminator,
               test_edge_delta_is_cumulative,
               test_name_is_the_handle,
               test_nested_clusters, test_plain_write_placement,
               test_move_nodes_and_cluster,
               test_duplicate_id_refused,
               test_duplicate_id_by_name_refused, test_same_file_link_is_duplicate,
               test_format_alignment, test_rechecks, test_overview_rechecks_cache,
               test_move_topology_refused,
               test_parse_guards, test_scan_confinement_and_case,
               test_node_place_rule, test_upgrading_underscore_scope,
               test_upgrading_protected_scope,
               test_traversal_deterministic, test_index_split,
               test_one_index_per_write,
               test_fingerprint_scope_and_racy, test_engine_epoch_fence,
               test_audit_fixes_2026_09_02,
               test_governance_amend_secrets_and_region,
               test_region_root_is_not_an_excluded_compartment,
               test_setup_doc_drift, test_documented_one_liners, test_turn_ledger, test_evictions,
               test_sync_pending_git_operations, test_sync_git_unknown_is_not_absent,
               test_publish_binds_checked_bytes,
               test_publish_validator_uses_snapshot, test_publish_external_engine_preserves_manifest,
               test_validate_at_uses_snapshot_engine, test_publish_preserves_mapped_dot_directories,
               test_ledger_damage_is_a_kind, test_node_parse_raises_only_its_kinds,
               test_validate_at_reads_only_a_report, test_validate_suite_survives_a_crashing_check,
               test_review_packet_sets_aside_a_mistyped_entry, test_every_cli_command_has_its_handler,
                test_write_edge_coordinates, test_node_heading_edge_coordinates,
                test_derived_from_once_per_round,
               test_write_pin_subtree_and_fork,
               test_review_empty_memory_and_incomplete_region,
               test_review_root_reparse_and_lazy_search, test_read_cache_dependencies,
               test_reparse_cache_membership,
               test_eviction_preservation_mcp,
               test_suite_timeout_is_isolated, test_suite_skip_is_not_pass,
               test_architecture_subprocess, test_surface_snapshot_subprocess, test_vault_subprocess,
                test_growth_loop_subprocesses, test_release_workflow_subprocess,
                test_upgrade_matrix_subprocess]:
        try:
            fn()
        except Exception as e:
            FAIL.append(f"{fn.__name__} 예외: {e!r}\n"
                        + "".join(traceback.format_exc().splitlines(True)[-6:]))
    # 이 실행의 몫만 센다 — 같은 기기에서 옛 수트가 동시에 돌면 그 누출도 실 임시
    # 디렉터리에 쌓인다(2026-09-24 실측). 격리 수트(자식)의 상태가 실행 자리에
    # 모였는가(돌림이 통째로 빠짐)와, 이 실행의 vault를 가리키는 상태가 실 임시
    # 디렉터리에 새로 생겼는가(일부 우회)를 함께 본다.
    mini = f"osk-integration-{hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]}-"
    caught = [p for p in RUN_TMP.glob("osk-integration-*.json") if not p.name.startswith(mini)]
    run_root = Path(_TMP.name).resolve()
    try:
        _TMP.cleanup()
    except OSError:
        pass                                  # 남은 자리는 아래 검사가 드러낸다
    leaked = []
    for name in sorted({p.name for p in REAL_TMP.glob("osk-integration-*.json")} - _LEAK_BEFORE):
        try:
            root = json.loads((REAL_TMP / name).read_text(encoding="utf-8")).get("root", "")
        except (OSError, ValueError):
            continue
        if Path(str(root)).is_relative_to(run_root):
            leaked.append(name)
    check("수트가 실 임시 디렉터리에 osk-integration-*·실행 자리를 남기지 않는다",
          caught and not leaked and not Path(_TMP.name).exists(),
          f"실행 자리 포착 {len(caught)}건 · 실 임시 누출 {len(leaked)}건 {leaked[:3]}"
          f" · 실행 자리 {_TMP.name} 잔존={Path(_TMP.name).exists()}")
    print(f"회귀 수트: 통과 {len(PASS)} / 실패 {len(FAIL)} / 생략 {len(SKIP)}"
          f"  (mini-vault: {MINI})")
    for f in FAIL:
        print("FAIL:", f)
    for s in SKIP:
        print("SKIP:", s)
    for p in PASS:
        print("  ✓", p)
    sys.exit(1 if FAIL else 0)
