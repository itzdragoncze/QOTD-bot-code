import asyncio
import aiosqlite
import pytest


@pytest.mark.asyncio
async def test_atomic_suggestion_claim(in_memory_db: aiosqlite.Connection):
    """Verifies that DELETE ... RETURNING * guarantees exactly one claimant succeeds."""
    # Insert a suggestion
    await in_memory_db.execute(
        """
        INSERT INTO suggestions (id, guild_id, target_channel_id, question, user_id, user_name, created_at)
        VALUES ('sugg-100', 1, 10, 'Test question?', 12345, 'Alice', '2026-09-06T12:00:00')
        """
    )
    await in_memory_db.commit()

    results = []

    async def try_claim(worker_name: str):
        cursor = await in_memory_db.execute(
            "DELETE FROM suggestions WHERE id = ? AND guild_id = ? RETURNING *",
            ("sugg-100", 1),
        )
        row = await cursor.fetchone()
        await in_memory_db.commit()
        if row:
            results.append((worker_name, dict(row)))
        else:
            results.append((worker_name, None))

    # Run two workers attempting to claim the suggestion concurrently
    await asyncio.gather(try_claim("Worker 1"), try_claim("Worker 2"))

    successful_claims = [r for r in results if r[1] is not None]
    failed_claims = [r for r in results if r[1] is None]

    assert len(successful_claims) == 1, "Exactly one worker must successfully claim the suggestion"
    assert len(failed_claims) == 1, "The second worker must receive None"
    assert successful_claims[0][1]["id"] == "sugg-100"


@pytest.mark.asyncio
async def test_channel_lock_mutual_exclusion():
    """Verifies that asyncio.Lock per channel ensures mutual exclusion."""
    channel_locks = {}

    def get_channel_lock(channel_id: int) -> asyncio.Lock:
        if channel_id not in channel_locks:
            channel_locks[channel_id] = asyncio.Lock()
        return channel_locks[channel_id]

    execution_order = []

    async def task_posting(worker_id: int, channel_id: int):
        async with get_channel_lock(channel_id):
            execution_order.append(f"start-{worker_id}")
            await asyncio.sleep(0.05)
            execution_order.append(f"end-{worker_id}")

    # Launch two tasks for the same channel concurrently
    await asyncio.gather(task_posting(1, 42), task_posting(2, 42))

    # Since tasks are locked, task 1 must finish before task 2 starts (or vice versa)
    first_worker = 1 if execution_order[0] == "start-1" else 2
    second_worker = 2 if first_worker == 1 else 1

    expected = [
        f"start-{first_worker}",
        f"end-{first_worker}",
        f"start-{second_worker}",
        f"end-{second_worker}",
    ]
    assert execution_order == expected
