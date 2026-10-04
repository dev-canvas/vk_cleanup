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

# Пустая переменная не должна давать truthy-список с пустой строкой,
# иначе CORS-middleware заблокирует все кросс-доменные запросы
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
]

API_VERSION = "5.199"
API_BASE = "https://api.vk.com/method/"
REQUEST_PAUSE = 0.35          # ~3 запроса/сек — безопасный темп
HTTP_TIMEOUT = 15             # таймаут на запрос к VK API

# ──────────────────────────── Логирование ────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cleanup")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # без спама от access-логов

# ──────────────────────────── FastAPI ────────────────────────────

app = FastAPI(title="VK Inactive Members Cleaner")

if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    log.info("CORS разрешён для доменов: %s", ", ".join(CORS_ORIGINS))
else:
    log.warning("CORS_ORIGINS не задана — запросы с фронтенда будут заблокированы браузером")

log.info("Сервис запущен. GROUP_ID=%s, токен задан: %s", GROUP_ID, bool(ACCESS_TOKEN))

# ──────────────────────────── Состояние ────────────────────────────

_task_status: dict = {"running": False, "result": None, "progress": None}


# ──────────────────────────── VK API helpers ────────────────────────────

def api_call(method: str, params: dict, ignore_errors: set[int] = frozenset()):
    """Вызов метода VK API. При ошибке пишет лог и возвращает None."""
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
            log.info("VK %s: пользователь %s недоступен для удаления (код %s), пропускаю",
                     method, params.get("user_id"), err["error_code"])
            return None
        log.error("VK ошибка %s для %s: %s", err["error_code"], method, err["error_msg"])
        return None
    return data["response"]


def get_all_members(group_id: int) -> list[int]:
    """Собирает все ID подписчиков сообщества."""
    members: list[int] = []
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
        log.info("Получено подписчиков: %d (offset=%d)", len(members), offset)
        if len(batch) < 1000:
            break
        offset += 1000
        time.sleep(REQUEST_PAUSE)
    return members


def check_users(user_ids: list[int]) -> list[int]:
    """Возвращает ID пользователей с deactivated == deleted | banned."""
    inactive: list[int] = []
    total = len(user_ids)
    for i in range(0, total, 100):
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
                log.info("Неактивный: id%s — %s", user["id"], user["deactivated"])
        progress = _task_status.get("progress")
        if progress is not None:
            progress["processed"] = min(i + 100, total)
        time.sleep(REQUEST_PAUSE)
    return inactive


def remove_users(group_id: int, user_ids: list[int]) -> tuple[int, list[str]]:
    """Удаляет пользователей из подписчиков. Возвращает (удалено, ошибки)."""
    removed = 0
    errors: list[str] = []
    # 7 — нет прав, 701 — «нельзя удалить этого пользователя»: оба пропускаем
    for idx, uid in enumerate(user_ids, start=1):
        resp = api_call("groups.removeUser", {
            "group_id": group_id,
            "user_id": uid,
        }, ignore_errors={7, 701})
        if resp is not None:
            removed += 1
        else:
            errors.append(str(uid))
        if idx % 50 == 0 or idx == len(user_ids):
            log.info("Удаление: %d/%d (успешно %d)", idx, len(user_ids), removed)
        progress = _task_status.get("progress")
        if progress is not None:
            progress["processed"] = idx
        time.sleep(REQUEST_PAUSE)
    return removed, errors


# ──────────────────────────── Основная логика ────────────────────────────

def run_cleanup():
    try:
        log.info("Очистка запущена")

        members = get_all_members(GROUP_ID)
        if not members:
            # Ошибка VK или прав — это не «пустое сообщество»,
            # маскировать её зелёным результатом нельзя
            raise RuntimeError(
                "Не удалось получить список подписчиков "
                "(проверьте токен и права на groups.getMembers)"
            )
        log.info("Всего подписчиков: %d. Начинаю проверку активности", len(members))

        _task_status["progress"] = {"stage": "check", "total": len(members), "processed": 0}
        inactive = check_users(members)
        log.info("Проверка завершена. Неактивных найдено: %d", len(inactive))

        _task_status["progress"] = {"stage": "remove", "total": len(inactive), "processed": 0}

        if not inactive:
            _task_status["result"] = {
                "total_members": len(members),
                "inactive_found": 0,
                "removed": 0,
                "errors": [],
            }
            log.info("Нечего удалять — все подписчики активны")
            return

        removed, errors = remove_users(GROUP_ID, inactive)
        _task_status["result"] = {
            "total_members": len(members),
            "inactive_found": len(inactive),
            "removed": removed,
            "errors": errors,
        }
        log.info("Очистка завершена: удалено %d из %d, ошибок %d",
                 removed, len(inactive), len(errors))
        if errors:
            log.warning("Не удалось удалить ID: %s", ", ".join(errors))
    except Exception:
        # Любой сбой не должен оставлять running=True навсегда
        log.exception("Очистка завершилась с ошибкой")
        _task_status["result"] = None
    finally:
        _task_status["running"] = False


# ──────────────────────────── Эндпоинты ────────────────────────────

@app.post("/clean")
def clean(background_tasks: BackgroundTasks):
    """Запускает очистку в фоне. 409, если задача уже идёт."""
    if _task_status["running"]:
        return JSONResponse(status_code=409, content={"error": "Задача уже выполняется"})

    _task_status.update(running=True, result=None, progress=None)
    background_tasks.add_task(run_cleanup)
    log.info("Принят запрос на запуск очистки")
    return {"started": True}


@app.get("/status")
def status():
    """Статус задачи: running, живой прогресс и результат."""
    return {
        "running": _task_status["running"],
        "progress": _task_status["progress"],
        "result": _task_status["result"],
    }


@app.get("/health")
def health():
    return {"status": "ok"}
