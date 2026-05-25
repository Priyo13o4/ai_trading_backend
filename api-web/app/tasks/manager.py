import asyncio
import logging

logger = logging.getLogger(__name__)

class TaskManager:
    _strategy_expiry_janitor_task = None
    _strategy_expiry_janitor_stop = None
    _session_index_prune_janitor_task = None
    _session_index_prune_janitor_stop = None
    _deferred_cancellation_janitor_task = None
    _deferred_cancellation_janitor_stop = None
    _plisio_renewal_janitor_task = None
    _plisio_renewal_janitor_stop = None
    _webhook_events_worker_task = None
    _webhook_events_worker_stop = None

    @classmethod
    def start_tasks(cls,
                    strategy_loop_func,
                    session_prune_func,
                    deferred_cancellation_func,
                    plisio_renewal_func,
                    webhook_worker_func,
                    session_index_prune_enabled: bool):
        if cls._strategy_expiry_janitor_task is None:
            cls._strategy_expiry_janitor_stop = asyncio.Event()
            cls._strategy_expiry_janitor_task = asyncio.create_task(
                strategy_loop_func(cls._strategy_expiry_janitor_stop)
            )

        if session_index_prune_enabled and cls._session_index_prune_janitor_task is None:
            cls._session_index_prune_janitor_stop = asyncio.Event()
            cls._session_index_prune_janitor_task = asyncio.create_task(
                session_prune_func(cls._session_index_prune_janitor_stop)
            )

        if cls._deferred_cancellation_janitor_task is None:
            cls._deferred_cancellation_janitor_stop = asyncio.Event()
            cls._deferred_cancellation_janitor_task = asyncio.create_task(
                deferred_cancellation_func(cls._deferred_cancellation_janitor_stop)
            )

        if cls._plisio_renewal_janitor_task is None:
            cls._plisio_renewal_janitor_stop = asyncio.Event()
            cls._plisio_renewal_janitor_task = asyncio.create_task(
                plisio_renewal_func(cls._plisio_renewal_janitor_stop)
            )

        if cls._webhook_events_worker_task is None:
            cls._webhook_events_worker_stop = asyncio.Event()
            cls._webhook_events_worker_task = asyncio.create_task(
                webhook_worker_func(cls._webhook_events_worker_stop)
            )

    @classmethod
    async def stop_tasks(cls):
        tasks_to_stop = [
            (cls._strategy_expiry_janitor_stop, cls._strategy_expiry_janitor_task, "strategy janitor"),
            (cls._session_index_prune_janitor_stop, cls._session_index_prune_janitor_task, "session index janitor"),
            (cls._deferred_cancellation_janitor_stop, cls._deferred_cancellation_janitor_task, "deferred cancellation janitor"),
            (cls._plisio_renewal_janitor_stop, cls._plisio_renewal_janitor_task, "Plisio renewal janitor"),
            (cls._webhook_events_worker_stop, cls._webhook_events_worker_task, "webhook worker")
        ]

        for stop_event, task, name in tasks_to_stop:
            if stop_event is not None:
                stop_event.set()

        for stop_event, task, name in tasks_to_stop:
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=10)
                except asyncio.TimeoutError:
                    logger.warning(f"[JANITOR] Timed out waiting for {name} to stop, cancelling task")
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                except Exception as exc:
                    logger.error(f"[JANITOR] {name} shutdown failed: %s", exc, exc_info=True)

        cls._strategy_expiry_janitor_task = None
        cls._strategy_expiry_janitor_stop = None
        cls._session_index_prune_janitor_task = None
        cls._session_index_prune_janitor_stop = None
        cls._deferred_cancellation_janitor_task = None
        cls._deferred_cancellation_janitor_stop = None
        cls._plisio_renewal_janitor_task = None
        cls._plisio_renewal_janitor_stop = None
        cls._webhook_events_worker_task = None
        cls._webhook_events_worker_stop = None
