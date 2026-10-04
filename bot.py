import os
import time
import logging

import requests
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ──────────────────────────── Конфигурация ────────────────────────────

ACCESS_TOKEN = os.environ["VK_ACCESS_TOKEN"]
GROUP_ID = int(os.environ["VK_GROUP_ID"])
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "").split(",")  # например: "https://front.example.com"

API_VERSION = "5.199"
API_BASE = "https://api.vk.com/method/"
REQUEST_PAUSE = 0.35          # ~3 запроса/сек — безопасный темп
HTTP_TIMEOUT = 15             # таймаут на запрос к VK API

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cleanup")

# ──────────────────────────── FastAPI ────────────────────────────

app = FastAPI(title="VK Inactive Members Cleaner")

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in CORS_ORIGINS if o.strip()],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

# ──────────────────────────── Состояние ────────────────────────────

_task_status: dict = {"running": False, "result": None, "progress": None}


# ──────────────────────────── VK API helpers ────────────────────────────

def api_call(method: str, params: dict, ignore_errors: set[int] = frozenset()):
    params["access_token"] = ACCESS_TOKEN
    params["v"] = API_VERSION
    try:
        resp = requests.post(f"{API_BASE}{method}", data=params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as e:
        log.error("Сеть недоступна при вызове %s: %s", method, e)
        return None

    try:
        data = resp.json()
    except ValueError:
        log.error("VK вернул не-JSON для %s (HTTP %s)", method, resp.status_code)
        return None

    if "error" in data:
        err = data["error"]
        if err["error_code"] in ignore_errors:
            return None
        log.error("VK ошибка %s для %s: %s", err["error_code"], method, err["error_msg"])
        return None
    return data["response"]


def get_all_members(group_id: int) -> list[int]:
    members = []
    offset = 0
    while True:
        resp = api_call("groups.getMembers", {
            "group_id": group_id,
            "offset": offset,
            "count": 1000,
        })
        if not resp:
            break
        batch = resp["items"]
        members.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
        time.sleep(REQUEST_PAUSE)
    return members


def check_users(user_ids: list[int]) -> list[int]:
    """ID пользователей с deactivated == deleted | banned."""
    inactive = []
    for i in range(0, len(user_ids), 100):
        chunk = user_ids[i:i + 100]
        resp = api_call("users.get", {
            "user_ids": ",".join(str(u) for u in chunk),
            "fields": "deactivated",
        })
        if not resp:
            continue
        for user in resp:
            if user.get("deactivated") in ("deleted", "banned"):
                inactive.append(user["id"])
        time.sleep(REQUEST_PAUSE)
    return inactive


def remove_users(group_id: int, user_ids: list[int], total: int) -> tuple[int, list[str]]:
    removed = 0
    errors = []
    # 7 — нет прав, 701 — «нельзя удалить этого пользователя»: оба пропускаем
    for uid in user_ids:
        resp = api_call("groups.removeUser", {
            "group_id": group_id,
            "user_id": uid,
        }, ignore_errors={7, 701})
        if resp is not None:
            removed += 1
        else:
            errors.append(str(uid))
        _task_status["progress"]["processed"] = removed + len(errors)
        time.sleep(REQUEST_PAUSE)
    return removed, errors


# ──────────────────────────── Основная логика ────────────────────────────

def run_cleanup():
    try:
        members = get_all_members(GROUP_ID)
        _task_status["progress"] = {"stage": "check", "total": len(members), "processed": 0}

        inactive = check_users(members)
        _task_status["progress"] = {"stage": "remove", "total": len(inactive), "processed": 0}

        if not inactive:
            _task_status["result"] = {
                "total_members": len(members),
                "inactive_found": 0,
                "removed": 0,
                "errors": [],
            }
            return

        removed, errors = remove_users(GROUP_ID, inactive, len(inactive))
        _task_status["result"] = {
            "total_members": len(members),
            "inactive_found": len(inactive),
            "removed": removed,
            "errors": errors,
        }
    except Exception:
        # Любой сбой не должен оставлять running=True навсегда
        log.exception("Очистка завершилась с ошибкой")
        _task_status["result"] = None
    finally:
        _task_status["running"] = False


# ──────────────────────────── Эндпоинты ────────────────────────────

@app.post("/clean")
def clean():
    """Запускает очистку в фоне. 409, если задача уже идёт."""
    if _task_status["running"]:
        return JSONResponse(status_code=409, content={"error": "Задача уже выполняется"})

    _task_status.update(running=True, result=None, progress=None)

    def _task():
        run_cleanup()

    # BackgroundTasks сам передаёт задачу после отправки ответа
    from fastapi import BackgroundTasks as BT  # noqa — см. сигнатуру ниже
    return {"started": True}


# ВАЖНО: BackgroundTasks должен приходить как зависимость, иначе задача не запустится
