import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
bind = f"{os.getenv('LISTEN_HOST', '0.0.0.0')}:{os.getenv('LISTEN_PORT', '9081')}"
# Keep a single worker because the bot starts in-process polling and dispatch threads.
workers = 1
timeout = 120
graceful_timeout = 30
keepalive = 5
log_dir = Path(os.getenv("LOG_DIR") or PROJECT_ROOT.joinpath("data", "logs"))
if not log_dir.is_absolute():
    log_dir = PROJECT_ROOT / log_dir
log_dir.mkdir(parents=True, exist_ok=True)
accesslog = str(log_dir / "gunicorn.access.log")
errorlog = str(log_dir / "gunicorn.error.log")
capture_output = True


def post_worker_init(worker):
    import src.main as main_module

    env_file = Path(
        os.getenv("CODER_BOT_ENV_FILE")
        or os.getenv("CODING_BOT_ENV_FILE")
        or Path(__file__).with_name("coder-bot.env")
    ).expanduser()
    main_module.bootstrap_service(env_file.resolve(strict=False))
