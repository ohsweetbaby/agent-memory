"""工作区 / Agent / EndUser 管理：CRUD、软删除唯一性、跨 workspace 隔离。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def test_workspace_and_agent_crud_is_isolated(client, admin) -> None:
    ws = client.post("/api/v1/workspaces", json={"name": "个人"}).json()
    agent = client.post(
        "/api/v1/agents", json={"workspace_id": ws["id"], "name": "助手"}
    ).json()
    assert client.get(f"/api/v1/agents/{agent['id']}").json()["name"] == "助手"
    assert client.get("/api/v1/agents?workspace_id=ws_other").status_code == 404


def test_workspace_get_patch_and_list(client, admin) -> None:
    created = client.post("/api/v1/workspaces", json={"name": "旧名字"}).json()
    assert client.get(f"/api/v1/workspaces/{created['id']}").json()["name"] == "旧名字"

    patched = client.patch(
        f"/api/v1/workspaces/{created['id']}", json={"name": "新名字"}
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "新名字"

    listed = client.get("/api/v1/workspaces")
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()["items"]}
    assert created["id"] in ids
    # setup 会创建默认工作区，保证全新安装可以直接创建第一个 API Key。
    assert "ws_default" in ids
    assert client.get("/api/v1/workspaces/ws_missing").status_code == 404


def test_agent_patch_list_and_soft_delete(client, admin) -> None:
    ws = client.post("/api/v1/workspaces", json={"name": "个人"}).json()
    agent = client.post(
        "/api/v1/agents", json={"workspace_id": ws["id"], "name": "助手"}
    ).json()

    patched = client.patch(
        f"/api/v1/agents/{agent['id']}",
        json={"name": "新助手", "description": "改写后的描述"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "新助手"

    listing = client.get(f"/api/v1/agents?workspace_id={ws['id']}")
    assert [item["id"] for item in listing.json()["items"]] == [agent["id"]]

    assert client.delete(f"/api/v1/agents/{agent['id']}").status_code == 204
    assert client.get(f"/api/v1/agents/{agent['id']}").status_code == 404
    assert client.get(f"/api/v1/agents?workspace_id={ws['id']}").json()["items"] == []


def test_soft_deleted_agent_name_can_be_recreated(client, admin) -> None:
    """软删除后同名 Agent 必须能重建（partial unique index）。"""
    ws = client.post("/api/v1/workspaces", json={"name": "个人"}).json()
    first = client.post(
        "/api/v1/agents", json={"workspace_id": ws["id"], "name": "助手"}
    ).json()
    assert client.delete(f"/api/v1/agents/{first['id']}").status_code == 204

    second = client.post("/api/v1/agents", json={"workspace_id": ws["id"], "name": "助手"})
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first["id"]

    listing = client.get(f"/api/v1/agents?workspace_id={ws['id']}").json()["items"]
    assert [item["id"] for item in listing] == [second.json()["id"]]


def test_duplicate_active_agent_name_conflicts(client, admin) -> None:
    ws = client.post("/api/v1/workspaces", json={"name": "个人"}).json()
    assert (
        client.post(
            "/api/v1/agents", json={"workspace_id": ws["id"], "name": "助手"}
        ).status_code
        == 201
    )
    duplicate = client.post(
        "/api/v1/agents", json={"workspace_id": ws["id"], "name": "助手"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


def test_soft_deleted_end_user_external_id_can_be_recreated(client, admin) -> None:
    """软删除后同 external_id 的 EndUser 必须能重建（partial unique index）。"""
    ws = client.post("/api/v1/workspaces", json={"name": "个人"}).json()
    payload = {"workspace_id": ws["id"], "external_id": "u_123"}
    first = client.post("/api/v1/end-users", json=payload)
    assert first.status_code == 201, first.text

    assert client.delete(f"/api/v1/end-users/{first.json()['id']}").status_code == 204

    second = client.post("/api/v1/end-users", json=payload)
    assert second.status_code == 201, second.text
    assert second.json()["id"] != first.json()["id"]

    listing = client.get(f"/api/v1/end-users?workspace_id={ws['id']}").json()["items"]
    assert [item["id"] for item in listing] == [second.json()["id"]]


def test_agent_from_other_workspace_is_404_for_api_key(
    client, admin, read_key
) -> None:
    other = client.post("/api/v1/workspaces", json={"name": "另一个工作区"}).json()
    foreign = client.post(
        "/api/v1/agents", json={"workspace_id": other["id"], "name": "外部助手"}
    ).json()

    client.cookies.clear()
    response = client.get(
        f"/api/v1/agents/{foreign['id']}",
        headers={"Authorization": f"Bearer {read_key['key']}"},
    )
    assert response.status_code == 404
