import io
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import docker
import telebot
from apscheduler.schedulers.background import BackgroundScheduler
from docker.errors import NotFound
from flask import Flask, abort, jsonify, request, send_file
from git import Repo
from dotenv import load_dotenv
from telebot.apihelper import ApiTelegramException
from telebot import types
from waitress import serve

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
PROJECTS_DIR = Path(os.getenv("PROJECTS_ROOT", str(BASE_DIR / "projects"))).resolve()
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data"))).resolve()
DB_PATH = Path(os.getenv("SQLITE_PATH", str(DATA_DIR / "ozod.sqlite"))).resolve()
API_PORT = int(os.getenv("PORT", os.getenv("API_PORT", "8000")))
BOT_TOKEN = os.getenv("BOT_TOKEN", "8787937843:AAFrpXDJW0Vw9tRxlaZEMXBKYqmhc6RRy7E").strip()
API_TOKEN = os.getenv("API_TOKEN", "").strip()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ozodhosting.onrender.com").rstrip("/")
PROJECT_LIMIT = int(os.getenv("PROJECT_LIMIT_PER_USER", "2"))
DEPLOYMENT_TIMEOUT_SEC = max(int(os.getenv("DEPLOYMENT_TIMEOUT_MS", "600000")) // 1000, 60)
GITHUB_POLL_MINUTES = max(int(os.getenv("GITHUB_POLL_MINUTES", "10")), 1)
LIST_PAGE_SIZE = 5

STATUS_PENDING = "pending"
STATUS_BUILDING = "building"
STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_PAUSED = "paused"
STATUS_FAILED = "failed"
STATUS_DELETED = "deleted"

SOURCE_GITHUB = "github"
SOURCE_ZIP = "zip"

LABEL_CREATE = "📦 Create Project"
LABEL_LIST = "📁 My Projects"
LABEL_SETTINGS = "⚙️ Settings"
LABEL_SYSTEM = "📊 System Status"
LABEL_BACK = "⬅️ Back"

app = Flask(__name__)
bot: Optional[telebot.TeleBot] = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML") if BOT_TOKEN else None
docker_client: Optional[docker.DockerClient] = None
background_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
jobs = {}
jobs_lock = threading.Lock()
user_states = {}
local_processes = {}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


def status_badge(status: str) -> str:
    return {
        STATUS_PENDING: "⏳ pending",
        STATUS_BUILDING: "🛠️ building",
        STATUS_RUNNING: "🟢 running",
        STATUS_STOPPED: "⚫ stopped",
        STATUS_PAUSED: "⏸️ paused",
        STATUS_FAILED: "🔴 failed",
        STATUS_DELETED: "🗑️ deleted",
    }.get(status, f"ℹ️ {status}")


def project_mode_badge(project: dict) -> str:
    if project.get("is_static"):
        return "🌐 static website"
    if project.get("project_kind") == "node":
        return "🟨 node app"
    if project.get("project_kind") == "python":
        return "🐍 python app"
    return "📦 app"


def ensure_dirs() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "project"
    return f"{base}-{uuid.uuid4().hex[:6]}"


def get_docker_client() -> docker.DockerClient:
    global docker_client
    if docker_client is None:
        try:
            docker_client = docker.from_env()
            docker_client.ping()
        except Exception as exc:
            raise RuntimeError("Docker topilmadi. Docker Desktop ni ishga tushiring.") from exc
    return docker_client


def docker_is_available() -> bool:
    try:
        get_docker_client()
        return True
    except Exception:
        return False


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    ensure_dirs()
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              telegram_id TEXT UNIQUE NOT NULL,
              username TEXT,
              first_name TEXT,
              last_name TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS projects (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              slug TEXT UNIQUE NOT NULL,
              description TEXT,
              source_type TEXT NOT NULL,
              repo_url TEXT,
              zip_name TEXT,
              branch TEXT DEFAULT 'main',
              last_commit TEXT,
              auto_update INTEGER DEFAULT 0,
              bot_token TEXT,
              env_json TEXT,
              main_file TEXT NOT NULL,
              start_command TEXT NOT NULL,
              build_command TEXT,
              runtime TEXT,
              project_kind TEXT,
              is_static INTEGER DEFAULT 0,
              output_dir TEXT,
              status TEXT NOT NULL,
              container_id TEXT,
              public_url TEXT,
              work_dir TEXT,
              last_log TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deployments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              project_id INTEGER NOT NULL,
              kind TEXT NOT NULL,
              status TEXT NOT NULL,
              job_id TEXT,
              commit_hash TEXT,
              logs TEXT,
              error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
        if "bot_token" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN bot_token TEXT")
        if "env_json" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN env_json TEXT")


def row_to_dict(row):
    return dict(row) if row else None


def upsert_user(telegram_id: str, username: str = "", first_name: str = "", last_name: str = ""):
    stamp = now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_name=excluded.last_name,
              updated_at=excluded.updated_at
            """,
            (telegram_id, username, first_name, last_name, stamp, stamp),
        )
        return row_to_dict(conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone())


def get_user_by_telegram_id(telegram_id: str):
    with db() as conn:
        return row_to_dict(conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone())


def get_user_by_id(user_id: int):
    with db() as conn:
        return row_to_dict(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def count_active_projects(user_id: int) -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS total FROM projects WHERE user_id = ? AND status IN (?, ?, ?, ?)",
            (user_id, STATUS_PENDING, STATUS_BUILDING, STATUS_RUNNING, STATUS_PAUSED),
        ).fetchone()["total"]


def get_project(project_id: int):
    with db() as conn:
        return row_to_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())


def get_latest_deployment(project_id: int):
    with db() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM deployments WHERE project_id = ? ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        )


def list_projects_for_user(user_id: int, page: int = 1, limit: int = 5):
    offset = (page - 1) * limit
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM projects WHERE user_id = ? AND status != ?",
            (user_id, STATUS_DELETED),
        ).fetchone()["total"]
        rows = conn.execute(
            """
            SELECT * FROM projects
            WHERE user_id = ? AND status != ?
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (user_id, STATUS_DELETED, limit, offset),
        ).fetchall()
    return [row_to_dict(row) for row in rows], total


def create_project_record(payload: dict):
    stamp = now()
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO projects (
              user_id, name, slug, description, source_type, repo_url, zip_name, branch, last_commit, auto_update,
              bot_token, env_json, main_file, start_command, build_command, runtime, project_kind, is_static, output_dir,
              status, container_id, public_url, work_dir, last_log, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, '', '', 0, '', ?, '', '', '', '', ?, ?)
            """,
            (
                payload["user_id"],
                payload["name"],
                slugify(payload["name"]),
                payload.get("description", ""),
                payload["source_type"],
                payload.get("repo_url", ""),
                payload.get("zip_name", ""),
                payload.get("branch", "main"),
                1 if payload["source_type"] == SOURCE_GITHUB else 0,
                payload.get("bot_token", ""),
                payload.get("env_json", "{}"),
                payload["main_file"],
                payload["start_command"],
                payload.get("build_command", ""),
                STATUS_PENDING,
                stamp,
                stamp,
            ),
        )
        return row_to_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)).fetchone())


def create_deployment_record(project_id: int, kind: str = "deploy", status: str = STATUS_PENDING):
    stamp = now()
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO deployments (project_id, kind, status, job_id, commit_hash, logs, error, created_at, updated_at)
            VALUES (?, ?, ?, '', '', '', '', ?, ?)
            """,
            (project_id, kind, status, stamp, stamp),
        )
        return row_to_dict(conn.execute("SELECT * FROM deployments WHERE id = ?", (cursor.lastrowid,)).fetchone())


def cleanup_failed_projects(user_id: int) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM deployments WHERE project_id IN (SELECT id FROM projects WHERE user_id = ? AND status IN (?, ?))",
            (user_id, STATUS_FAILED, STATUS_DELETED),
        )
        conn.execute(
            "DELETE FROM projects WHERE user_id = ? AND status IN (?, ?)",
            (user_id, STATUS_FAILED, STATUS_DELETED),
        )
        conn.execute(
            """
            DELETE FROM projects
            WHERE user_id = ?
              AND status = ?
              AND IFNULL(container_id, '') = ''
              AND id NOT IN (SELECT project_id FROM deployments)
            """,
            (user_id, STATUS_PENDING),
        )


def update_project(project_id: int, **fields):
    if not fields:
        return get_project(project_id)
    fields["updated_at"] = now()
    cols = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [project_id]
    with db() as conn:
        conn.execute(f"UPDATE projects SET {cols} WHERE id = ?", values)
    return get_project(project_id)


def update_deployment(deployment_id: int, **fields):
    if not fields:
        with db() as conn:
            return row_to_dict(conn.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,)).fetchone())
    fields["updated_at"] = now()
    cols = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [deployment_id]
    with db() as conn:
        conn.execute(f"UPDATE deployments SET {cols} WHERE id = ?", values)
        return row_to_dict(conn.execute("SELECT * FROM deployments WHERE id = ?", (deployment_id,)).fetchone())


def github_candidates():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE source_type = ? AND auto_update = 1 AND status != ?",
            (SOURCE_GITHUB, STATUS_DELETED),
        ).fetchall()
    return [row_to_dict(row) for row in rows]


def validate_github_url(url: str) -> None:
    if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+(?:\.git)?$", url or ""):
        raise RuntimeError("Only direct GitHub repository URLs are allowed.")


def project_dir(project: dict) -> Path:
    return PROJECTS_DIR / project["slug"]


def project_log_path(project: dict) -> Path:
    return project_dir(project) / "runtime.log"


def safe_remove_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def detect_runtime(source_dir: Path, project: dict) -> dict:
    package_json = source_dir / "package.json"
    requirements_txt = source_dir / "requirements.txt"

    if package_json.exists():
        package = json.loads(package_json.read_text("utf-8"))
        deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
        build_command = project["build_command"] or package.get("scripts", {}).get("build", "")
        is_static = bool(build_command and any(dep in deps for dep in ("react", "vite", "next")))
        output_dir = "dist" if "vite" in deps else ".next" if "next" in deps else "build"
        return {
            "project_kind": "node",
            "runtime": "node:22-bookworm-slim",
            "build_command": build_command,
            "is_static": 1 if is_static else 0,
            "output_dir": output_dir if is_static else "",
        }

    if requirements_txt.exists():
        return {
            "project_kind": "python",
            "runtime": "python:3.12-slim",
            "build_command": project["build_command"] or "",
            "is_static": 0,
            "output_dir": "",
        }

    raise RuntimeError("Unsupported project type. package.json yoki requirements.txt bo'lishi kerak.")


def write_dockerfile(source_dir: Path, project: dict, runtime: dict) -> None:
    if runtime["project_kind"] == "node":
        lines = [
            f"FROM {runtime['runtime']}",
            "WORKDIR /app",
            "COPY . .",
            "RUN npm install --omit=dev",
            f"RUN {runtime['build_command']}" if runtime["build_command"] else "",
            "RUN useradd -r -u 1001 app",
            "USER 1001",
            f'CMD ["sh", "-lc", {json.dumps(project["start_command"])}]',
        ]
    else:
        lines = [
            f"FROM {runtime['runtime']}",
            "WORKDIR /app",
            "COPY . .",
            "RUN pip install --no-cache-dir -r requirements.txt",
            "RUN useradd -r -u 1001 app",
            "USER 1001",
            f'CMD ["sh", "-lc", {json.dumps(project["start_command"])}]',
        ]
    (source_dir / "Dockerfile").write_text("\n".join(line for line in lines if line) + "\n", encoding="utf-8")


def write_project_env(source_dir: Path, project: dict) -> None:
    env_path = source_dir / ".env"
    values = {}

    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()

    token = (project.get("bot_token") or "").strip()
    if token:
        values["BOT_TOKEN"] = token
        values["TELEGRAM_BOT_TOKEN"] = token
    else:
        values.pop("BOT_TOKEN", None)
        values.pop("TELEGRAM_BOT_TOKEN", None)

    try:
        custom_env = json.loads(project.get("env_json") or "{}")
        if isinstance(custom_env, dict):
            for key, value in custom_env.items():
                clean_key = str(key).strip()
                if not clean_key:
                    continue
                values[clean_key] = str(value)
    except Exception:
        pass

    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + ("\n" if values else ""),
        encoding="utf-8",
    )


def parse_secret_input(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise RuntimeError("Secret formati KEY=VALUE bo'lishi kerak.")
    key, value = text.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", key):
        raise RuntimeError("Secret key faqat A-Z, 0-9, _ bo'lishi kerak.")
    if not value:
        raise RuntimeError("Secret value bo'sh bo'lmasin.")
    return key, value


def prepare_source(project: dict) -> tuple[Path, str]:
    target = project_dir(project)
    safe_remove_dir(target)
    target.mkdir(parents=True, exist_ok=True)

    if project["source_type"] == SOURCE_GITHUB:
        Repo.clone_from(project["repo_url"], target, branch=project["branch"], depth=1)
        return target, Repo(target).head.commit.hexsha

    archive_path = PROJECTS_DIR / project["zip_name"]
    if not archive_path.exists():
        raise RuntimeError("ZIP file topilmadi.")

    with zipfile.ZipFile(archive_path, "r") as archive:
        archive.extractall(target)

    children = list(target.iterdir())
    if len(children) == 1 and children[0].is_dir():
        inner = children[0]
        for item in inner.iterdir():
            shutil.move(str(item), target / item.name)
        inner.rmdir()
    return target, ""


def build_static(source_dir: Path, runtime: dict, slug: str) -> str:
    if not runtime["is_static"] or not runtime["build_command"]:
        return ""
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{source_dir}:/app",
            "-w",
            "/app",
            runtime["runtime"],
            "sh",
            "-lc",
            f"npm install && {runtime['build_command']}",
        ],
        check=True,
        timeout=DEPLOYMENT_TIMEOUT_SEC,
    )
    return f"{PUBLIC_BASE_URL}/projects/{slug}/{runtime['output_dir']}/"


def start_local_process(project: dict, source_dir: Path) -> tuple[str, str, str]:
    log_path = project_log_path(project)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "",
        "PYTHONUNBUFFERED": "1",
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "WINDIR": os.environ.get("WINDIR", ""),
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "PATHEXT": os.environ.get("PATHEXT", ""),
    }
    project_env_path = source_dir / ".env"
    if project_env_path.exists():
        for raw_line in project_env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key.upper() in {
                "BOT_TOKEN",
                "API_TOKEN",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_TOKEN",
            } and value == BOT_TOKEN:
                continue
            child_env[key] = value

    requirements_path = source_dir / "requirements.txt"
    if requirements_path.exists():
        subprocess.run(
            ["python", "-m", "pip", "install", "-r", str(requirements_path)],
            cwd=str(source_dir),
            env=child_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=DEPLOYMENT_TIMEOUT_SEC,
            creationflags=creationflags,
        )

    process = subprocess.Popen(
        project["start_command"],
        cwd=str(source_dir),
        shell=True,
        env=child_env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    local_processes[project["id"]] = process
    return f"local:{process.pid}", "", f"Local mode started with PID {process.pid}"


def stop_local_process(project: dict) -> None:
    process = local_processes.get(project["id"])
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except Exception:
            process.kill()
    local_processes.pop(project["id"], None)
    container_id = project.get("container_id") or ""
    if container_id.startswith("local:"):
        try:
            pid = int(container_id.split(":", 1)[1])
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass


def remove_container(container_id: str) -> None:
    if not container_id:
        return
    if container_id.startswith("local:"):
        return
    try:
        get_docker_client().containers.get(container_id).remove(force=True)
    except NotFound:
        return


def run_container(project: dict, image_tag: str) -> tuple[str, str, str]:
    try:
        get_docker_client().containers.get(f"ozod-{project['slug']}").remove(force=True)
    except Exception:
        pass

    container = get_docker_client().containers.run(
        image_tag,
        detach=True,
        name=f"ozod-{project['slug']}",
        environment={"PORT": "3000", "NODE_ENV": "production"},
        ports={"3000/tcp": None},
        mem_limit="256m",
        nano_cpus=500000000,
        pids_limit=128,
        security_opt=["no-new-privileges"],
        cap_drop=["ALL"],
        tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
        user="1001:1001",
        restart_policy={"Name": "unless-stopped"},
    )
    container.reload()
    binding = container.attrs["NetworkSettings"]["Ports"].get("3000/tcp") or []
    host_port = binding[0]["HostPort"] if binding else ""
    logs = container.logs(stdout=True, stderr=True, tail=200).decode("utf-8", errors="ignore")[-12000:]
    return container.id, f"{PUBLIC_BASE_URL}:{host_port}" if host_port else "", logs


def deploy_project(project_id: int, deployment_id: int) -> None:
    project = get_project(project_id)
    if not project:
        raise RuntimeError("Project not found")

    update_project(project_id, status=STATUS_BUILDING)
    update_deployment(deployment_id, status=STATUS_BUILDING)

    try:
        source_dir, commit_hash = prepare_source(project)
        runtime = detect_runtime(source_dir, project)
        write_project_env(source_dir, project)
        write_dockerfile(source_dir, project, runtime)
        remove_container(project["container_id"] or "")
        stop_local_process(project)

        if docker_is_available():
            static_url = build_static(source_dir, runtime, project["slug"])
            image_tag = f"ozod/{project['slug']}:latest"
            get_docker_client().images.build(path=str(source_dir), tag=image_tag, rm=True)
            container_id, runtime_url, logs = run_container(project, image_tag)
        else:
            static_url = ""
            container_id, runtime_url, logs = start_local_process(project, source_dir)

        update_project(
            project_id,
            status=STATUS_RUNNING,
            container_id=container_id,
            public_url=static_url or runtime_url,
            work_dir=str(source_dir),
            last_log=logs,
            runtime=runtime["runtime"],
            project_kind=runtime["project_kind"],
            is_static=runtime["is_static"],
            output_dir=runtime["output_dir"],
            build_command=runtime["build_command"],
            last_commit=commit_hash,
        )
        update_deployment(deployment_id, status=STATUS_RUNNING, commit_hash=commit_hash, logs=logs)
    except Exception as exc:
        error = str(exc)
        update_project(project_id, status=STATUS_FAILED, last_log=error)
        update_deployment(deployment_id, status=STATUS_FAILED, error=error, logs=error)
        raise


def control_project(project_id: int, action: str) -> None:
    project = get_project(project_id)
    if not project:
        raise RuntimeError("Project not found")

    if action == "delete":
        remove_container(project["container_id"] or "")
        stop_local_process(project)
        safe_remove_dir(project_dir(project))
        update_project(project_id, status=STATUS_DELETED, container_id="", public_url="", work_dir="")
        return

    if not project["container_id"]:
        raise RuntimeError("Container not found")

    if project["container_id"].startswith("local:"):
        if action in {"pause", "stop"}:
            stop_local_process(project)
            update_project(project_id, status=STATUS_PAUSED if action == "pause" else STATUS_STOPPED)
            return
        if action in {"start", "restart"}:
            stop_local_process(project)
            container_id, runtime_url, logs = start_local_process(project, Path(project["work_dir"]))
            update_project(project_id, status=STATUS_RUNNING, container_id=container_id, public_url=runtime_url, last_log=logs)
            return
        raise RuntimeError("Unsupported action")

    container = get_docker_client().containers.get(project["container_id"])
    if action == "pause":
        container.pause()
        next_status = STATUS_PAUSED
    elif action == "start":
        try:
            container.unpause()
        except Exception:
            container.start()
        next_status = STATUS_RUNNING
    elif action == "restart":
        container.restart()
        next_status = STATUS_RUNNING
    elif action == "stop":
        container.stop(timeout=10)
        next_status = STATUS_STOPPED
    else:
        raise RuntimeError("Unsupported action")
    update_project(project_id, status=next_status)


def latest_logs(project: dict) -> bytes:
    if not project["container_id"]:
        return (project["last_log"] or "").encode("utf-8")
    if project["container_id"].startswith("local:"):
        log_path = project_log_path(project)
        if log_path.exists():
            return log_path.read_bytes()[-12000:]
        return (project["last_log"] or "").encode("utf-8")
    try:
        return get_docker_client().containers.get(project["container_id"]).logs(stdout=True, stderr=True, tail=300)
    except Exception:
        return (project["last_log"] or "").encode("utf-8")


def check_github_update(project: dict) -> tuple[bool, str]:
    temp_dir = PROJECTS_DIR / f".check-{project['slug']}"
    safe_remove_dir(temp_dir)
    Repo.clone_from(project["repo_url"], temp_dir, branch=project["branch"], depth=1)
    commit_hash = Repo(temp_dir).head.commit.hexsha
    safe_remove_dir(temp_dir)
    return commit_hash != (project["last_commit"] or ""), commit_hash


def enqueue_job(job_type: str, payload: dict) -> str:
    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {"id": job_id, "type": job_type, "status": STATUS_PENDING, "error": "", "created_at": now()}
    background_queue.put((job_type, {"job_id": job_id, **payload}))
    return job_id


def notify_project_owner(project_id: int, text: str) -> None:
    if not bot:
        return
    try:
        project = get_project(project_id)
        if not project:
            return
        user = get_user_by_id(project["user_id"])
        if not user or not user.get("telegram_id"):
            return
        bot.send_message(int(user["telegram_id"]), text)
    except Exception as exc:
        log(f"notify failed: {exc}")


def set_job_status(job_id: str, status: str, error: str = "") -> None:
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id]["status"] = status
            jobs[job_id]["error"] = error


def worker_loop() -> None:
    while True:
        job_type, payload = background_queue.get()
        job_id = payload["job_id"]
        set_job_status(job_id, STATUS_BUILDING)
        try:
            if job_type == "deploy":
                deploy_project(payload["project_id"], payload["deployment_id"])
                project = get_project(payload["project_id"])
                if project:
                    notify_project_owner(
                        payload["project_id"],
                        "\n".join(
                            [
                                "✅ <b>Deploy Success</b>",
                                f"📦 {project['name']}",
                                f"📊 {status_badge(project['status'])}",
                                f"🌐 URL: {project['public_url'] or 'n/a'}",
                            ]
                        ),
                    )
            elif job_type == "control":
                control_project(payload["project_id"], payload["action"])
                project = get_project(payload["project_id"])
                if project:
                    notify_project_owner(
                        payload["project_id"],
                        "\n".join(
                            [
                                "✅ <b>Action Success</b>",
                                f"⚙️ {payload['action']}",
                                f"📦 {project['name']}",
                                f"📊 {status_badge(project['status'])}",
                            ]
                        ),
                    )
            else:
                raise RuntimeError("Unknown job type")
            set_job_status(job_id, STATUS_RUNNING)
        except Exception as exc:
            set_job_status(job_id, STATUS_FAILED, str(exc))
            log(f"job failed: {exc}")
            if "project_id" in payload:
                notify_project_owner(
                    payload["project_id"],
                    f"❌ <b>Job Failed</b>\n🧩 {job_type}\n<code>{exc}</code>",
                )
        finally:
            background_queue.task_done()


def github_poll() -> None:
    for project in github_candidates():
        try:
            has_update, _ = check_github_update(project)
            if has_update:
                deployment = create_deployment_record(project["id"], kind="update")
                job_id = enqueue_job("deploy", {"project_id": project["id"], "deployment_id": deployment["id"]})
                update_deployment(deployment["id"], job_id=job_id)
        except Exception as exc:
            log(f"github poll skipped: {exc}")


def api_auth() -> None:
    if request.path in {"/", "/health", "/favicon.ico"}:
        return
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not API_TOKEN or token != API_TOKEN:
        abort(401)


app.before_request(api_auth)


@app.get("/")
def api_root():
    return jsonify(
        {
            "ok": True,
            "service": "Ozod Hosting",
            "docker_available": docker_is_available(),
            "bot_enabled": bool(BOT_TOKEN),
        }
    )


@app.get("/favicon.ico")
def api_favicon():
    return ("", 204)


@app.get("/health")
def api_health():
    return jsonify({"ok": True, "bot": bool(BOT_TOKEN)})


@app.post("/users/sync")
def api_sync_user():
    body = request.get_json(force=True)
    return jsonify(
        upsert_user(
            str(body["telegramId"]),
            body.get("username", ""),
            body.get("firstName", ""),
            body.get("lastName", ""),
        )
    )


@app.get("/projects")
def api_projects():
    page = max(int(request.args.get("page", "1")), 1)
    user = get_user_by_telegram_id(str(request.args.get("telegramId", "")))
    if not user:
        return jsonify({"items": [], "page": page, "pages": 0, "total": 0})
    items, total = list_projects_for_user(user["id"], page)
    return jsonify({"items": items, "page": page, "pages": (total + 4) // 5, "total": total})


@app.get("/projects/<int:project_id>")
def api_project(project_id: int):
    project = get_project(project_id)
    if not project or project["status"] == STATUS_DELETED:
        abort(404)
    return jsonify({"project": project, "deployment": get_latest_deployment(project_id)})


@app.post("/projects")
def api_create_project():
    body = request.get_json(force=True)
    user = get_user_by_telegram_id(str(body["telegramId"]))
    if not user:
        abort(404, "User not found")
    cleanup_failed_projects(user["id"])
    if PROJECT_LIMIT > 0 and count_active_projects(user["id"]) >= PROJECT_LIMIT:
        abort(400, f"Project limit is {PROJECT_LIMIT}")
    if body["sourceType"] == SOURCE_GITHUB:
        validate_github_url(body.get("repoUrl", ""))
    project = create_project_record(
        {
            "user_id": user["id"],
            "name": body["name"],
            "description": body.get("description", ""),
            "source_type": body["sourceType"],
            "repo_url": body.get("repoUrl", ""),
            "zip_name": body.get("zipFileName", ""),
            "branch": body.get("branch", "main"),
            "bot_token": body.get("botToken", ""),
            "main_file": body["mainFile"],
            "start_command": body["startCommand"],
            "build_command": body.get("buildCommand", ""),
        }
    )
    deployment = create_deployment_record(project["id"])
    job_id = enqueue_job("deploy", {"project_id": project["id"], "deployment_id": deployment["id"]})
    deployment = update_deployment(deployment["id"], job_id=job_id)
    return jsonify({"project": project, "deployment": deployment, "jobId": job_id}), 202


@app.post("/projects/<int:project_id>/control")
def api_control(project_id: int):
    if not get_project(project_id):
        abort(404)
    action = request.get_json(force=True)["action"]
    job_id = enqueue_job("control", {"project_id": project_id, "action": action})
    return jsonify({"ok": True, "jobId": job_id}), 202


@app.delete("/projects/<int:project_id>")
def api_delete(project_id: int):
    if not get_project(project_id):
        abort(404)
    job_id = enqueue_job("control", {"project_id": project_id, "action": "delete"})
    return jsonify({"ok": True, "jobId": job_id}), 202


@app.get("/projects/<int:project_id>/logs")
def api_logs(project_id: int):
    project = get_project(project_id)
    if not project:
        abort(404)
    buffer = io.BytesIO(latest_logs(project))
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{project['slug']}.log", mimetype="text/plain")


@app.get("/jobs/<job_id>")
def api_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        abort(404)
    return jsonify(job)


def main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(LABEL_CREATE, LABEL_LIST)
    markup.row(LABEL_SETTINGS, LABEL_SYSTEM)
    return markup


def circle_number(value: int) -> str:
    circles = {
        1: "①",
        2: "②",
        3: "③",
        4: "④",
        5: "⑤",
        6: "⑥",
        7: "⑦",
        8: "⑧",
        9: "⑨",
        10: "⑩",
    }
    return circles.get(value, str(value))


def reset_state(chat_id: int) -> None:
    user_states[chat_id] = {"flow": None, "project": {}}


def sync_bot_user(message) -> None:
    upsert_user(
        str(message.from_user.id),
        message.from_user.username or "",
        message.from_user.first_name or "",
        message.from_user.last_name or "",
    )


def send_project_cards(chat_id: int, telegram_id: str, page: int = 1) -> None:
    user = get_user_by_telegram_id(telegram_id)
    if not user:
        bot.send_message(chat_id, "📁 Hali hech qanday project yo'q.\n\n`📦 Create Project` dan boshlang.", reply_markup=main_keyboard())
        return
    limit = LIST_PAGE_SIZE
    items, total = list_projects_for_user(user["id"], page, limit)
    if not items:
        bot.send_message(chat_id, "📁 Hali hech qanday project yo'q.\n\n`📦 Create Project` dan boshlang.", reply_markup=main_keyboard())
        return
    lines = ["📁 <b>My Projects</b>", ""]
    for index, item in enumerate(items, start=1 + (page - 1) * limit):
        icon = "🌐" if item.get("is_static") else "🤖" if item.get("bot_token") else "📦"
        mode = "Web App" if item.get("is_static") else "Python Bot" if item.get("project_kind") == "python" else "Node App" if item.get("project_kind") == "node" else "App"
        lines.append(f"{circle_number(index)} {icon} <b>{item['name']}</b>")
        lines.append(mode)
        lines.append(f"📊 {status_badge(item['status'])}")
        lines.append("")
    pages = (total + limit - 1) // limit
    lines.append(f"Sahifa: {page}/{max(pages, 1)}")
    lines.append("Projectni tanlang:")
    markup = types.InlineKeyboardMarkup(row_width=5)
    buttons = []
    for index, item in enumerate(items, start=1 + (page - 1) * limit):
        buttons.append(types.InlineKeyboardButton(circle_number(index), callback_data=f"project:{item['id']}"))
    markup.add(*buttons)
    if pages > 1:
        nav = []
        if page > 1:
            nav.append(types.InlineKeyboardButton("◀️", callback_data=f"page:{page - 1}"))
        nav.append(types.InlineKeyboardButton(f"{page}/{pages}", callback_data="noop"))
        if page < pages:
            nav.append(types.InlineKeyboardButton("▶️", callback_data=f"page:{page + 1}"))
        markup.row(*nav)
    bot.send_message(chat_id, "\n".join(lines).strip(), reply_markup=markup)


def render_secrets(project: dict) -> str:
    try:
        secrets = json.loads(project.get("env_json") or "{}")
        if not isinstance(secrets, dict):
            secrets = {}
    except Exception:
        secrets = {}
    items = []
    if (project.get("bot_token") or "").strip():
        items.append("BOT_TOKEN = ********")
    for key, value in secrets.items():
        masked = str(value)
        if len(masked) > 8:
            masked = f"{masked[:4]}...{masked[-2:]}"
        else:
            masked = "********"
        items.append(f"{key} = {masked}")
    if not items:
        return "🧾 Secretlar yo'q."
    return "🧾 <b>Secrets</b>\n\n" + "\n".join(f"• <code>{line}</code>" for line in items)


def send_settings_panel(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "\n".join(
            [
                "⚙️ <b>Settings</b>",
                "",
                "🔐 Update Token va ➕ Add Secret project detail ichida mavjud.",
                "🌐 Website projectlar deploy bo'lsa URL project detail ichida chiqadi.",
                "📄 Logs tugmasi orqali runtime loglarni yuklab olasiz.",
            ]
        ),
        reply_markup=main_keyboard(),
    )


def send_system_status(chat_id: int, telegram_id: str) -> None:
    user = get_user_by_telegram_id(telegram_id)
    total_projects = 0
    running_projects = 0
    if user:
        items, total_projects = list_projects_for_user(user["id"], 1, 100)
        running_projects = sum(1 for item in items if item["status"] == STATUS_RUNNING)
    bot.send_message(
        chat_id,
        "\n".join(
            [
                "📊 <b>System Status</b>",
                "",
                f"🐳 Docker: {'✅ available' if docker_is_available() else '⚠️ local mode'}",
                f"🤖 Bot: {'✅ enabled' if bot else '❌ disabled'}",
                f"📦 Projects: {total_projects}",
                f"🟢 Running: {running_projects}",
                f"🌐 Base URL: {PUBLIC_BASE_URL}",
            ]
        ),
        reply_markup=main_keyboard(),
    )


if bot:
    @bot.message_handler(commands=["start"])
    def handle_start(message):
        sync_bot_user(message)
        user = get_user_by_telegram_id(str(message.from_user.id))
        if user:
            cleanup_failed_projects(user["id"])
        reset_state(message.chat.id)
        bot.send_message(
            message.chat.id,
            "\n".join(
                [
                    f"🚀 <b>Ozod Hosting</b>",
                    f"Xush kelibsiz, <b>{message.from_user.first_name or 'foydalanuvchi'}</b>.",
                    "",
                    "Quyidagilardan birini tanlang:"    
                ]
            ),
            reply_markup=main_keyboard(),
        )


    @bot.message_handler(func=lambda message: message.text == LABEL_CREATE)
    def handle_create(message):
        reset_state(message.chat.id)
        user_states[message.chat.id]["flow"] = "source"
        markup = types.InlineKeyboardMarkup()
        markup.row(
            types.InlineKeyboardButton("🌐 GitHub", callback_data="source:github"),
            types.InlineKeyboardButton("🗜️ ZIP", callback_data="source:zip"),
        )
        bot.send_message(
            message.chat.id,
            "📦 <b>New Project</b>\n\n1/7 Manba turini tanlang:",
            reply_markup=markup,
        )


    @bot.message_handler(func=lambda message: message.text == LABEL_LIST)
    def handle_list(message):
        user = get_user_by_telegram_id(str(message.from_user.id))
        if user:
            cleanup_failed_projects(user["id"])
        send_project_cards(message.chat.id, str(message.from_user.id), 1)


    @bot.message_handler(func=lambda message: message.text == LABEL_SETTINGS)
    def handle_settings(message):
        send_settings_panel(message.chat.id)


    @bot.message_handler(func=lambda message: message.text == LABEL_SYSTEM)
    def handle_system(message):
        send_system_status(message.chat.id, str(message.from_user.id))


    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback(call):
        state = user_states.setdefault(call.message.chat.id, {"flow": None, "project": {}})
        data = call.data or ""

        if data.startswith("source:"):
            source = data.split(":")[1]
            state["project"]["sourceType"] = source
            state["flow"] = "repoUrl" if source == SOURCE_GITHUB else "zip"
            bot.answer_callback_query(call.id)
            bot.send_message(
                call.message.chat.id,
                "📦 <b>New Project</b>\n\n2/7 GitHub repo URL yuboring." if source == SOURCE_GITHUB else "📦 <b>New Project</b>\n\n2/7 ZIP fayl yuboring.",
            )
            return

        if data.startswith("page:"):
            bot.answer_callback_query(call.id)
            send_project_cards(call.message.chat.id, str(call.from_user.id), int(data.split(":")[1]))
            return

        if data == "noop":
            bot.answer_callback_query(call.id)
            return

        if data == "back:projects":
            bot.answer_callback_query(call.id)
            send_project_cards(call.message.chat.id, str(call.from_user.id), 1)
            return

        if data.startswith("project:"):
            project = get_project(int(data.split(":")[1]))
            if not project:
                bot.answer_callback_query(call.id, "Project topilmadi.")
                return
            description = project["description"] or "Tavsif yo'q"
            text = "\n".join(
                [
                    f"📦 <b>{project['name']}</b>",
                    f"📝 {description}",
                    f"📊 {status_badge(project['status'])}",
                    f"🧩 {project_mode_badge(project)}",
                    f"🔐 Token: {'✅ set' if (project.get('bot_token') or '').strip() else '❌ missing'}",
                    f"📄 Main: <code>{project['main_file']}</code>",
                    f"▶️ Start: <code>{project['start_command']}</code>",
                    f"🌐 URL: {project['public_url'] or 'n/a'}",
                ]
            )
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton("⏸️ Pause", callback_data=f"action:{project['id']}:pause"),
                types.InlineKeyboardButton("▶️ Start", callback_data=f"action:{project['id']}:start"),
            )
            markup.row(
                types.InlineKeyboardButton("🔄 Restart", callback_data=f"action:{project['id']}:restart"),
                types.InlineKeyboardButton("🗑️ Delete", callback_data=f"action:{project['id']}:delete"),
            )
            markup.row(
                types.InlineKeyboardButton("📄 Logs", callback_data=f"logs:{project['id']}"),
                types.InlineKeyboardButton("🔐 Update Token", callback_data=f"update_token:{project['id']}"),
            )
            markup.row(types.InlineKeyboardButton("➕ Add Secret", callback_data=f"add_secret:{project['id']}"))
            markup.row(types.InlineKeyboardButton("🌐 Open URL", url=project["public_url"])) if project.get("public_url") else None
            markup.row(
                types.InlineKeyboardButton("🧾 View Secrets", callback_data=f"view_secrets:{project['id']}"),
                types.InlineKeyboardButton("⬅️ Back", callback_data="back:projects"),
            )
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, text, reply_markup=markup)
            return

        if data.startswith("logs:"):
            project = get_project(int(data.split(":")[1]))
            if not project:
                bot.answer_callback_query(call.id, "Project topilmadi.")
                return
            bot.answer_callback_query(call.id)
            bot.send_document(call.message.chat.id, ("logs.txt", latest_logs(project)))
            return

        if data.startswith("action:"):
            _, project_id, action = data.split(":")
            bot.answer_callback_query(call.id, "⏳ Queue ga yuborildi.")
            enqueue_job("control", {"project_id": int(project_id), "action": action})
            bot.send_message(call.message.chat.id, f"⚙️ <b>{action}</b> jarayoni queue ga yuborildi.")
            return

        if data.startswith("update_token:"):
            project_id = int(data.split(":")[1])
            state["flow"] = "updateToken"
            state["project"] = {"projectId": project_id}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "🔐 Yangi <b>BOT_TOKEN</b> yuboring.\nO'chirish uchun <code>skip</code> deb yozing.")
            return

        if data.startswith("add_secret:"):
            project_id = int(data.split(":")[1])
            state["flow"] = "addSecret"
            state["project"] = {"projectId": project_id}
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "➕ Secret yuboring:\n<code>KEY=VALUE</code>")

        if data.startswith("view_secrets:"):
            project_id = int(data.split(":")[1])
            project = get_project(project_id)
            if not project:
                bot.answer_callback_query(call.id, "Project topilmadi.")
                return
            markup = types.InlineKeyboardMarkup()
            try:
                secret_map = json.loads(project.get("env_json") or "{}")
                if not isinstance(secret_map, dict):
                    secret_map = {}
            except Exception:
                secret_map = {}
            for key in secret_map.keys():
                markup.add(types.InlineKeyboardButton(f"❌ Delete {key}", callback_data=f"delete_secret:{project_id}:{key}"))
            markup.row(types.InlineKeyboardButton("⬅️ Back", callback_data=f"project:{project_id}"))
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, render_secrets(project), reply_markup=markup)
            return

        if data.startswith("delete_secret:"):
            _, project_id, key = data.split(":", 2)
            project = get_project(int(project_id))
            if not project:
                bot.answer_callback_query(call.id, "Project topilmadi.")
                return
            try:
                current = json.loads(project.get("env_json") or "{}")
                if not isinstance(current, dict):
                    current = {}
                current.pop(key, None)
                update_project(int(project_id), env_json=json.dumps(current))
                bot.answer_callback_query(call.id, "Secret o'chirildi.")
                updated = get_project(int(project_id))
                markup = types.InlineKeyboardMarkup()
                latest = json.loads(updated.get("env_json") or "{}")
                if isinstance(latest, dict):
                    for latest_key in latest.keys():
                        markup.add(types.InlineKeyboardButton(f"❌ Delete {latest_key}", callback_data=f"delete_secret:{project_id}:{latest_key}"))
                markup.row(types.InlineKeyboardButton("⬅️ Back", callback_data=f"project:{project_id}"))
                bot.send_message(call.message.chat.id, render_secrets(updated), reply_markup=markup)
            except Exception as exc:
                bot.answer_callback_query(call.id, f"Xatolik: {exc}")

    @bot.message_handler(content_types=["document"])
    def handle_document(message):
        state = user_states.setdefault(message.chat.id, {"flow": None, "project": {}})
        if state["flow"] != "zip":
            return
        file_name = message.document.file_name or ""
        if not file_name.lower().endswith(".zip"):
            bot.send_message(message.chat.id, "❌ Faqat <b>ZIP</b> fayl yuboring.")
            return
        file_info = bot.get_file(message.document.file_id)
        file_data = bot.download_file(file_info.file_path)
        archive_name = f"{uuid.uuid4().hex[:8]}-{Path(file_name).name}"
        (PROJECTS_DIR / archive_name).write_bytes(file_data)
        state["project"]["zipFileName"] = archive_name
        state["flow"] = "name"
        bot.send_message(message.chat.id, "📦 <b>New Project</b>\n\n3/7 Project nomini yuboring.")


    @bot.message_handler(content_types=["text"])
    def handle_text(message):
        state = user_states.setdefault(message.chat.id, {"flow": None, "project": {}})
        flow = state["flow"]
        text = (message.text or "").strip()
        if not flow or text in {LABEL_CREATE, LABEL_LIST, LABEL_SETTINGS, LABEL_SYSTEM, LABEL_BACK}:
            return

        if flow == "repoUrl":
            state["project"]["repoUrl"] = text
            state["flow"] = "name"
            bot.send_message(message.chat.id, "📦 <b>New Project</b>\n\n3/7 Project nomini yuboring.")
            return
        if flow == "name":
            state["project"]["name"] = text
            state["flow"] = "description"
            bot.send_message(message.chat.id, "📦 <b>New Project</b>\n\n4/7 Description yuboring.")
            return
        if flow == "description":
            state["project"]["description"] = text
            state["flow"] = "mainFile"
            bot.send_message(message.chat.id, "📦 <b>New Project</b>\n\n5/7 Main file kiriting.\nMasalan: <code>index.js</code> yoki <code>main.py</code>")
            return
        if flow == "mainFile":
            state["project"]["mainFile"] = text
            state["flow"] = "startCommand"
            bot.send_message(message.chat.id, "📦 <b>New Project</b>\n\n6/7 Start command kiriting.\nMasalan: <code>node index.js</code>")
            return
        if flow == "startCommand":
            state["project"]["startCommand"] = text
            state["flow"] = "buildCommand"
            bot.send_message(message.chat.id, "📦 <b>New Project</b>\n\n7/7 Build command yuboring.\nBo'lmasa <code>skip</code> deb yozing.")
            return
        if flow == "buildCommand":
            state["project"]["buildCommand"] = "" if text.lower() == "skip" else text
            state["flow"] = "botToken"
            bot.send_message(message.chat.id, "🔐 <b>Optional Bot Token</b>\nDeploy qilinadigan bot uchun <code>BOT_TOKEN</code> yuboring.\nBot bo'lmasa <code>skip</code> deb yozing.")
            return
        if flow == "botToken":
            try:
                if "sourceType" not in state["project"]:
                    reset_state(message.chat.id)
                    raise RuntimeError("Flow buzilgan. Qaytadan Create Project bosing.")
                token_text = "" if text.lower() == "skip" else text
                if token_text and not re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", token_text):
                    raise RuntimeError("BOT_TOKEN noto'g'ri formatda.")
                state["project"]["botToken"] = token_text
                user = get_user_by_telegram_id(str(message.from_user.id))
                if not user:
                    sync_bot_user(message)
                    user = get_user_by_telegram_id(str(message.from_user.id))
                if not user:
                    raise RuntimeError("User topilmadi")
                cleanup_failed_projects(user["id"])
                if PROJECT_LIMIT > 0 and count_active_projects(user["id"]) >= PROJECT_LIMIT:
                    raise RuntimeError(f"Project limit is {PROJECT_LIMIT}")
                if state["project"]["sourceType"] == SOURCE_GITHUB:
                    validate_github_url(state["project"].get("repoUrl", ""))
                if state["project"]["sourceType"] == SOURCE_ZIP and not state["project"].get("zipFileName"):
                    raise RuntimeError("ZIP file topilmadi. Qaytadan ZIP yuboring.")
                project = create_project_record(
                    {
                        "user_id": user["id"],
                        "name": state["project"]["name"],
                        "description": state["project"].get("description", ""),
                        "source_type": state["project"]["sourceType"],
                        "repo_url": state["project"].get("repoUrl", ""),
                        "zip_name": state["project"].get("zipFileName", ""),
                        "branch": "main",
                        "bot_token": state["project"].get("botToken", ""),
                        "main_file": state["project"]["mainFile"],
                        "start_command": state["project"]["startCommand"],
                        "build_command": state["project"]["buildCommand"],
                    }
                )
                deployment = create_deployment_record(project["id"])
                job_id = enqueue_job("deploy", {"project_id": project["id"], "deployment_id": deployment["id"]})
                update_deployment(deployment["id"], job_id=job_id)
                bot.send_message(
                    message.chat.id,
                    f"🚀 <b>Deploy queued</b>\n\n📦 {project['name']}\n🆔 Job: <code>{job_id[:8]}</code>\n⏳ Jarayon boshlandi.",
                )
                reset_state(message.chat.id)
            except Exception as exc:
                bot.send_message(message.chat.id, f"❌ <b>Xatolik</b>\n<code>{exc}</code>")
            return
        if flow == "updateToken":
            try:
                project_id = int(state["project"]["projectId"])
                token_text = "" if text.lower() == "skip" else text
                if token_text and not re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", token_text):
                    raise RuntimeError("BOT_TOKEN noto'g'ri formatda.")
                update_project(project_id, bot_token=token_text)
                bot.send_message(message.chat.id, "✅ <b>BOT_TOKEN yangilandi.</b>\nO'zgarishlar restartdan keyin qo'llanadi.")
                reset_state(message.chat.id)
            except Exception as exc:
                bot.send_message(message.chat.id, f"❌ <b>Xatolik</b>\n<code>{exc}</code>")
            return
        if flow == "addSecret":
            try:
                project_id = int(state["project"]["projectId"])
                project = get_project(project_id)
                if not project:
                    raise RuntimeError("Project topilmadi.")
                key, value = parse_secret_input(text)
                current = json.loads(project.get("env_json") or "{}")
                if not isinstance(current, dict):
                    current = {}
                current[key] = value
                update_project(project_id, env_json=json.dumps(current))
                bot.send_message(message.chat.id, f"✅ <b>Secret saqlandi</b>\n🔑 <code>{key}</code>")
                reset_state(message.chat.id)
            except Exception as exc:
                bot.send_message(message.chat.id, f"❌ <b>Xatolik</b>\n<code>{exc}</code>")


def run_bot():
    if not bot:
        log("BOT_TOKEN yo'q. Bot disabled.")
        return
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True)
    except ApiTelegramException as exc:
        if "409" in str(exc) or "terminated by other getUpdates request" in str(exc):
            log("Bot polling conflict: boshqa bot instance ishlayapti. Faqat bitta instance qoldiring.")
            return
        raise


def main():
    init_db()
    threading.Thread(target=worker_loop, daemon=True).start()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(github_poll, "interval", minutes=GITHUB_POLL_MINUTES, max_instances=1)
    scheduler.start()

    if bot:
        threading.Thread(target=run_bot, daemon=True).start()
    else:
        log("BOT_TOKEN yo'q. Faqat API ishlaydi.")

    log(f"API listening on {API_PORT}")
    serve(app, host="0.0.0.0", port=API_PORT, threads=16)


if __name__ == "__main__":
    main()
