import asyncio
import threading

from nexus.core.events import EventBus


def test_coroutine_listener_receives_events_from_worker_thread():
    bus = EventBus()
    received = []

    async def main():
        loop = asyncio.get_running_loop()
        bus.bind_loop(loop)
        got = asyncio.Event()

        async def listener(data):
            received.append(data)
            got.set()

        bus.on("lead.transition", listener)
        worker = threading.Thread(target=lambda: bus.emit("lead.transition", {"lead_id": "abc"}))
        worker.start()
        worker.join()
        await asyncio.wait_for(got.wait(), timeout=2)

    asyncio.run(main())
    assert received == [{"lead_id": "abc"}]


def test_pattern_listeners_get_entry_with_seq():
    bus = EventBus()
    entries = []
    bus.on("lead.*", entries.append)
    bus.emit("lead.created", {"x": 1})
    bus.emit("control.kill_switch", {})
    assert [e["event"] for e in entries] == ["lead.created"]
    assert entries[0]["seq"] >= 1 and "ts" in entries[0]


def test_unbound_coroutine_is_dropped_without_crashing():
    bus = EventBus()

    async def listener(data):
        raise AssertionError("should not run")

    bus.on("x", listener)
    bus.emit("x", {})  # no loop bound and none running → warning, no exception
    assert bus.get_history("x")[-1]["event"] == "x"


def test_emit_is_thread_safe_under_load():
    bus = EventBus()
    count = []
    bus.on("tick", lambda d: count.append(d))
    threads = [threading.Thread(target=lambda: [bus.emit("tick", 1) for _ in range(200)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(count) == 1600
