"""Regression tests for the lost-update race on state.json `jobs`.

Many concurrent processes (telegram poller, watchdog, scheduled PM cycles, the
job executor, scanners, core-discovery) all run `load_state -> mutate -> save_state`
on the same state.json with no lock. A writer holding a stale snapshot used to
overwrite jobs another process had appended in the interim — the "research jobs
vanish from the queue" bug. save_state now merges the jobs list with the on-disk
version under a cross-process lock, so no appended job or status advance is lost.
"""

from __future__ import annotations

import threading

from tradingagents.portfolio_advisor import state


def _cfg(tmp_path):
    return {"portfolio_advisor_dir": str(tmp_path)}


def test_stale_save_does_not_clobber_concurrently_appended_job(tmp_path):
    """A queues job Y; B (which loaded before Y existed) then saves job Z.
    Without the merge, B's save would drop Y. All three must survive."""
    cfg = _cfg(tmp_path)
    seed = state.load_state(cfg)
    seed["jobs"].append({"id": "X", "status": "pending", "ticker": "AAA"})
    state.save_state(cfg, seed)

    a = state.load_state(cfg)  # both load the same snapshot (sees only X)
    b = state.load_state(cfg)

    a["jobs"].append({"id": "Y", "status": "pending", "ticker": "BBB"})
    state.save_state(cfg, a)

    # B never saw Y — under the old overwrite behaviour this save erased it.
    b["jobs"].append({"id": "Z", "status": "pending", "ticker": "CCC"})
    state.save_state(cfg, b)

    ids = {j["id"] for j in state.load_state(cfg)["jobs"]}
    assert ids == {"X", "Y", "Z"}, ids


def test_stale_save_does_not_revert_a_completed_job(tmp_path):
    """The executor marks X done; a stale writer that still sees X pending then
    saves. X must stay done (status only moves forward), and the stale writer's
    own new job must still land."""
    cfg = _cfg(tmp_path)
    seed = state.load_state(cfg)
    seed["jobs"].append({"id": "X", "status": "pending", "ticker": "AAA"})
    state.save_state(cfg, seed)

    stale = state.load_state(cfg)  # sees X pending

    done = state.load_state(cfg)
    for j in done["jobs"]:
        if j["id"] == "X":
            j["status"] = "done"
    state.save_state(cfg, done)

    stale["jobs"].append({"id": "Y", "status": "pending"})
    state.save_state(cfg, stale)

    by_id = {j["id"]: j["status"] for j in state.load_state(cfg)["jobs"]}
    assert by_id["X"] == "done"      # not reverted to pending
    assert by_id["Y"] == "pending"


def test_threaded_appends_lose_nothing(tmp_path):
    """20 threads each load -> append a distinct job -> save concurrently.
    Every job id must be present afterwards."""
    cfg = _cfg(tmp_path)
    state.save_state(cfg, state.default_state())

    n = 20

    def worker(i: int) -> None:
        st = state.load_state(cfg)
        st["jobs"].append({"id": f"job{i}", "status": "pending"})
        state.save_state(cfg, st)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ids = {j["id"] for j in state.load_state(cfg)["jobs"]}
    expected = {f"job{i}" for i in range(n)}
    assert ids == expected, f"lost jobs: {expected - ids}"


def test_force_reset_wipes_jobs(tmp_path):
    """A deliberate reset (merge_jobs=False) must clear jobs, not preserve them."""
    cfg = _cfg(tmp_path)
    seed = state.load_state(cfg)
    seed["jobs"].append({"id": "X", "status": "pending"})
    state.save_state(cfg, seed)

    state.save_state(cfg, state.default_state(), merge_jobs=False)
    assert state.load_state(cfg)["jobs"] == []


def test_merge_jobs_unit():
    """_merge_jobs unions by id and keeps the most-advanced status."""
    disk = [{"id": "a", "status": "pending"}, {"id": "b", "status": "done"}]
    mem = [{"id": "b", "status": "pending"}, {"id": "c", "status": "pending"}]
    out = {j["id"]: j["status"] for j in state._merge_jobs(disk, mem)}
    assert out == {"a": "pending", "b": "done", "c": "pending"}  # b not reverted
