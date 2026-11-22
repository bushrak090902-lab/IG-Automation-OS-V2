"""Isolated test-mode order processor. No Instagram engagement actions."""

import asyncio
import logging
from datetime import datetime, timezone

from ..database import SessionLocal
from ..models import InstagramServiceOrder as Order
from ..models import InstagramServiceOrderLog as OrderLog

logger = logging.getLogger("ig_automation_os.services_test")


class ServicesTestWorker:
    def __init__(self):
        self._task = None
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Instagram Services test worker started")

    async def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Instagram Services test worker stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._process_next()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Test worker iteration failed")
            await asyncio.sleep(5)

    async def _process_next(self):
        db = SessionLocal()
        try:
            order = (
                db.query(Order)
                .filter(
                    Order.execution_mode == "test",
                    Order.status == "pending",
                )
                .order_by(Order.id)
                .first()
            )
            if order is None:
                return

            order_id = order.id
            order.status = "processing"
            db.add(OrderLog(
                order_id=order_id,
                event_type="test_started",
                message="Isolated test execution started; no Instagram actions",
            ))
            db.commit()
        finally:
            db.close()

        await asyncio.sleep(2)

        db = SessionLocal()
        try:
            order = db.get(Order, order_id)
            if order is None or order.execution_mode != "test":
                return
            if order.status != "processing":
                return

            order.status = "completed"
            order.completed_quantity = 0
            order.completed_at = datetime.now(timezone.utc)
            db.add(OrderLog(
                order_id=order_id,
                event_type="test_completed",
                message="Test workflow completed; zero Instagram engagements delivered",
            ))
            db.commit()
        finally:
            db.close()


test_worker = ServicesTestWorker()
