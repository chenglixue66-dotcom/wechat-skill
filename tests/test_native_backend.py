"""Native adapter contracts using only in-memory transport and synthetic data."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import tempfile
import unittest

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import native_backend as native
import send_guard as guard
from identity_catalog import IdentityCatalog


def wire():
    return json.loads(json.dumps(asdict(guard.mock_observation())))


def profile_pair():
    def string(value):return {"status": 0, "type": "CFString", "value": value}
    children = [{"token": "child-" + str(i), "process_epoch": "fixture-epoch", "parent_root_token": "profile-root",
                 "parent_matches_root": True, "role": string("AXButton"), "identifier": string(""),
                 "value": {"status": -25205, "type": "unknown", "value": None}} for i in range(28)]
    for index, identifier in enumerate(("head_view_", "value_remark_view", "chat_img_button")):
        children[index]["identifier"] = string(identifier)
    children[5]["value"] = string("Weixin ID:")
    children[5]["role"] = string("AXStaticText")
    children[6]["value"] = string("fixture_alias_a")
    children[6]["role"] = string("AXStaticText")
    snapshot = {"process_epoch": "fixture-epoch", "root_token": "profile-root", "window_id": None,
                "window_association_verified": False, "composer_token": "fixture-composer",
                "send_token": "fixture-send", "children": children}
    return {"schema": "recipient-profile-v1-28", "first": snapshot, "second": json.loads(json.dumps(snapshot))}


class FreshFixture:
    def __init__(self, catalog):
        self.catalog = catalog
        self.source_token = "synthetic-source-token"
        self.closed = False
        self.checks = 0
    def assert_current(self):
        self.checks += 1
        if self.closed:raise RuntimeError("synthetic stale source")
        self.catalog.assert_current()
    def resolve(self, query, *, kind):
        self.assert_current()
        if kind != "weixin_id":raise AssertionError("unexpected query")
        return self.catalog.match_weixin_id(query)


class FixtureTransport:
    def __init__(self, document=None):
        self.document = wire() if document is None else document
        self.calls = []
        self.set_effects = 0
        self.closed = False
        self.before_set = None
        self.after_set = None
        self.failure = None
    def request(self, operation, arguments):
        self.calls.append(operation)
        if self.failure:
            raise self.failure
        if operation == "read_observation":
            return json.loads(json.dumps(self.document))
        if operation == "set_draft":
            if self.before_set:
                self.before_set(self)
            current = native.observation_from_wire(self.document)
            guard.validate_observation(current, binding=guard.Binding(**arguments["binding"]),
                                       draft_hash=arguments["expected_draft_hash"])
            self.document["draft"] = arguments["text"]
            self.document["send_enabled"] = bool(arguments["text"])
            self.set_effects += 1
            if self.after_set:
                self.after_set(self)
            return {"applied": True, "observation": json.loads(json.dumps(self.document))}
        raise AssertionError("No press or other operation may reach the transport")
    def close(self):
        self.closed = True


class QtFixtureTransport(FixtureTransport):
    def __init__(self, *, payload_press_supported=True):
        super().__init__()
        self.document.update(press_supported=False,send_enabled=False)
        self.payload_press_supported = payload_press_supported
    def request(self, operation, arguments):
        result = super().request(operation,arguments)
        if operation == "set_draft":
            self.document["press_supported"] = bool(arguments["text"]) and self.payload_press_supported
            result["observation"] = json.loads(json.dumps(self.document))
        return result


class PipeProcess:
    """A thread-backed pair of local pipes, never an application process."""
    def __init__(self, mode):
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = os.fdopen(output_read, "rb", buffering=0)
        os.set_blocking(input_write, False)
        os.set_blocking(output_read, False)
        self.cancel = threading.Event()
        self.mode = mode
        self.requests = []
        def work():
            try:
                with os.fdopen(input_read, "rb", buffering=0) as reader:
                    while True:
                        line = reader.readline()
                        if not line:
                            break
                        request = json.loads(line)
                        self.requests.append(request)
                        if mode == "timeout":
                            self.cancel.wait(2)
                            break
                        if mode == "invalid_json":
                            response = b"not-json\n"
                        else:
                            response = json.dumps({"id": "wrong-id" if mode == "wrong_id" else request["id"],
                                                   "ok": True, "result": wire()}).encode() + b"\n"
                        os.write(output_write, response[:7])
                        os.write(output_write, response[7:])
            except (OSError, ValueError):
                pass
            finally:
                os.close(output_write)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
    def poll(self):
        return None if self.worker.is_alive() else 0
    def kill(self):
        self.cancel.set()
    def wait(self, timeout=None):
        self.worker.join(timeout)
        if self.worker.is_alive():
            raise subprocess.TimeoutExpired("synthetic-pipes", timeout)
        return 0


def pipe_transport(mode):
    # Test the production codec with local memory pipes, bypassing its fixed
    # app launcher rather than adding a production binary/fixture option.
    transport = native._ProcessTransport.__new__(native._ProcessTransport)
    transport.process = PipeProcess(mode)
    transport.timeout = 0.05 if mode == "timeout" else 1.0
    transport._mutex = threading.RLock()
    transport._buffer = b""
    transport._closed = False
    return transport


class NativeBackendTests(unittest.TestCase):
    def setUp(self):
        self.transport = FixtureTransport()
        self.backend = native.NativeBackend(transport=self.transport)
        self.binding = guard.mock_observation().binding
        self.token = guard.BoundComposer(self.binding, guard.text_hash(""))

    def blocked(self, operation, code=None):
        with self.assertRaises(guard.GuardError) as caught:
            operation()
        if code:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_typed_wire_mapping(self):
        result = self.backend.read_observation()
        self.assertEqual(result.binding, self.binding)
        self.assertEqual(result.draft, "")
        self.assertEqual(result.read_errors, ())
        self.assertIs(result.send_enabled, False)

    def test_unknown_read_is_never_empty(self):
        for change in ({"draft": None}, {"read_errors": None}, {"read_errors": ["AX_READ_FAILED"]}):
            document = wire();document.update(change)
            result = native.observation_from_wire(document)
            self.assertTrue(result.read_errors)
            if change.get("draft", "sentinel") is None:
                self.assertIsNone(result.draft)
            self.blocked(lambda: guard.validate_observation(result))

    def test_unknown_action_list_is_not_a_known_absent_press_action(self):
        unknown = wire()
        unknown.update(press_supported=None, read_errors=["SEND_ACTION_UNKNOWN"])
        result = native.observation_from_wire(unknown)
        self.assertIsNone(result.press_supported)
        self.assertIn("SEND_ACTION_UNKNOWN", result.read_errors)
        self.blocked(lambda: guard.validate_observation(result))
        known_empty = wire()
        known_empty.update(press_supported=False, read_errors=[])
        result = native.observation_from_wire(known_empty)
        self.assertIs(result.press_supported, False)
        self.assertEqual(result.read_errors, ())

    def test_unknown_identity_cannot_be_inferred_from_identifier_or_name(self):
        document = wire()
        document.update(identity_verified=False, AXIdentifier="wxid_untrusted", display_name="same name")
        result = native.observation_from_wire(document)
        self.assertIsNone(result.binding)
        self.assertIs(result.identity_verified, False)
        self.assertTrue(result.read_errors)

    def test_invalid_wire_types_remain_unknown(self):
        for change in ({"pid": True}, {"window_id": None}, {"composer_token": ""}):
            document = wire();document["binding"].update(change)
            result = native.observation_from_wire(document)
            self.assertIsNone(result.binding)
            self.assertTrue(result.read_errors)
        document = wire();document["focused"] = 1;document["recipient_matches"] = True
        result = native.observation_from_wire(document)
        self.assertIsNone(result.focused)
        self.assertIsNone(result.recipient_matches)
        self.blocked(lambda: guard.validate_observation(result))

    def test_draft_writes_are_disabled_by_default_without_any_ipc(self):
        self.blocked(lambda: self.backend.set_draft(self.token, "synthetic"), "DRAFT_WRITES_DISABLED")
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.transport.set_effects, 0)

    def test_send_is_hard_disabled_even_if_fixture_claims_support(self):
        self.transport.document["policy_live_send_enabled"] = True
        result = self.backend.press_send(guard.BoundSend(self.binding, guard.text_hash(""), "synthetic-op", float("inf")))
        self.assertIs(result, guard.PressOutcome.REJECTED)
        self.assertEqual(self.transport.calls, [])

    def test_explicit_fixture_draft_write_and_clear_stay_object_bound(self):
        backend = native.NativeBackend(transport=self.transport, allow_draft_writes=True)
        backend.set_draft(self.token, "synthetic❤️")
        self.assertEqual(self.transport.document["draft"], "synthetic❤️")
        backend.set_draft(guard.BoundComposer(self.binding, guard.text_hash("synthetic❤️")), "")
        self.assertEqual(self.transport.document["draft"], "")
        self.assertEqual(self.transport.set_effects, 2)
        self.assertNotIn("press_send", self.transport.calls)

    def test_native_qt_empty_payload_and_cleanup_capabilities(self):
        transport = QtFixtureTransport()
        backend = native.NativeBackend(transport=transport,allow_draft_writes=True)
        with tempfile.TemporaryDirectory(prefix="native-qt-synthetic-") as directory:
            sender = guard.SendGuard(backend,Path(directory).resolve()/"operations.json")
            request = guard.RecipientRequest("qt-operation","mock_account","mock_person","person","synthetic❤️")
            prepared = sender.prepare(request)
            self.assertIs(transport.document["press_supported"],True)
            self.assertTrue(sender.cleanup(prepared))
            self.assertEqual(transport.document["draft"],"")
            self.assertIs(transport.document["press_supported"],False)
            self.assertEqual(transport.set_effects,2)
            self.assertNotIn("press_send",transport.calls)

    def test_native_missing_payload_press_action_cleans_without_preparing(self):
        transport = QtFixtureTransport(payload_press_supported=False)
        backend = native.NativeBackend(transport=transport,allow_draft_writes=True)
        with tempfile.TemporaryDirectory(prefix="native-qt-synthetic-") as directory:
            sender = guard.SendGuard(backend,Path(directory).resolve()/"operations.json")
            request = guard.RecipientRequest("qt-unavailable","mock_account","mock_person","person","synthetic")
            self.blocked(lambda:sender.prepare(request),"SEND_ACTION_UNAVAILABLE")
            self.assertEqual(transport.document["draft"],"")
            self.assertEqual(transport.set_effects,2)
            self.assertNotIn("press_send",transport.calls)

    def test_native_unknown_action_list_cannot_edit_even_empty(self):
        transport = QtFixtureTransport()
        transport.document.update(press_supported=None,read_errors=["SEND_ACTION_UNKNOWN"])
        backend = native.NativeBackend(transport=transport,allow_draft_writes=True)
        self.blocked(lambda:backend.set_draft(self.token,"synthetic"))
        self.assertEqual(transport.set_effects,0)
        self.assertNotIn("set_draft",transport.calls)

    def test_restart_or_control_change_invalidates_old_token_before_write(self):
        for changes in ({"pid": 101, "launch_token": "new-launch"}, {"launch_token": "reused-pid-new-launch"},
                        {"window_id": "new-window"}, {"composer_token": "new-composer"}, {"send_token": "new-send"}):
            transport = FixtureTransport();transport.document["binding"].update(changes)
            backend = native.NativeBackend(transport=transport, allow_draft_writes=True)
            self.blocked(lambda: backend.set_draft(self.token, "synthetic"))
            self.assertNotIn("set_draft", transport.calls)
            self.assertEqual(transport.set_effects, 0)

    def test_unknown_account_and_ambiguous_recipient_do_not_write(self):
        for change in ({"identity_verified": False}, {"account_matches": None}, {"recipient_matches": 2}):
            transport = FixtureTransport();transport.document.update(change)
            backend = native.NativeBackend(transport=transport, allow_draft_writes=True)
            self.blocked(lambda: backend.set_draft(self.token, "synthetic"))
            self.assertNotIn("set_draft", transport.calls)

    def test_native_effect_boundary_rechecks_binding_and_draft(self):
        backend = native.NativeBackend(transport=self.transport, allow_draft_writes=True)
        self.transport.before_set = lambda t: t.document["binding"].update(recipient_id="other")
        self.blocked(lambda: backend.set_draft(self.token, "synthetic"), "DRAFT_WRITE_UNCERTAIN")
        self.assertEqual(self.transport.set_effects, 0)
        self.assertEqual(self.transport.calls.count("set_draft"), 1)

    def test_write_readback_mismatch_is_unknown_and_not_retried(self):
        backend = native.NativeBackend(transport=self.transport, allow_draft_writes=True)
        self.transport.after_set = lambda t: t.document.update(draft="user changed")
        self.blocked(lambda: backend.set_draft(self.token, "synthetic"), "DRAFT_WRITE_UNCERTAIN")
        self.assertEqual(self.transport.set_effects, 1)
        self.assertEqual(self.transport.calls.count("set_draft"), 1)
        self.assertEqual(self.transport.document["draft"], "user changed")

    def test_probe_outputs_hashes_and_flags_only(self):
        self.transport.document["draft"] = "synthetic confidential text"
        encoded = json.dumps(self.backend.probe_report())
        for raw in ("mock_account", "mock_person", "mock_window", "mock_launch", "synthetic confidential text"):
            self.assertNotIn(raw, encoded)
        self.assertIn(guard.text_hash("synthetic confidential text"), encoded)

    def test_transport_errors_do_not_echo_payload(self):
        self.transport.failure = RuntimeError("private synthetic text not for output")
        encoded = json.dumps(self.backend.probe_report())
        self.assertNotIn("private synthetic", encoded)
        self.assertFalse(json.loads(encoded)["ok"])

    def test_jsonl_codec_roundtrip_uses_exact_request_id_and_partial_reads(self):
        transport = pipe_transport("valid")
        try:
            document = transport.request("read_observation", {})
            self.assertEqual(document["binding"]["account_id"], "mock_account")
            self.assertEqual(len(transport.process.requests), 1)
        finally:
            transport.close()

    def test_jsonl_failure_closes_transport_and_does_not_retry(self):
        for mode, code in (("wrong_id", "NATIVE_BRIDGE_RESPONSE_MISMATCH"),
                           ("invalid_json", "NATIVE_BRIDGE_IPC_FAILED"),
                           ("timeout", "NATIVE_BRIDGE_TIMEOUT")):
            with self.subTest(mode=mode):
                transport = pipe_transport(mode)
                self.blocked(lambda: transport.request("read_observation", {}), code)
                self.assertTrue(transport._closed)
                self.blocked(lambda: transport.request("read_observation", {}), "NATIVE_BRIDGE_UNAVAILABLE")
                self.assertEqual(len(transport.process.requests), 1)

    def test_malformed_unicode_is_not_echoed(self):
        backend = native.NativeBackend(transport=self.transport, allow_draft_writes=True)
        error = self.blocked(lambda: backend.set_draft(self.token, "synthetic\ud800"), "INVALID_DRAFT_ARGUMENTS")
        self.assertEqual(str(error), "INVALID_DRAFT_ARGUMENTS")
        self.assertEqual(self.transport.calls, [])

    def test_cli_has_no_fixture_force_or_write_enable_route(self):
        script = SCRIPTS / "native_backend.py"
        result = subprocess.run([sys.executable, "-B", str(script), "self-test"],capture_output=True,text=True,check=True)
        self.assertTrue(json.loads(result.stdout)["synthetic_only"])
        for extra in (("--fixture", "real"), ("--force",), ("--allow-draft-writes",), ("--binary", "anything")):
            result = subprocess.run([sys.executable, "-B", str(script), "self-test", *extra],capture_output=True,text=True)
            self.assertEqual(result.returncode, 2)

    def test_compiled_bridge_default_capabilities_are_read_only(self):
        result = subprocess.run([str(SCRIPTS / "native_ax_bridge"), "--self-test"], capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertTrue(report["synthetic_only"])
        self.assertEqual(report["ui_writes"], 0)
        self.assertEqual(report["send_attempts"], 0)
        self.assertEqual(report["local_header_checks"], 7)

    def test_profile_raw_exact_field_is_partial_evidence_only(self):
        result = native.parse_recipient_profile_pair(profile_pair(), expected_process_epoch="fixture-epoch")
        self.assertEqual(result.raw_weixin_id, "fixture_alias_a")
        self.assertFalse(result.window_association_verified)
        self.assertFalse(result.ui_identity_confirmed)
        encoded = json.dumps(result.to_public_dict())
        self.assertNotIn("fixture_alias_a", encoded)
        self.assertNotIn("fixture_alias_a", repr(result))
        self.assertFalse(json.loads(encoded)["send_allowed"])

    def test_profile_ids_are_never_trimmed_or_normalized(self):
        for identifier in (" fixture_alias_a", "fixture_alias_a ", "fixture_alias_a\n", "fixture_alias_a\u200b",
                           "fixture_alias_a\ufe0f", "fixtur\u0435_alias_a", ""):
            with self.subTest(identifier_kind=len(identifier)):
                pair = profile_pair()
                for snapshot in (pair["first"], pair["second"]):
                    snapshot["children"][6]["value"]["value"] = identifier
                self.blocked(lambda: native.parse_recipient_profile_pair(pair, expected_process_epoch="fixture-epoch"), "PROFILE_RAW_ID_INVALID")

    def test_profile_markers_labels_and_adjacent_types_are_strict(self):
        changes = [lambda s: s["children"][0]["identifier"].update(value=""),
                   lambda s: s["children"][9]["identifier"].update(value="head_view_other"),
                   lambda s: s["children"][9]["value"].update(status=0,type="CFString",value="Weixin ID:"),
                   lambda s: s["children"][5]["value"].update(value="Weixin ID: "),
                   lambda s: s["children"][5]["value"].update(status=False),
                   lambda s: s["children"][5]["value"].update(type="CFAttributedString"),
                   lambda s: s["children"][6]["role"].update(value="AXButton"),
                   lambda s: s["children"][6]["value"].update(type="CFAttributedString"),
                   lambda s: s["children"][6]["value"].update(status=-25205),
                   lambda s: s["children"][6].update(parent_root_token="foreign-root"),
                   lambda s: s["children"][6].update(parent_matches_root=False),
                   lambda s: s["children"].pop(5)]
        for number, mutate in enumerate(changes):
            with self.subTest(case=number):
                pair = profile_pair()
                mutate(pair["first"]);mutate(pair["second"])
                self.blocked(lambda: native.parse_recipient_profile_pair(pair, expected_process_epoch="fixture-epoch"))

    def test_profile_semantic_schema_allows_reordered_localized_variable_rows(self):
        for label in ("Weixin ID:", "WeChat ID:", "微信号:", "微信号："):
            pair = profile_pair()
            pair["schema"] = "recipient-profile-v2"
            for snapshot in (pair["first"], pair["second"]):
                children = snapshot["children"]
                children[5]["value"]["value"] = label
                row = children[5:7]
                del children[5:7]
                children.extend(row)
                children.pop(10)
            profile = native.parse_recipient_profile_pair(pair, expected_process_epoch="fixture-epoch")
            self.assertEqual(profile.raw_weixin_id, "fixture_alias_a")
            self.assertFalse(profile.ui_identity_confirmed)

    def test_profile_semantic_scope_without_legacy_ids_is_unique(self):
        pair = profile_pair()
        pair["schema"] = "recipient-profile-v2"
        for snapshot in (pair["first"], pair["second"]):
            for child in snapshot["children"]:
                child["identifier"]["value"] = ""
            snapshot["children"][2]["public_action"] = "message"
        result = native.parse_recipient_profile_pair(pair, expected_process_epoch="fixture-epoch")
        self.assertEqual(result.raw_weixin_id, "fixture_alias_a")
        for snapshot in (pair["first"], pair["second"]):
            snapshot["children"][3]["public_action"] = "message"
        self.blocked(lambda: native.parse_recipient_profile_pair(pair, expected_process_epoch="fixture-epoch"), "PROFILE_MARKERS_MISSING_OR_AMBIGUOUS")

    def test_live_transport_uncertainty_consumes_the_attempt(self):
        class FailingSendTransport(FixtureTransport):
            def request(self, operation, arguments):
                if operation == "press_send":
                    self.calls.append(operation)
                    raise guard.GuardError("NATIVE_BRIDGE_TIMEOUT")
                return super().request(operation, arguments)
        import time
        transport = FailingSendTransport()
        transport.document.update(draft="synthetic", send_enabled=True, press_supported=True)
        backend = native.NativeBackend(transport=transport, allow_live_send=True)
        token = guard.BoundSend(self.binding, guard.text_hash("synthetic"), "uncertain-single-attempt", time.monotonic()+5)
        self.blocked(lambda: backend.press_send(token), "NATIVE_BRIDGE_TIMEOUT")
        self.blocked(lambda: backend.press_send(token), "SEND_ALREADY_ATTEMPTED")
        self.assertEqual(transport.calls.count("press_send"), 1)

    def test_optional_lock_unknown_remains_unknown_without_schema_failure(self):
        document = wire()
        document["locked"] = None
        result = native.observation_from_wire(document)
        self.assertIsNone(result.locked)
        self.assertEqual(result.read_errors, ())
        guard.validate_observation(result)
        document["locked"] = "unknown"
        self.assertIn("NATIVE_SCHEMA_UNKNOWN", native.observation_from_wire(document).read_errors)

    def test_open_chat_requires_fresh_catalog_and_independent_native_binding(self):
        class NavigationFresh(FreshFixture):
            account_id = "mock_account"
            def resolve(self, query, *, kind):
                self.assert_current()
                return {"username": self.catalog.lookup_username,
                        "weixin_id": self.catalog.match_weixin_id,
                        "account_display": self.catalog.match_account_display}[kind](query)
        class NavigationTransport(FixtureTransport):
            def request(self, operation, arguments):
                if operation == "open_chat":
                    self.calls.append(operation)
                    self.navigation_arguments = arguments
                    return json.loads(json.dumps(self.document))
                return super().request(operation, arguments)
        with tempfile.TemporaryDirectory(prefix="navigation-synthetic-") as directory:
            path = Path(directory)/"contacts.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE contact(username,remark,nick_name,alias)")
                connection.executemany("INSERT INTO contact VALUES(?,?,?,?)", [
                    ("mock_account", "", "Self Fixture", "self_alias"),
                    ("mock_person", "Target Fixture", "Target Nick", "target_alias")])
            fresh = NavigationFresh(IdentityCatalog.from_db(path))
            target = {"recipient_id":"mock_person", "account_id":"mock_account", "kind":"person",
                      "source_token":fresh.source_token, "search_terms":["Target Fixture"]}
            transport = NavigationTransport()
            backend = native.NativeBackend(transport=transport)
            result = backend.open_chat(target, fresh)
            self.assertEqual(result.binding, self.binding)
            self.assertIn("Self Fixture self_alias", transport.navigation_arguments["account_fields"])
            self.assertEqual(transport.navigation_arguments["search_terms"][0], "target_alias")
            # A native response missing independent GUI evidence never becomes a
            # Binding just because the controller supplied the desired recipient.
            transport.document.update(identity_verified=False, binding=None)
            self.blocked(lambda: backend.open_chat(target, fresh), "CHAT_BINDING_UNAVAILABLE")
            calls = len(transport.calls)
            fresh.closed = True
            self.blocked(lambda: backend.open_chat(target, fresh), "CONTACT_NAVIGATION_FAILED")
            self.assertEqual(len(transport.calls), calls)

    def test_live_opt_in_validates_exact_ready_state_before_single_press(self):
        class SendTransport(FixtureTransport):
            def request(self, operation, arguments):
                if operation == "press_send":
                    self.calls.append(operation)
                    return {"outcome": "ACCEPTED"}
                return super().request(operation, arguments)
        import time
        transport = SendTransport()
        transport.document.update(draft="synthetic", send_enabled=True, press_supported=True)
        backend = native.NativeBackend(transport=transport, allow_live_send=True)
        token = guard.BoundSend(self.binding, guard.text_hash("synthetic"), "test-live-capability", time.monotonic()+5)
        self.assertIs(backend.press_send(token), guard.PressOutcome.ACCEPTED)
        self.assertEqual(transport.calls.count("press_send"), 1)
        transport.document["binding"]["recipient_id"] = "different-recipient"
        self.blocked(lambda: backend.press_send(token))
        self.assertEqual(transport.calls.count("press_send"), 1)
        expired = guard.BoundSend(self.binding, guard.text_hash("synthetic"), "test-expired", time.monotonic()-1)
        self.blocked(lambda: backend.press_send(expired), "PREPARED_EXPIRED")
        self.assertEqual(transport.calls.count("press_send"), 1)

    def test_profile_process_root_control_or_raw_value_changes_reject(self):
        cases = ("process", "root", "value", "child", "composer", "window")
        for case in cases:
            pair = profile_pair();second = pair["second"]
            if case == "process":
                second["process_epoch"] = "new-epoch"
                for child in second["children"]:child["process_epoch"] = "new-epoch"
            elif case == "root":
                second["root_token"] = "new-root"
                for child in second["children"]:child["parent_root_token"] = "new-root"
            elif case == "value":second["children"][6]["value"]["value"] = "FIXTURE_ALIAS_A"
            elif case == "child":second["children"][6]["token"] = "new-value-node"
            elif case == "composer":second["composer_token"] = "new-composer"
            else:second.update(window_id="new-window",window_association_verified=True)
            with self.subTest(case=case):
                self.blocked(lambda: native.parse_recipient_profile_pair(pair, expected_process_epoch="fixture-epoch"))
        self.blocked(lambda: native.parse_recipient_profile_pair(profile_pair(), expected_process_epoch="old-epoch"), "PROFILE_PROCESS_CHANGED")
        self.blocked(lambda: native.parse_recipient_profile_pair(profile_pair(), expected_process_epoch="fixture-epoch", expected_root_token="old-root"), "PROFILE_ROOT_CHANGED")

    def test_profile_catalog_exact_unique_bridge_never_unlocks_ui(self):
        with tempfile.TemporaryDirectory(prefix="profile-catalog-synthetic-") as directory:
            path = Path(directory) / "contact.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE contact(username,remark,nick_name,alias)")
                connection.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_fixture_a","Fixture","Fixture","fixture_alias_a"))
            fresh = FreshFixture(IdentityCatalog.from_db(path))
            profile = native.parse_recipient_profile_pair(profile_pair(), expected_process_epoch="fixture-epoch")
            matched = native.match_profile_catalog(profile, fresh)
            self.assertEqual(matched.username, "wxid_fixture_a")
            self.assertGreaterEqual(fresh.checks, 3)
            self.assertFalse(matched.ui_identity_confirmed)
            self.assertFalse(matched.to_public_dict()["send_allowed"])
            self.assertEqual(matched.to_public_dict()["account_binding"], "unverified")
            self.assertNotIn("wxid_fixture_a", json.dumps(matched.to_public_dict()))
            fresh.closed = True
            self.blocked(lambda: native.match_profile_catalog(profile, fresh), "PROFILE_CATALOG_UNAVAILABLE")

    def test_profile_catalog_ambiguity_and_case_change_reject(self):
        with tempfile.TemporaryDirectory(prefix="profile-catalog-synthetic-") as directory:
            path = Path(directory) / "contact.db"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE contact(username,remark,nick_name,alias)")
                connection.executemany("INSERT INTO contact VALUES(?,?,?,?)", [
                    ("wxid_fixture_a","A","A","fixture_alias_a"), ("wxid_fixture_b","B","B","fixture_alias_a")])
            fresh = FreshFixture(IdentityCatalog.from_db(path))
            profile = native.parse_recipient_profile_pair(profile_pair(), expected_process_epoch="fixture-epoch")
            self.blocked(lambda: native.match_profile_catalog(profile, fresh), "PROFILE_CATALOG_NOT_UNIQUE")
            self.blocked(lambda: native.match_profile_catalog(replace(profile,raw_weixin_id="FIXTURE_ALIAS_A"), fresh), "PROFILE_CATALOG_NOT_UNIQUE")

    def test_profile_reader_does_not_promote_observation_identity(self):
        self.transport.document.update(binding=None,identity_verified=False,
            native_scope={"launch_token":"fixture-epoch"},recipient_profile=profile_pair())
        profile = self.backend.read_recipient_profile(expected_process_epoch="fixture-epoch")
        self.assertEqual(profile.raw_weixin_id,"fixture_alias_a")
        self.assertIsNone(self.backend.read_observation().binding)
        self.assertFalse(self.backend.probe_report()["identity_verified"])
        self.blocked(lambda: self.backend.read_recipient_profile(expected_process_epoch="old-epoch"), "PROFILE_PROCESS_CHANGED")

    def test_profile_window_link_is_explicit_and_not_filled_from_main(self):
        pair = profile_pair()
        for snapshot in (pair["first"],pair["second"]):snapshot["window_id"]="pretend-main"
        self.blocked(lambda:native.parse_recipient_profile_pair(pair,expected_process_epoch="fixture-epoch"),"PROFILE_WINDOW_ASSOCIATION_UNKNOWN")


if __name__ == "__main__":
    unittest.main()
