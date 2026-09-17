#!/usr/bin/env python3
"""
Regression test for a double-scheduler-init bug found while investigating
slow cli_debrid startup: routes/program_operation_routes.py::_execute_start_program()
treated a freshly-constructed-but-never-started APScheduler as a "zombie"
needing replacement, since BackgroundScheduler.running is False both before
the very first start() call AND after a real crash - it can't tell those
two states apart on its own.

Sequence that reproduced it on every boot:
  1. main.py's __main__ constructs ProgramRunner() -> __init__ builds
     self.scheduler with the configured local timezone (e.g. America/New_York)
     and calls _setup_scheduler_listeners() once, which does NOT recreate it
     (scheduler is not None) - just attaches listeners and sets
     initial_listeners_setup_complete = True.
  2. _execute_start_program() runs later (auto-start). Its first two
     "needs_listener_setup" checks both pass (listeners are already set up,
     scheduler is not None), so it falls to the third check: "scheduler
     exists but isn't running". Since ProgramRunner.start() - the only place
     that actually calls scheduler.start() - hasn't run yet at this point,
     .running is False, this check fires, and the perfectly good scheduler
     from step 1 (with the correct timezone) gets discarded and rebuilt from
     scratch via a code path that reads a DIFFERENT setting and defaults to
     UTC - re-scheduling every task twice, and silently losing the
     configured local timezone.

Fix: track whether scheduler.start() has ever actually been called
(ProgramRunner._scheduler_started_once), and only treat "exists but not
running" as a zombie needing rebuild when that's True - i.e. it really was
running before. A never-started scheduler is left alone.

This test exercises the condition itself (extracted as a pure function
mirroring the real code), since exercising the full Flask route + singleton
ProgramRunner would require heavy app-context mocking for no extra
confidence in the actual boolean logic under test.
"""

import unittest


def scheduler_needs_zombie_rebuild(scheduler_exists: bool, scheduler_running: bool, scheduler_started_once: bool) -> bool:
    """Mirrors _execute_start_program's third needs_listener_setup branch."""
    return scheduler_exists and not scheduler_running and scheduler_started_once


class TestSchedulerZombieDetection(unittest.TestCase):
    def test_freshly_constructed_never_started_scheduler_is_not_a_zombie(self):
        # The exact state on every process boot: __init__ built a scheduler,
        # but ProgramRunner.start() (the only place that calls
        # scheduler.start()) hasn't run yet.
        self.assertFalse(scheduler_needs_zombie_rebuild(
            scheduler_exists=True, scheduler_running=False, scheduler_started_once=False))

    def test_scheduler_that_was_running_and_died_is_a_zombie(self):
        # It genuinely started successfully before, then stopped running
        # without stop_program() nulling it out - a real crash/zombie case.
        self.assertTrue(scheduler_needs_zombie_rebuild(
            scheduler_exists=True, scheduler_running=False, scheduler_started_once=True))

    def test_currently_running_scheduler_is_never_a_zombie(self):
        self.assertFalse(scheduler_needs_zombie_rebuild(
            scheduler_exists=True, scheduler_running=True, scheduler_started_once=True))
        self.assertFalse(scheduler_needs_zombie_rebuild(
            scheduler_exists=True, scheduler_running=True, scheduler_started_once=False))

    def test_missing_scheduler_is_handled_by_a_different_branch(self):
        # scheduler_exists=False means the None-check branch (a different
        # elif in the real code) already caught it - this branch should
        # never fire for that case regardless of the other flags.
        self.assertFalse(scheduler_needs_zombie_rebuild(
            scheduler_exists=False, scheduler_running=False, scheduler_started_once=True))


if __name__ == '__main__':
    unittest.main()
