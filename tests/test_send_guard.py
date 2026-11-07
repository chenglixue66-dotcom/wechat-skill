"""All fixtures are in memory and temporary directories; no real UI is loaded."""
import ast
from dataclasses import replace
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import send_guard as sg


class Clock:
    def __init__(self):
        self.now = 100.0
    def __call__(self):
        return self.now


class HookBackend(sg.MockBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = 0
        self.on_read = None
        self.before_set = None
        self.after_set = None
        self.before_press = None
        self.after_press = None
        self.global_returns = 0

    def read_observation(self):
        self.reads += 1
        if self.on_read:
            self.on_read(self, self.reads)
        return super().read_observation()

    def set_draft(self, token, text):
        if self.before_set:
            self.before_set(self, token, text)
        super().set_draft(token, text)
        if self.after_set:
            self.after_set(self, token, text)

    def press_send(self, token):
        if self.before_press:
            self.before_press(self, token)
        result = super().press_send(token)
        if self.after_press:
            self.after_press(self, token)
        return result

    def global_return(self):
        self.global_returns += 1
        raise AssertionError("Global Return is forbidden, including preparation.")


class QtBackend(HookBackend):
    """Known empty action list when empty, AXPress only after nonempty input."""
    def __init__(self, *args, payload_press_supported=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.payload_press_supported = payload_press_supported
        self.observation = replace(self.observation, draft="", send_enabled=False, press_supported=False)
    def set_draft(self, token, text):
        super().set_draft(token, text)
        self.observation = replace(self.observation, press_supported=bool(text) and self.payload_press_supported)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="send-guard-test-")
        self.directory = Path(self.temporary.name).resolve()
        self.path = self.directory / "operations.json"
        self.clock = Clock()
        self.backend = HookBackend(clock=self.clock)
        self.guard = sg.SendGuard(self.backend, self.path, clock=self.clock)
        self.request = sg.RecipientRequest("operation_1", "mock_account", "mock_person", "person", "模拟消息❤️")

    def tearDown(self):
        self.temporary.cleanup()

    def assert_blocked(self, call, code=None):
        with self.assertRaises(sg.GuardError) as caught:
            call()
        if code:
            self.assertEqual(caught.exception.code, code)

    def fresh(self, name, observation):
        backend = HookBackend(observation, clock=self.clock)
        guard = sg.SendGuard(backend, self.directory / name / "operations.json", clock=self.clock)
        return backend, guard

    def test_prepare_has_no_send_or_global_return(self):
        prepared = self.guard.prepare(self.request)
        self.assertEqual(self.backend.observation.draft, self.request.payload)
        self.assertEqual(self.backend.presses, 0)
        self.assertEqual(self.backend.global_returns, 0)
        self.assertEqual(self.guard.operation_state(self.request.operation_id), "PREPARED")
        self.assertNotIn(self.request.payload, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(prepared.payload_hash, sg.text_hash(self.request.payload))
        self.assertFalse(hasattr(prepared, "payload"))

    def test_accepted_action_is_not_delivery(self):
        prepared = self.guard.prepare(self.request)
        def inspect_intent(backend, token):
            document = json.loads(self.path.read_text())
            self.assertEqual(document["operations"][self.request.operation_id]["state"], "COMMIT_STARTED")
            self.assertEqual(token.binding, prepared.binding)
        self.backend.before_press = inspect_intent
        result = self.guard.commit(prepared)
        self.assertEqual(result.state, "ATTEMPTED")
        self.assertIs(result.delivered, False)
        self.assertEqual(self.backend.presses, 1)
        self.assert_blocked(lambda: self.guard.commit(prepared))
        self.assertEqual(self.backend.presses, 1)
        self.assert_blocked(lambda: self.guard.cleanup(prepared))

    def test_unknowns_and_existing_drafts_are_zero_write(self):
        original = sg.mock_observation()
        changes = [dict(draft=None), dict(draft="user draft"), dict(read_errors=None),
                   dict(read_errors=("cannot_read",)), dict(binding=None)]
        for field in ("identity_verified", "on_screen", "focused", "composer_focused",
                      "window_minimized", "modal", "composer_editable", "send_enabled", "press_supported"):
            changes.append({field: None})
        for field in ("account_matches", "recipient_matches", "composer_matches", "send_matches"):
            changes.extend(({field: None}, {field: 2}, {field: True}))
        changes.extend((dict(on_screen=False), dict(focused=False), dict(composer_focused=False),
                        dict(window_minimized=True), dict(modal=True), dict(locked=True)))
        for number, change in enumerate(changes):
            with self.subTest(change=change):
                backend, guard = self.fresh(str(number), replace(original, **change))
                self.assert_blocked(lambda: guard.prepare(self.request))
                self.assertNotIn("set_draft", backend.events)
                self.assertEqual(backend.presses, 0)

    def test_unknown_lock_requires_all_foreground_controls_and_keeps_unknown(self):
        original = replace(sg.mock_observation(), locked=None)
        backend, guard = self.fresh("unknown_lock_foreground", original)
        prepared = guard.prepare(self.request)
        self.assertIsNone(backend.observation.locked)
        self.assertEqual(guard.commit(prepared).state, "ATTEMPTED")
        self.assertEqual(backend.presses, 1)
        for number, change in enumerate((dict(focused=False), dict(composer_focused=False),
                                        dict(on_screen=False), dict(modal=True), dict(locked=True))):
            backend, guard = self.fresh("lock_focus_" + str(number), replace(original, **change))
            self.assert_blocked(lambda: guard.prepare(self.request))
            self.assertNotIn("set_draft", backend.events)
            self.assertEqual(backend.presses, 0)

    def test_lock_boolean_coercions_are_rejected(self):
        for number, value in enumerate((0, 1, "false", [])):
            backend, guard = self.fresh("invalid_lock_" + str(number),
                                        replace(sg.mock_observation(), locked=value))
            self.assert_blocked(lambda: guard.prepare(self.request), "LOCK_STATE_INVALID")
            self.assertEqual(backend.presses, 0)

    def test_same_display_name_does_not_override_id_or_type(self):
        # Display names are intentionally absent from policy inputs.
        for number, change in enumerate((dict(recipient_id="mock_other"),
                                         dict(recipient_id="mock_group", recipient_type="group"),
                                         dict(account_id="mock_other_account"))):
            observation = replace(sg.mock_observation(), binding=replace(sg.mock_observation().binding, **change))
            backend, guard = self.fresh(str(number), observation)
            self.assert_blocked(lambda: guard.prepare(self.request), "RECIPIENT_MISMATCH")
            self.assertNotIn("set_draft", backend.events)

    def test_read_exception_is_not_an_empty_draft(self):
        def fail(*_):
            raise TimeoutError("do not log this backend payload")
        self.backend.on_read = fail
        self.assert_blocked(lambda: self.guard.prepare(self.request), "OBSERVATION_FAILED")
        self.assertNotIn("set_draft", self.backend.events)
        self.assertEqual(self.backend.presses, 0)

    def test_partial_write_or_emoji_drift_is_not_cleared(self):
        for number, altered in enumerate(("partial", "模拟消息❤", "user changed this")):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            backend.after_set = lambda b, _t, text, value=altered: setattr(b, "observation", replace(b.observation, draft=value))
            self.assert_blocked(lambda: guard.prepare(self.request), "DRAFT_CHANGED")
            self.assertEqual(backend.observation.draft, altered)
            self.assertEqual(backend.events.count("set_draft"), 1)
            self.assertEqual(backend.presses, 0)

    def test_prepare_binding_drift_at_both_write_boundaries(self):
        def switch(backend, *_):
            backend.observation = replace(backend.observation, binding=replace(backend.observation.binding, recipient_id="other"))
        for number, stage in enumerate(("before_set", "after_set")):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            setattr(backend, stage, switch)
            self.assert_blocked(lambda: guard.prepare(self.request))
            self.assertEqual(backend.presses, 0)
            self.assertLessEqual(backend.events.count("set_draft"), 1)

    def test_prepare_write_timeout_does_not_guess_draft_ownership(self):
        def timeout(backend, _token, _text):
            backend.after_set = None
            raise TimeoutError("not persisted")
        self.backend.after_set = timeout
        self.assert_blocked(lambda: self.guard.prepare(self.request), "DRAFT_WRITE_UNCERTAIN")
        self.assertEqual(self.backend.observation.draft, self.request.payload)
        self.assertEqual(self.backend.presses, 0)
        self.assertEqual(self.guard.operation_state(self.request.operation_id), "PREPARE_FAILED")

    def test_all_unsafe_states_at_final_effect_boundaries(self):
        changes = [dict(read_errors=("failed",)), dict(read_errors=None), dict(draft=None),
                   dict(identity_verified=False), dict(on_screen=False), dict(focused=False),
                   dict(composer_focused=False), dict(window_minimized=True), dict(modal=True),
                   dict(locked=True), dict(composer_editable=False), dict(press_supported=None),
                   dict(send_enabled=None)]
        for field in ("account_matches", "recipient_matches", "composer_matches", "send_matches"):
            changes.extend(({field: 2}, {field: None}))
        for stage in ("before_set", "before_press"):
            stage_changes = changes + ([dict(press_supported=False)] if stage == "before_press" else [])
            for number, change in enumerate(stage_changes):
                with self.subTest(stage=stage, change=change):
                    backend, guard = self.fresh(stage + str(number), sg.mock_observation())
                    if stage == "before_press":
                        prepared = guard.prepare(self.request)
                    def mutate(b, *_args, update=change):
                        b.observation = replace(b.observation, **update)
                    setattr(backend, stage, mutate)
                    if stage == "before_set":
                        self.assert_blocked(lambda: guard.prepare(self.request))
                        self.assertNotIn("set_draft", backend.events)
                    else:
                        self.assertEqual(guard.commit(prepared).state, "UNKNOWN")
                    self.assertEqual(backend.presses, 0)

    def test_known_false_action_is_edit_safe_but_never_send_ready(self):
        empty = replace(sg.mock_observation(), press_supported=False)
        sg.validate_observation(empty, draft_hash=sg.text_hash(""), ready=False)
        self.assert_blocked(lambda: sg.validate_observation(empty, ready=True), "SEND_CONTROL_UNAVAILABLE")
        nonempty = replace(empty, draft="synthetic", send_enabled=True)
        self.assert_blocked(lambda: sg.validate_observation(nonempty, ready=True), "SEND_ACTION_UNAVAILABLE")
        for unknown in (None, 0, "false"):
            self.assert_blocked(lambda: sg.validate_observation(replace(empty,press_supported=unknown)), "SEND_ACTION_UNKNOWN")

    def test_qt_empty_false_payload_true_cleanup_false(self):
        backend = QtBackend(clock=self.clock)
        guard = sg.SendGuard(backend, self.path, clock=self.clock)
        self.assertIs(backend.observation.press_supported, False)
        prepared = guard.prepare(self.request)
        self.assertIs(backend.observation.send_enabled, True)
        self.assertIs(backend.observation.press_supported, True)
        self.assertTrue(guard.cleanup(prepared))
        self.assertEqual(backend.observation.draft, "")
        self.assertIs(backend.observation.send_enabled, False)
        self.assertIs(backend.observation.press_supported, False)
        self.assertEqual(backend.presses, 0)
        self.assertEqual(guard.operation_state(self.request.operation_id), "CLEANED")

    def test_payload_without_press_action_fails_preparation_and_cleans_owned_draft(self):
        backend = QtBackend(clock=self.clock,payload_press_supported=False)
        guard = sg.SendGuard(backend,self.path,clock=self.clock)
        self.assert_blocked(lambda:guard.prepare(self.request),"SEND_ACTION_UNAVAILABLE")
        self.assertEqual(backend.observation.draft, "")
        self.assertEqual(backend.events.count("set_draft"), 2)
        self.assertEqual(backend.presses, 0)
        record = json.loads(self.path.read_text())["operations"][self.request.operation_id]
        self.assertEqual(record["state"], "PREPARE_FAILED")
        self.assertIs(record["draft_cleaned"], True)

    def test_qt_action_disappearing_after_prepare_blocks_commit(self):
        backend = QtBackend(clock=self.clock)
        guard = sg.SendGuard(backend,self.path,clock=self.clock)
        prepared = guard.prepare(self.request)
        backend.observation = replace(backend.observation,press_supported=False)
        self.assert_blocked(lambda:guard.commit(prepared),"SEND_ACTION_UNAVAILABLE")
        self.assertEqual(backend.presses,0)
        self.assertTrue(guard.cleanup(prepared))

    def test_failed_prepare_cleanup_distinguishes_known_false_unknown_and_user_text(self):
        cases = (("disabled",dict(send_enabled=False),True),
                 ("unknown_action",dict(press_supported=None),False),
                 ("unknown_enabled",dict(send_enabled=None),False),
                 ("user_text",dict(press_supported=False,draft="user modified"),False))
        for name, change, cleaned in cases:
            backend, sender = self.fresh(name,sg.mock_observation())
            backend.after_set = lambda b,_token,_text,update=change: setattr(b,"observation",replace(b.observation,**update))
            self.assert_blocked(lambda:sender.prepare(self.request))
            self.assertEqual(backend.presses,0)
            self.assertEqual(backend.events.count("set_draft"),2 if cleaned else 1)
            record = json.loads((self.directory/name/"operations.json").read_text())["operations"][self.request.operation_id]
            self.assertIs(record["draft_cleaned"],cleaned)
            if name == "user_text":self.assertEqual(backend.observation.draft,"user modified")

    def test_known_false_action_never_bypasses_other_unknowns(self):
        for number, change in enumerate((dict(identity_verified=False),dict(binding=None),
                                        dict(focused=False),dict(locked=True),dict(press_supported=None))):
            backend = QtBackend(clock=self.clock)
            backend.observation = replace(backend.observation,**change)
            guard = sg.SendGuard(backend,self.directory/str(number)/"ops.json",clock=self.clock)
            self.assert_blocked(lambda:guard.prepare(self.request))
            self.assertNotIn("set_draft",backend.events)
            self.assertEqual(backend.presses,0)

    def test_recovered_known_false_does_not_reopen_uncertain_operations(self):
        def fail_after_write(backend,_token,_text):
            backend.after_set = None
            raise TimeoutError("synthetic uncertain write")
        self.backend.after_set = fail_after_write
        self.assert_blocked(lambda:self.guard.prepare(self.request),"DRAFT_WRITE_UNCERTAIN")
        self.backend.observation = replace(self.backend.observation,press_supported=False,send_enabled=False)
        writes = self.backend.events.count("set_draft")
        self.assert_blocked(lambda:self.guard.prepare(self.request),"OPERATION_ALREADY_EXISTS")
        self.assertEqual(self.backend.events.count("set_draft"),writes)
        self.assertEqual(self.backend.observation.draft,self.request.payload)
        backend = QtBackend(clock=self.clock)
        sender = sg.SendGuard(backend,self.directory/"uncertain-submit"/"operations.json",clock=self.clock)
        request = replace(self.request,operation_id="uncertain-submit")
        prepared = sender.prepare(request)
        backend.after_press = lambda *_: (_ for _ in ()).throw(TimeoutError("synthetic uncertain submit"))
        self.assertEqual(sender.commit(prepared).state,"UNKNOWN")
        backend.observation = replace(backend.observation,draft=request.payload,press_supported=False,send_enabled=False)
        writes = backend.events.count("set_draft")
        self.assert_blocked(lambda:sender.cleanup(prepared),"CLEANUP_AFTER_SUBMISSION_FORBIDDEN")
        self.assert_blocked(lambda:sender.commit(prepared),"OPERATION_NOT_COMMITTABLE")
        self.assert_blocked(lambda:sender.prepare(request),"OPERATION_ALREADY_EXISTS")
        self.assertEqual(backend.events.count("set_draft"),writes)
        self.assertEqual(backend.presses,1)

    def test_every_binding_component_is_rechecked_at_commit(self):
        changes = dict(account_id="other", recipient_id="other", recipient_type="group", pid=101,
                       launch_token="restarted", bundle_path="/tmp/Fake.app", window_id="other",
                       composer_token="other", send_token="other")
        for number, (field, value) in enumerate(changes.items()):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            prepared = guard.prepare(self.request)
            backend.observation = replace(backend.observation, binding=replace(prepared.binding, **{field: value}))
            self.assert_blocked(lambda: guard.commit(prepared))
            self.assertEqual(backend.presses, 0)

    def test_commit_focus_modal_draft_unknown_and_action_drift(self):
        for number, change in enumerate((dict(focused=False), dict(composer_focused=False), dict(modal=True),
                                         dict(locked=True), dict(on_screen=False), dict(window_minimized=True),
                                         dict(draft=None), dict(draft="edited"), dict(read_errors=("error",)),
                                         dict(send_enabled=False), dict(press_supported=False))):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            prepared = guard.prepare(self.request)
            backend.observation = replace(backend.observation, **change)
            self.assert_blocked(lambda: guard.commit(prepared))
            self.assertEqual(backend.presses, 0)

    def test_expiration_and_forged_prepared_are_blocked(self):
        prepared = self.guard.prepare(self.request)
        self.assert_blocked(lambda: self.guard.commit(replace(prepared)), "PREPARED_NOT_ISSUED_HERE")
        self.clock.now += 6
        self.assert_blocked(lambda: self.guard.commit(prepared), "PREPARED_EXPIRED")
        self.assertEqual(self.backend.presses, 0)
        self.assertTrue(self.guard.cleanup(prepared))

    def test_drift_on_commit_reads_before_and_after_durable_intent(self):
        for number, read_number in enumerate((3, 4)):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            prepared = guard.prepare(self.request)
            def mutate(b, count, trigger=read_number):
                if count == trigger:
                    b.observation = replace(b.observation, binding=replace(b.observation.binding, window_id="other"))
            backend.on_read = mutate
            self.assert_blocked(lambda: guard.commit(prepared))
            self.assertEqual(backend.presses, 0)
            self.assertEqual(guard.operation_state(self.request.operation_id), "ABORTED" if read_number == 3 else "ABORTED_PRECOMMIT")

    def test_backend_bound_token_checks_final_action_boundary(self):
        prepared = self.guard.prepare(self.request)
        def switch(backend, _token):
            backend.observation = replace(backend.observation, binding=replace(prepared.binding, recipient_id="other"))
        self.backend.before_press = switch
        result = self.guard.commit(prepared)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertEqual(self.backend.presses, 0)

    def test_deadline_is_rechecked_at_final_backend_boundary(self):
        prepared = self.guard.prepare(self.request)
        self.backend.before_press = lambda *_: setattr(self.clock, "now", self.clock.now + 6)
        self.assertEqual(self.guard.commit(prepared).state, "UNKNOWN")
        self.assertEqual(self.backend.presses, 0)
        self.assert_blocked(lambda: self.guard.commit(prepared))

    def test_commit_observation_timeouts_never_press(self):
        for number, trigger in enumerate((3, 4)):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            prepared = guard.prepare(self.request)
            def timeout(_backend, count, failing=trigger):
                if count == failing:
                    raise TimeoutError("not logged")
            backend.on_read = timeout
            self.assert_blocked(lambda: guard.commit(prepared), "OBSERVATION_FAILED")
            self.assertEqual(backend.presses, 0)

    def test_changed_journal_identity_or_hash_blocks_commit(self):
        prepared = self.guard.prepare(self.request)
        document = json.loads(self.path.read_text())
        document["operations"][self.request.operation_id]["payload_hash"] = sg.text_hash("different")
        self.path.write_text(json.dumps(document))
        self.assert_blocked(lambda: self.guard.commit(prepared), "OPERATION_NOT_COMMITTABLE")
        self.assertEqual(self.backend.presses, 0)

    def test_timeout_after_effect_is_unknown_and_not_retried(self):
        prepared = self.guard.prepare(self.request)
        self.backend.after_press = lambda *_: (_ for _ in ()).throw(TimeoutError("private backend details"))
        result = self.guard.commit(prepared)
        self.assertEqual(result.state, "UNKNOWN")
        self.assertEqual(self.backend.presses, 1)
        self.assert_blocked(lambda: self.guard.commit(prepared))
        restarted = sg.SendGuard(HookBackend(clock=self.clock), self.path, clock=self.clock)
        self.assertEqual(restarted.operation_state(self.request.operation_id), "UNKNOWN")
        self.assert_blocked(lambda: restarted.prepare(self.request), "OPERATION_ALREADY_EXISTS")
        self.assertNotIn("private backend details", self.path.read_text())

    def test_journal_failure_before_and_after_press(self):
        for number, phase in enumerate(("COMMIT_STARTED", "ATTEMPTED")):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            prepared = guard.prepare(self.request)
            write = guard.journal.write
            def failing(document, blocked=phase):
                if document["operations"][self.request.operation_id]["state"] == blocked:
                    raise sg.GuardError("JOURNAL_IO_FAILED")
                return write(document)
            guard.journal.write = failing
            if phase == "COMMIT_STARTED":
                self.assert_blocked(lambda: guard.commit(prepared), "JOURNAL_IO_FAILED")
                self.assertEqual(backend.presses, 0)
            else:
                self.assertEqual(guard.commit(prepared).state, "UNKNOWN")
                self.assertEqual(backend.presses, 1)
                self.assertEqual(guard.operation_state(self.request.operation_id), "UNKNOWN")
            self.assert_blocked(lambda: guard.commit(prepared))

    def test_duplicate_operation_cannot_change_payload_or_recipient(self):
        self.guard.prepare(self.request)
        writes = self.backend.events.count("set_draft")
        for request in (self.request, replace(self.request, payload="another"), replace(self.request, recipient_id="other")):
            self.assert_blocked(lambda: self.guard.prepare(request), "OPERATION_ALREADY_EXISTS")
        self.assertEqual(self.backend.events.count("set_draft"), writes)

    def test_cleanup_preserves_user_changes_and_control_changes(self):
        for number, change in enumerate(("draft", "binding", "read_error")):
            backend, guard = self.fresh(str(number), sg.mock_observation())
            prepared = guard.prepare(self.request)
            if change == "draft":
                backend.observation = replace(backend.observation, draft="user draft")
            elif change == "binding":
                backend.observation = replace(backend.observation, binding=replace(prepared.binding, composer_token="other"))
            else:
                backend.observation = replace(backend.observation, read_errors=("failed",))
            writes = backend.events.count("set_draft")
            self.assertFalse(guard.cleanup(prepared))
            self.assertEqual(backend.events.count("set_draft"), writes)
            self.assertEqual(backend.presses, 0)

    def test_thread_concurrency_has_at_most_one_effect(self):
        prepared = self.guard.prepare(self.request)
        results = []
        def commit():
            try:
                results.append(self.guard.commit(prepared).state)
            except sg.GuardError:
                results.append("blocked")
        threads = [threading.Thread(target=commit) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(self.backend.presses, 1)
        self.assertCountEqual(results, ["ATTEMPTED", "blocked"])

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX process-lock regression")
    def test_process_concurrency_and_real_process_death_are_not_retried(self):
        for scenario in ("race", "crash", "crash_before_press"):
            directory = self.directory / scenario
            directory.mkdir(mode=0o700)
            counter = multiprocessing.get_context("fork").Value("i", 0)
            class SharedBackend(HookBackend):
                def press_send(inner, token):
                    result = super().press_send(token)
                    with counter.get_lock():counter.value += 1
                    if scenario == "crash":os._exit(17)
                    return result
            backend = SharedBackend(clock=self.clock)
            guard = sg.SendGuard(backend, directory / "operations.json", clock=self.clock)
            prepared = guard.prepare(self.request)
            if scenario == "crash_before_press":
                write = guard.journal.write
                def crash_after_durable_intent(document):
                    write(document)
                    if document["operations"][self.request.operation_id]["state"] == "COMMIT_STARTED":
                        os._exit(17)
                guard.journal.write = crash_after_durable_intent
            children = []
            for _ in range(2 if scenario == "race" else 1):
                pid = os.fork()
                if pid == 0:
                    try:guard.commit(prepared)
                    except sg.GuardError:pass
                    os._exit(0)
                children.append(pid)
            for pid in children:os.waitpid(pid, 0)
            self.assertEqual(counter.value, 0 if scenario == "crash_before_press" else 1)
            self.assert_blocked(lambda: guard.commit(prepared))
            if scenario in {"crash", "crash_before_press"}:
                self.assertEqual(guard.operation_state(self.request.operation_id), "UNKNOWN")

    def test_corrupt_or_unsafe_journal_is_not_treated_as_empty(self):
        for number, scenario in enumerate(("invalid_json", "invalid_schema", "permissions", "symlink", "parent_permissions")):
            directory = self.directory / str(number)
            directory.mkdir(mode=0o700)
            path = directory / "operations.json"
            if scenario == "symlink":
                target = directory / "target"
                target.write_text('{}');target.chmod(0o600);path.symlink_to(target)
            elif scenario == "parent_permissions":directory.chmod(0o755)
            else:
                path.write_text('bad' if scenario == "invalid_json" else '{"version":1,"operations":{"old":{}}}')
                path.chmod(0o644 if scenario == "permissions" else 0o600)
            backend = HookBackend(clock=self.clock)
            guard = sg.SendGuard(backend, path, clock=self.clock)
            self.assert_blocked(lambda: guard.prepare(self.request))
            self.assertNotIn("set_draft", backend.events)
            self.assertEqual(backend.presses, 0)

    def test_only_mock_cli_modes_exist_and_no_ui_libraries_are_imported(self):
        source = SCRIPTS / "send_guard.py"
        tree = ast.parse(source.read_text())
        imports = {node.names[0].name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)}
        imports |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse(imports & {"AppKit", "Quartz", "ApplicationServices", "pyautogui", "subprocess", "ctypes"})
        for mode, count in (("self-test", 0), ("mock", 1)):
            result = subprocess.run([sys.executable, "-B", str(source), mode], capture_output=True, text=True, check=True)
            report = json.loads(result.stdout)
            self.assertTrue(report["mock_only"])
            self.assertEqual(report["mock_presses"], count)
            self.assertFalse(report["delivered"])
        for extra in (("--fixture", "real"), ("--force",), ("--backend", "real")):
            result = subprocess.run([sys.executable, "-B", str(source), "mock", *extra], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
