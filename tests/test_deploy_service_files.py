from pathlib import Path


def test_scheduler_service_runs_dbdoctor_before_scheduler_start() -> None:
    service_file = (
        Path(__file__).resolve().parents[1] / "deploy" / "sms-scheduler.service"
    )

    contents = service_file.read_text(encoding="utf-8")

    assert "ExecStartPre=/usr/local/bin/dbdoctor --apply" in contents
    assert (
        contents.index("ExecStartPre=/usr/local/bin/dbdoctor --apply")
        < contents.index("ExecStart=/bin/bash /opt/sms-admin/deploy/run_scheduler_once.sh")
    )


def test_scheduler_wrapper_uses_runtime_app_bootstrap() -> None:
    wrapper_file = (
        Path(__file__).resolve().parents[1] / "deploy" / "run_scheduler_once.sh"
    )

    contents = wrapper_file.read_text(encoding="utf-8")

    assert "from app import create_runtime_app" in contents
    assert "app = create_runtime_app()" in contents
