"""Own exactly the selected scheduler and listener for one fixed configuration."""
from __future__ import annotations

import logging
import signal
import threading
import uuid
from pathlib import Path


class _StopServing(BaseException):
    pass


def serve(config, plan, *, scheduler_factory, listener_factory):
    scheduler = listener = None
    stop = threading.Event()
    previous = {}
    listening = False

    def stopped(signum, frame):
        stop.set()
        if listening:
            raise _StopServing()

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stopped)
    result = 0
    try:
        if plan.start_scheduler:
            scheduler = scheduler_factory()
            scheduler.start()
        if plan.start_listener and not stop.is_set():
            listener = listener_factory(scheduler)
            listening = True
            listener.run()
        elif scheduler is not None:
            scheduler.wait(stop)
    except (_StopServing, KeyboardInterrupt):
        pass
    except Exception:
        logging.getLogger(__name__).error("Selected background component failed")
        result = 1
    finally:
        listening = False
        # A listener owns its command workers; stop them before the shared
        # scheduler so no late callback can submit another recorded task.
        for component in (listener, scheduler):
            if component is not None:
                try:
                    component.stop()
                except Exception:
                    logging.getLogger(__name__).error("Background cleanup failed")
                    result = 1
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return result


def reply_usage(readings, user_id, now, *, client=None):
    """A response to an authorized command is owned by the bot feature."""
    from quota_sentinel.runtime.cards import render_usage_card
    from quota_sentinel.runtime.feishu import FeishuClient
    transport = client if client is not None else FeishuClient()
    transport.send(render_usage_card(tuple(readings), readings, user_id,
                                    "reply-" + str(uuid.uuid4()), now=int(now)))


class ReplyNotifier:
    def __init__(self, client, user_id):
        self.client, self.user_id = client, user_id

    def validate_ready(self):
        self.client.require_credentials()

    def usage(self, readings, now):
        reply_usage(readings, self.user_id, now, client=self.client)

    def busy(self, now):
        from quota_sentinel.runtime.cards import render_busy_card
        self.client.send(render_busy_card(self.user_id, "reply-" + str(uuid.uuid4())))


def run_selected_host(config, plan, state_dir, config_path):
    """Lazy imports keep opening-only services independent of the bot SDK."""
    from task_orchestrator import TaskOrchestrator, ScheduleState, TaskStore
    from quota_sentinel.runtime.budgets import check_budget
    import sys
    state_dir, config_path = Path(state_dir).resolve(), Path(config_path).resolve()

    def scheduler_factory():
        return TaskOrchestrator(
            scheduler_command=(sys.executable, "-m", "quota_sentinel",
                               "--state-dir", str(state_dir), "--config", str(config_path), "check"),
            schedule_state=ScheduleState(state_dir, plan.opening_providers),
            store=TaskStore(state_dir / "task-orchestrator.sqlite3"),
            check_timeout=check_budget(config, plan),
        )

    def listener_factory(scheduler):
        from feishu_listener import FeishuListener
        return FeishuListener(config, state_dir, config_path, scheduler)

    return serve(config, plan, scheduler_factory=scheduler_factory,
                 listener_factory=listener_factory)
