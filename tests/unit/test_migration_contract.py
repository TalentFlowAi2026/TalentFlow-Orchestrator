from pathlib import Path


def migrations() -> tuple[str, str, str]:
    root = Path(__file__).resolve().parents[3]
    directory = root / "supabase" / "migrations"
    return tuple(
        (directory / name).read_text(encoding="utf-8")
        for name in (
            "202609230004_interview_cancelled_enum.sql",
            "202609230005_scheduling_orchestrator.sql",
            "202609230006_scheduling_indexes.sql",
        )
    )  # type: ignore[return-value]


def security_alignment_migration() -> str:
    root = Path(__file__).resolve().parents[3]
    return (
        root
        / "supabase"
        / "migrations"
        / "202609240007_scheduling_security_alignment.sql"
    ).read_text(encoding="utf-8")


def test_enum_change_is_isolated_before_transactional_schema_change() -> None:
    enum_sql, schema_sql, _ = migrations()
    assert "ALTER TYPE public.interview_status ADD VALUE IF NOT EXISTS 'cancelled'" in enum_sql
    assert "BEGIN;" not in enum_sql
    assert schema_sql.index("BEGIN;") < schema_sql.index("SET LOCAL")
    assert schema_sql.rstrip().endswith("COMMIT;")


def test_tenant_composite_foreign_keys_never_null_the_tenant_column() -> None:
    _, schema_sql, _ = migrations()
    assert "FOREIGN KEY (company_id,proposal_id)" in schema_sql
    assert "FOREIGN KEY (company_id,interview_id)" in schema_sql
    assert "REFERENCES public.scheduling_proposals(company_id,id) ON DELETE SET NULL" not in schema_sql
    assert "REFERENCES public.interviews(company_id,id) ON DELETE SET NULL" not in schema_sql


def test_queue_partition_and_concurrent_index_rollout_are_explicit() -> None:
    _, schema_sql, index_sql = migrations()
    assert "executor_service = 'scheduling_orchestrator'" in schema_sql
    assert "'orchestrator_pending','orchestrator_leased','orchestrator_completed'" in schema_sql
    assert "session_id IS NULL" in schema_sql
    assert "worker_jobs_executor_boundary" in schema_sql
    assert "worker job executor boundary violation" in schema_sql
    assert "CREATE INDEX CONCURRENTLY" in index_sql
    assert "BEGIN;" not in index_sql


def test_canonical_interview_schedule_persists_end_timezone_version_and_buffer() -> None:
    _, schema_sql, _ = migrations()
    for column in (
        "scheduled_end_at",
        "scheduling_timezone",
        "scheduling_buffer_minutes",
        "schedule_version",
    ):
        assert f"ADD COLUMN {column}" in schema_sql


def test_orchestrator_role_is_explicit_and_new_tables_enable_rls() -> None:
    _, schema_sql, _ = migrations()
    assert "CREATE ROLE talentflow_orchestrator NOLOGIN BYPASSRLS" in schema_sql
    for table in (
        "job_candidates",
        "scheduling_proposals",
        "scheduling_proposal_items",
        "interview_interviewers",
        "integration_actions",
        "scheduling_events",
    ):
        assert f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY" in schema_sql


def test_existing_company_candidate_relationships_are_backfilled() -> None:
    _, schema_sql, _ = migrations()
    assert "INSERT INTO public.job_candidates" in schema_sql
    assert "FROM public.interviews i" in schema_sql
    assert "cm.role='company_admin'" in schema_sql


def test_post_006_invitation_binding_is_additive_and_concurrent() -> None:
    sql = security_alignment_migration()
    assert "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS" in sql
    assert "invitations_interview_candidate_tenant_fk" in sql
    assert "FOREIGN KEY (company_id,interview_id,candidate_id)" in sql
    assert "REFERENCES public.interviews(company_id,id,candidate_id)" in sql
    assert "NOT VALID" in sql
    assert "VALIDATE CONSTRAINT" in sql
    assert "BEGIN;" not in sql


def test_post_006_orchestrator_reads_only_required_base_columns() -> None:
    sql = security_alignment_migration()
    assert "REVOKE SELECT ON TABLE" in sql
    assert "GRANT SELECT (id,role,full_name,email)" in sql
    assert "cv_parsed_data" not in sql
    assert "livekit_room_id" not in sql
    assert "overall_score" not in sql
