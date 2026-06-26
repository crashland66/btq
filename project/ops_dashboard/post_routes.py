from __future__ import annotations

from http import HTTPStatus

from ops_dashboard.sections import batch_images, candidates, captures, drafts, employee_detail, equipment, failed, home, issues, prospect_detail, site_detail, sites, supplies, system, tokens

Response = tuple[HTTPStatus, str, bytes, dict[str, str]]


def dispatch_post_route(
    route_path: str,
    ctx: object,
    body: bytes,
    content_type: str,
    section_routes: dict[str, object],
) -> Response | None:
    if route_path in {"/field-capture/review/approve", "/field-capture/review/reject"}:
        ctx.action = "approve" if route_path.endswith("/approve") else "reject"
        return candidates.handle_review_post(ctx, body)
    if route_path == "/field-capture/review/edit":
        return candidates.handle_edit(ctx, body)
    if route_path == "/field-capture/review/approve-set":
        return candidates.handle_approve_set(ctx, body)
    if route_path == "/field-capture/review/archive":
        return candidates.handle_archive(ctx, body)
    if route_path == "/field-capture/review/unarchive":
        return candidates.handle_unarchive(ctx, body)
    if route_path == "/field-capture/review/client-informed":
        return candidates.handle_client_informed(ctx, body)
    if route_path == "/field-capture/review/resolve":
        return candidates.handle_resolve(ctx, body)
    if route_path == "/field-capture/review/dismiss-completion":
        return candidates.handle_completion_dismiss_post(ctx, body)
    if route_path == "/captures/analyze-deeper":
        return captures.handle_analyze_deeper_post(ctx, body)
    if route_path == "/captures/send-to-shift-report":
        return captures.handle_send_to_shift_report(ctx, body)
    if route_path == "/drafts/generate":
        return drafts.handle_generate_post(ctx, body)
    if route_path == "/drafts/stage":
        return drafts.handle_stage_post(ctx, body)
    if route_path == "/supplies/mark-ordered":
        return supplies.handle_mark_supply_ordered(ctx, body)
    if route_path == "/supplies/mark-delivered":
        return supplies.handle_mark_supply_delivered(ctx, body)
    if route_path == "/supplies/mark-stocked":
        return supplies.handle_mark_supply_stocked(ctx, body)
    if route_path == "/supplies/mark-no-action-needed":
        return supplies.handle_mark_supply_no_action_needed(ctx, body)
    if route_path == "/supplies/archive":
        return supplies.handle_supply_archive(ctx, body)
    if route_path == "/supplies/restore":
        return supplies.handle_supply_restore(ctx, body)
    if route_path == "/supplies/edit":
        return supplies.handle_supply_edit(ctx, body)
    if route_path == "/field-capture/issues/mark-monitoring":
        return issues.handle_mark_issue_monitoring(ctx, body)
    if route_path == "/field-capture/issues/mark-resolved":
        return issues.handle_mark_issue_resolved(ctx, body)
    if route_path == "/field-capture/issues/reopen":
        return issues.handle_mark_issue_open(ctx, body)
    if route_path == "/field-capture/issues/archive":
        return issues.handle_issue_archive(ctx, body)
    if route_path == "/field-capture/issues/restore":
        return issues.handle_issue_restore(ctx, body)
    if route_path == "/field-capture/issues/edit":
        return issues.handle_issue_edit(ctx, body)
    if route_path == "/equipment/mark-approved":
        return equipment.handle_mark_equipment_approved(ctx, body)
    if route_path == "/equipment/mark-denied":
        return equipment.handle_mark_equipment_denied(ctx, body)
    if route_path == "/equipment/mark-ordered":
        return equipment.handle_mark_equipment_ordered(ctx, body)
    if route_path == "/equipment/mark-provided":
        return equipment.handle_mark_equipment_provided(ctx, body)
    if route_path == "/equipment/mark-no-action-needed":
        return equipment.handle_mark_equipment_no_action_needed(ctx, body)
    if route_path == "/equipment/archive":
        return equipment.handle_equipment_archive(ctx, body)
    if route_path == "/equipment/restore":
        return equipment.handle_equipment_restore(ctx, body)
    if route_path == "/equipment/edit":
        return equipment.handle_equipment_edit(ctx, body)
    if route_path == "/failed/retry-sidecar":
        return failed.handle_retry_sidecar_post(ctx, body)
    if route_path == "/sites/save":
        return sites.handle_save_post(ctx, body)
    if route_path.startswith("/sites/") and route_path.endswith("/urls"):
        site_id = route_path.removeprefix("/sites/").removesuffix("/urls").strip("/")
        if site_id and "/" not in site_id:
            return site_detail.handle_site_url_post(ctx, site_id, body)
    if route_path.endswith("/save-section"):
        for prefix, handler in (("/sites/", site_detail.handle_save_section), ("/employees/", employee_detail.handle_save_section)):
            entity_id = route_path.removeprefix(prefix).removesuffix("/save-section") if route_path.startswith(prefix) else ""
            if entity_id and "/" not in entity_id:
                return handler(ctx, entity_id, body)
    if route_path == "/sites/new":
        return sites.handle_new_post(ctx, body)
    if route_path == "/tokens/new":
        return tokens.handle_new_post(ctx, body)
    if route_path == "/tokens/edit":
        return tokens.handle_edit_post(ctx, body)
    if route_path == "/tokens/revoke":
        return tokens.handle_revoke_post(ctx, body)
    if route_path == "/tokens/regenerate":
        return tokens.handle_regenerate_post(ctx, body)
    if route_path == "/tokens/set-raw":
        return tokens.handle_set_raw_post(ctx, body)
    if route_path == "/system/save":
        return system.handle_save_post(ctx, body)
    if route_path == "/vault-home/voice-memo":
        return home.handle_voice_memo_post(ctx, body, content_type=content_type)
    if route_path == "/batch-images/upload":
        return batch_images.handle_batch_upload_post(ctx, body, content_type=content_type)
    if route_path.startswith("/prospects/") and route_path not in section_routes:
        rest = route_path.removeprefix("/prospects/").rstrip("/")
        prospect_id, _sep, tail = rest.partition("/")
        if prospect_id and tail == "promote":
            return prospect_detail.handle_promote_post(ctx, body, prospect_id=prospect_id)
    return None
