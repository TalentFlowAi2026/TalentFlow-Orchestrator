import inspect
from pathlib import Path

from talentflow_orchestrator.api.routes.scheduling import reschedule_availability
from talentflow_orchestrator.persistence.repository import SchedulingRepository


def test_proposal_creation_has_no_provider_side_effects() -> None:
    source = inspect.getsource(SchedulingRepository.create_proposal)
    assert "integration_actions" not in source
    assert "worker_jobs" not in source
    assert "calendar" not in source.casefold()
    assert "gmail" not in source.casefold()


def test_reschedule_availability_is_a_side_effect_free_preview() -> None:
    source = inspect.getsource(reschedule_availability)
    assert "reschedule_search_request" in source
    assert "reschedule_busy_intervals" in source
    assert "_propose" in source
    assert "repository.reschedule(" not in source
    assert "_create_provider_actions" not in source


def test_duplicate_confirmation_replays_confirmed_items() -> None:
    source = inspect.getsource(SchedulingRepository.confirm)
    assert "event_type='schedule_confirmed'" in source
    assert 'raise ServiceError("idempotency_conflict"' in source
    assert 'if str(proposal["status"]) == "confirmed"' in source
    assert "return await self._confirmed_items(conn, proposal_id)" in source
    assert "talentflow:scheduled-interview:" in source


def test_confirm_persists_canonical_schedule_fields() -> None:
    source = inspect.getsource(SchedulingRepository.confirm)
    for field in (
        "scheduled_at",
        "scheduled_end_at",
        "scheduling_timezone",
        "scheduling_buffer_minutes",
        "schedule_version",
    ):
        assert field in source


def test_confirm_creates_interviews_and_queues_calendar_and_email_jobs() -> None:
    source = inspect.getsource(SchedulingRepository.confirm)
    assert "INSERT INTO interviews" in source
    assert "_create_provider_actions" in source
    assert "JobKind.CREATE_CALENDAR_EVENT" in source
    assert "JobKind.SEND_INVITATION" in source
    assert source.index("INSERT INTO interviews") < source.index("_create_provider_actions")


def test_candidate_assignment_never_accepts_an_arbitrary_global_profile() -> None:
    source = inspect.getsource(SchedulingRepository.assign_candidates)
    assert "WHERE i.company_id=$2 AND i.candidate_id=p.id" in source
    assert "WHERE known.company_id=$2 AND known.candidate_id=p.id" in source


def test_cancelled_interview_is_not_startable_by_the_interview_engine() -> None:
    root = Path(__file__).resolve().parents[3]
    models = (
        root / "python-worker" / "src" / "talentflow_worker" / "domain" / "models.py"
    ).read_text(encoding="utf-8")
    sessions = (
        root / "python-worker" / "src" / "talentflow_worker" / "api" / "routes" / "sessions.py"
    ).read_text(encoding="utf-8")
    assert '"flagged", "cancelled"' in models
    assert 'context.status not in {"scheduled", "in_progress"}' in sessions


def test_azure_definition_creates_dedicated_api_and_worker_apps() -> None:
    root = Path(__file__).resolve().parents[2]
    bicep = (root / "azure" / "main.bicep").read_text(encoding="utf-8")
    assert bicep.count("resource api 'Microsoft.App/containerApps@") == 1
    assert bicep.count("resource worker 'Microsoft.App/containerApps@") == 1
    assert "command: ['talentflow-orchestrator-api']" in bicep
    assert "command: ['talentflow-orchestrator-worker']" in bicep
    assert "talentflow_worker" not in bicep
    assert "Python Worker" not in bicep
    assert "DEFAULT_INTERVIEW_DAY_START" in bicep
    assert "DEFAULT_INTERVIEW_DAY_END" in bicep


def test_docker_context_excludes_local_credentials_and_tool_caches() -> None:
    root = Path(__file__).resolve().parents[2]
    ignored = set((root / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {".env*", ".azure-cli", ".pip-audit-cache", ".build-artifacts"} <= ignored
