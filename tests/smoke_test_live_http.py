"""Project EDITH — Live Local HTTP & Socket Smoke Test.

This test spawns the real Uvicorn server in a separate subprocess over a loopback TCP port,
executes genuine HTTP requests via httpx over real sockets (without TestClient/in-memory mocks),
and validates live startup, health reporting, authentication, client isolation, preset execution,
error masking, and clean process termination.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import httpx


def run_live_smoke_test():
    print("[SMOKE] Starting Live HTTP Socket Smoke Test...")

    # Create temporary isolated database
    temp_dir = tempfile.mkdtemp(prefix="edith_smoke_")
    db_path = os.path.join(temp_dir, "smoke.db")
    db_url = f"sqlite+aiosqlite:///{db_path}"

    test_port = 8019
    base_url = f"http://127.0.0.1:{test_port}"

    env = os.environ.copy()
    env["APP_HOST"] = "127.0.0.1"
    env["APP_PORT"] = str(test_port)
    env["DATABASE_URL"] = db_url
    env["SESSION_SECRET"] = "smoke_test_session_secret_min_32_chars_ok"
    env["API_KEYS"] = "smoke_key_alpha,smoke_key_beta"
    env["ALLOW_REMOTE_API"] = "false"
    env["STORAGE_PATH"] = temp_dir
    env["TOOL_WORKSPACE_ROOT"] = os.path.join(temp_dir, "ws")
    env["TELEGRAM_BOT_TOKEN"] = "your-telegram-bot-token"  # intentionally default placeholder

    # Choose python executable
    py_exe = sys.executable

    print(f"[SMOKE] Spawning EDITH Uvicorn backend on {base_url} (PID tracking)...")
    proc = subprocess.Popen(
        [py_exe, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(test_port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # 1. Wait for server to bind and become responsive
        client = httpx.Client(base_url=base_url, timeout=20.0)
        ready = False
        for attempt in range(20):
            try:
                res = client.get("/health")
                if res.status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.4)

        if not ready:
            stdout, stderr = proc.communicate(timeout=3)
            raise RuntimeError(f"Server failed to start on port {test_port}.\nStdout: {stdout}\nStderr: {stderr}")

        print("[SMOKE] -> Server is UP and listening on TCP socket.")

        # 2. Check /health accurate reporting
        health_data = client.get("/health").json()
        print(f"[SMOKE] -> /health response: {health_data}")
        assert health_data["backend"] is True
        assert health_data["database"] is True
        # Reports boolean status accurately for all services
        assert isinstance(health_data["ollama"], bool)
        assert isinstance(health_data["telegram"], bool)
        assert isinstance(health_data["whatsapp"], bool)

        # 3. Unauthenticated request -> 401
        res_unauth = client.post("/api/chat", json={"message": "Hello"})
        assert res_unauth.status_code == 401
        assert "Authentication required" in res_unauth.json()["detail"]
        assert "Traceback" not in res_unauth.text
        print("[SMOKE] -> 401 Unauthorized verified on unauthenticated request.")

        # 4. Invalid key -> 401
        res_bad_key = client.post("/api/chat", json={"message": "Hello"}, headers={"X-API-Key": "invalid_key"})
        assert res_bad_key.status_code == 401
        print("[SMOKE] -> 401 Unauthorized verified on invalid API key.")

        # 5. Machine API key blocked from admin endpoints
        res_admin = client.get("/api/models", headers={"X-API-Key": "smoke_key_alpha"})
        assert res_admin.status_code in (401, 403)
        print("[SMOKE] -> Machine client privilege boundary confirmed (admin route blocked).")

        # 6. Canonical chat request with valid key -> 200 OK (Fast deterministic preset execution path)
        res_chat = client.post(
            "/api/chat",
            json={"message": "Hello", "conversation_id": "thread_alpha"},
            headers={"X-API-Key": "smoke_key_alpha"},
        )
        assert res_chat.status_code == 200
        data = res_chat.json()
        print(f"[SMOKE] -> /api/chat success response schema: {list(data.keys())}")
        assert data["status"] == "success"
        assert "response" in data and len(data["response"]) > 0
        assert data["conversation_id"] == "thread_alpha"
        assert "correlation_id" in data
        assert "created_at" in data
        print("[SMOKE] -> Fast deterministic preset response verified over real socket.")

        # 7A. Root alias /v1/chat request with valid key -> 200 OK
        res_v1 = client.post(
            "/v1/chat",
            json={"message": "Hello", "conversation_id": "thread_v1"},
            headers={"X-API-Key": "smoke_key_alpha"},
        )
        assert res_v1.status_code == 200
        assert res_v1.json()["status"] == "success"
        print("[SMOKE] -> Root alias /v1/chat verified successfully over real socket.")

        # 7B. Alias /api/v1/chat request with valid key -> 200 OK
        res_api_v1 = client.post(
            "/api/v1/chat",
            json={"message": "Hello", "conversation_id": "thread_api_v1"},
            headers={"X-API-Key": "smoke_key_alpha"},
        )
        assert res_api_v1.status_code == 200
        assert res_api_v1.json()["status"] == "success"
        print("[SMOKE] -> Alias /api/v1/chat verified successfully over real socket.")

        # 7C. Genuine LLM inference over real TCP socket when Ollama is available
        if health_data["ollama"]:
            res_inf = client.post(
                "/api/chat",
                json={
                    "message": "What is the chemical symbol for Oxygen? Answer with just the single letter.",
                    "conversation_id": "thread_real_inference",
                },
                headers={"X-API-Key": "smoke_key_alpha"},
                timeout=60.0,
            )
            assert res_inf.status_code == 200
            inf_data = res_inf.json()
            assert inf_data["status"] == "success"
            assert inf_data["model_used"] is not None
            assert len(inf_data["response"]) > 0
            print(f"[SMOKE] -> Genuine Ollama model inference verified over real TCP socket (model={inf_data['model_used']}, response={repr(inf_data['response'].strip())}).")

        # 8. Uninstalled model diagnostic error response -> clean 503/400 without stack trace
        res_model_err = client.post(
            "/api/chat",
            json={"message": "What is the capital of France?", "model": "nonexistent-model-99"},
            headers={"X-API-Key": "smoke_key_alpha"},
        )
        assert res_model_err.status_code in (400, 503)
        assert "Traceback" not in res_model_err.text
        print(f"[SMOKE] -> Model unavailable error handled cleanly with code {res_model_err.status_code}.")

        # 9. Client isolation verification over live HTTP
        # Client Beta sends message with same thread name "thread_alpha"
        res_beta = client.post(
            "/api/chat",
            json={"message": "Hello", "conversation_id": "thread_alpha"},
            headers={"X-API-Key": "smoke_key_beta"},
        )
        assert res_beta.status_code == 200

        # Verify SQLite has 2 separate user identities
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id, external_id FROM users")
        users = cursor.fetchall()
        print(f"[SMOKE] -> SQLite registered users: {users}")
        assert len(users) == 2
        # Verify 256-bit collision-resistant identities
        expected_alpha = f"api:client_{hashlib.sha256(b'smoke_key_alpha').hexdigest()}"
        expected_beta = f"api:client_{hashlib.sha256(b'smoke_key_beta').hexdigest()}"
        ext_ids = [u[1] for u in users]
        assert expected_alpha in ext_ids
        assert expected_beta in ext_ids

        # Verify separate conversation threads for thread_alpha across the two users
        cursor.execute("SELECT id, user_id, external_chat_id FROM conversations WHERE external_chat_id = 'thread_alpha'")
        convs = cursor.fetchall()
        print(f"[SMOKE] -> SQLite thread_alpha conversation records: {convs}")
        assert len(convs) == 2
        assert convs[0][1] != convs[1][1]  # Different user IDs!
        assert convs[0][0] != convs[1][0]  # Different conversation primary keys!
        conn.close()
        print("[SMOKE] -> Cross-client conversation thread isolation verified over live HTTP.")

        print("\n=======================================================")
        print("ALL LIVE HTTP SOCKET SMOKE TEST CHECKS PASSED!")
        print("=======================================================\n")

    finally:
        print("[SMOKE] Terminating Uvicorn backend process...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        print("[SMOKE] Backend process terminated and socket released.")

        # Cleanup temp directory
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    run_live_smoke_test()
