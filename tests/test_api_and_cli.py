"""Integration tests for FastAPI REST endpoints and Click CLI."""

from __future__ import annotations

from click.testing import CliRunner
from fastapi.testclient import TestClient

from ger.api import create_app
from ger.cli import cli
from ger.config import ApiConfig, AppConfig
from ger.db import Database


def test_api_auth_and_crud(tmp_path) -> None:
    cfg = AppConfig(
        db_path=str(tmp_path / "api.db"),
        api=ApiConfig(enabled=True, auth_token="super-secret"),
    )
    db = Database(cfg.db_path)
    app = create_app(cfg, db=db, enable_scheduler=False)

    with TestClient(app) as client:
        # Web UI and health are public; /auth/verify enforces Bearer token
        root_resp = client.get("/")
        assert root_resp.status_code == 200
        assert "Game Event Reminder" in root_resp.text
        assert client.get("/health").status_code == 200
        assert client.get("/auth/verify").status_code == 401
        assert client.get("/auth/verify", headers={"Authorization": "Bearer wrong"}).status_code == 401

        # Events require Bearer token
        assert client.get("/events").status_code == 401
        headers = {"Authorization": "Bearer super-secret"}
        assert client.get("/auth/verify", headers=headers).status_code == 200

        # Create event
        create_resp = client.post(
            "/events",
            headers=headers,
            json={
                "game": "明日方舟",
                "title": "集成战略限时任务",
                "start_time": "2099-12-01T16:00:00+08:00",
                "end_time": "2099-12-15T03:59:00+08:00",
            },
        )
        assert create_resp.status_code == 201
        payload = create_resp.json()
        event_id = payload["event"]["id"]
        assert len(payload["reminders"]) == 2

        # Check digest queue endpoint
        q_resp = client.get("/queue", headers=headers)
        assert q_resp.status_code == 200
        assert q_resp.json()["mode"] == "digest"

        # List events
        list_resp = client.get("/events", headers=headers, params={"game": "明日方舟"})
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 1

        # Patch event (mute)
        patch_resp = client.patch(
            f"/events/{event_id}",
            headers=headers,
            json={"status": "muted", "notes": "已完成"},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["event"]["status"] == "muted"
        assert all(r["status"] == "muted" for r in patch_resp.json()["reminders"])

        # Delete event
        del_resp = client.delete(f"/events/{event_id}", headers=headers)
        assert del_resp.status_code == 200
        assert client.get(f"/events/{event_id}", headers=headers).status_code == 404


def test_cli_workflow(tmp_path) -> None:
    cfg_file = tmp_path / "config.yaml"
    db_file = tmp_path / "cli.db"
    cfg_file.write_text(
        f'timezone: "Asia/Shanghai"\ndb_path: "{db_file.as_posix()}"\n',
        encoding="utf-8",
    )

    runner = CliRunner()
    res_add = runner.invoke(
        cli,
        [
            "-c",
            str(cfg_file),
            "add",
            "--game",
            "绝区零",
            "--title",
            "式舆防卫战",
            "--start",
            "2099-12-01T10:00:00+08:00",
            "--end",
            "2099-12-15T03:59:00+08:00",
        ],
    )
    assert res_add.exit_code == 0, res_add.output
    assert "Created event #1" in res_add.output

    res_list = runner.invoke(cli, ["-c", str(cfg_file), "list"])
    assert res_list.exit_code == 0
    assert "式舆防卫战" in res_list.output

    res_show = runner.invoke(cli, ["-c", str(cfg_file), "show", "1"])
    assert res_show.exit_code == 0
    assert "end:-24h" in res_show.output

    res_mute = runner.invoke(cli, ["-c", str(cfg_file), "mute", "1"])
    assert res_mute.exit_code == 0
    assert "Muted event #1" in res_mute.output
