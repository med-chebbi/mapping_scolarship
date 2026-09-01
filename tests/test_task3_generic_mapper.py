from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from mapping_v3.arxml import ArxmlIndex  # noqa: E402
from mapping_v3.datatypes import normalize_flync_datatype  # noqa: E402
from mapping_v3.engine import run_mapping  # noqa: E402
from mapping_v3.flync import FlyncModel  # noqa: E402
from mapping_v3.model import Evidence, MatchResult, Status  # noqa: E402
from mapping_v3.output import write_output  # noqa: E402
from mapping_v3.schema_identity import CONTROLLER_IDENTITY, ECU_IDENTITY, ImpliedStrategy, implied_semantic_identity  # noqa: E402
from mapping_v3.status import calculate_status  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def write_yaml(root: Path, relative: str, data: dict) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def write_arxml(root: Path, relative: str, body: str, prefix: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    namespace = "urn:test:autosar"
    if prefix:
        body = body.replace("<", f"<{prefix}:").replace(f"<{prefix}:/", f"</{prefix}:")
        text = f'<{prefix}:AUTOSAR xmlns:{prefix}="{namespace}">{body}</{prefix}:AUTOSAR>'
    else:
        text = f'<AUTOSAR xmlns="{namespace}">{body}</AUTOSAR>'
    path.write_text(text, encoding="utf-8")
    return path


def minimal_flync(root: Path) -> None:
    write_yaml(root, "system_metadata.flync.yaml", {"author": "team", "release": {"version": "1.0.0"}})
    write_yaml(root, "ecus/node_alpha/ecu_metadata.flync.yaml", {"name": "node_alpha", "author": "team"})
    write_yaml(root, "ecus/node_alpha/ports.flync.yaml", {"ports": [{"name": "port_alpha"}]})
    write_yaml(root, "ecus/node_alpha/topology.flync.yaml", {"connections": []})
    write_yaml(
        root,
        "ecus/node_alpha/controllers/controller_alpha/controller_metadata.flync.yaml",
        {"name": "controller_alpha", "controller_metadata": {"type": "embedded", "author": "team"}},
    )


def test_arxml_files_are_recursive_and_filename_independent(tmp_path):
    body = "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME><ELEMENTS><MACHINE-DESIGN><SHORT-NAME>node_alpha</SHORT-NAME></MACHINE-DESIGN></ELEMENTS></AR-PACKAGE></AR-PACKAGES>"  # noqa: E501
    write_arxml(tmp_path, "one/two/arbitrary-name.xml.arxml", body)
    index = ArxmlIndex(tmp_path)
    assert len(index.files) == 1
    assert index.candidates(("MACHINE-DESIGN",), "node_alpha")


def test_namespace_prefix_does_not_change_index(tmp_path):
    body = "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME><ELEMENTS><CAN-CLUSTER><SHORT-NAME>BusA</SHORT-NAME></CAN-CLUSTER></ELEMENTS></AR-PACKAGE></AR-PACKAGES>"  # noqa: E501
    first, second = tmp_path / "first", tmp_path / "second"
    write_arxml(first, "a.arxml", body)
    write_arxml(second, "renamed/wrapped.arxml", body, "ar")
    assert [(item.tag, item.short_name) for item in ArxmlIndex(first).elements] == [
        (item.tag, item.short_name) for item in ArxmlIndex(second).elements
    ]


def test_absolute_nested_reference_resolves(tmp_path):
    body = '<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Pkg</SHORT-NAME><ELEMENTS><ECU-INSTANCE><SHORT-NAME>E1</SHORT-NAME><CONNECTORS><CAN-COMMUNICATION-CONNECTOR><SHORT-NAME>C1</SHORT-NAME></CAN-COMMUNICATION-CONNECTOR></CONNECTORS></ECU-INSTANCE><X><SHORT-NAME>Owner</SHORT-NAME><TARGET-REF DEST="CAN-COMMUNICATION-CONNECTOR">/Pkg/E1/C1</TARGET-REF></X></ELEMENTS></AR-PACKAGE></AR-PACKAGES>'  # noqa: E501
    write_arxml(tmp_path, "a.arxml", body)
    index = ArxmlIndex(tmp_path)
    owner = index.candidates(("X",), "Owner")[0]
    resolution = index.resolve(owner.references[0].value, owner, owner.references[0].dest)
    assert resolution.state == "resolved"


def test_ambiguous_terminal_reference_is_not_guessed(tmp_path):
    body = "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>A</SHORT-NAME><ELEMENTS><IMPLEMENTATION-DATA-TYPE><SHORT-NAME>T</SHORT-NAME></IMPLEMENTATION-DATA-TYPE></ELEMENTS></AR-PACKAGE><AR-PACKAGE><SHORT-NAME>B</SHORT-NAME><ELEMENTS><IMPLEMENTATION-DATA-TYPE><SHORT-NAME>T</SHORT-NAME></IMPLEMENTATION-DATA-TYPE><X><SHORT-NAME>Owner</SHORT-NAME></X></ELEMENTS></AR-PACKAGE></AR-PACKAGES>"  # noqa: E501
    write_arxml(tmp_path, "a.arxml", body)
    index = ArxmlIndex(tmp_path)
    resolution = index.resolve("T", None, "IMPLEMENTATION-DATA-TYPE")
    assert resolution.state == "ambiguous" and len(resolution.candidates) == 2


def test_missing_datatype_target_is_explicit(tmp_path):
    body = '<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><ARGUMENT-DATA-PROTOTYPE><SHORT-NAME>payload</SHORT-NAME><TYPE-TREF DEST="APPLICATION-ARRAY-DATA-TYPE">/Types/Missing</TYPE-TREF></ARGUMENT-DATA-PROTOTYPE></ELEMENTS></AR-PACKAGE></AR-PACKAGES>'  # noqa: E501
    write_arxml(tmp_path / "arxml", "service.arxml", body)
    flync = tmp_path / "flync"
    write_yaml(
        flync,
        "communication/someip/services/service.flync.yaml",
        {
            "name": "ServiceA",
            "id": 1,
            "events": [
                {
                    "name": "E",
                    "id": 2,
                    "parameters": [
                        {
                            "name": "payload",
                            "datatype": {"type": "array", "dimensions": [{"kind": "fixed", "length": 2}], "element_type": {"type": "uint8"}},
                        }
                    ],
                }
            ],
            "fields": [],
            "methods": [],
            "eventgroups": [],
        },
    )
    row = next(item for item in run_mapping("adaptive", flync, tmp_path / "arxml") if item.category == "datatype")
    assert row.status == Status.PARTIAL
    assert any(item.result == MatchResult.UNRESOLVED for item in row.evidence)


def test_recursive_nested_struct_and_array_are_normalized():
    value = {
        "type": "array",
        "dimensions": [{"kind": "dynamic", "length_of_length_field": 16}],
        "element_type": {
            "type": "struct",
            "name": "Object",
            "members": [
                {"type": "uint32", "member_name": "id"},
                {"type": "struct", "name": "Position", "member_name": "position", "members": [{"type": "float32", "member_name": "x"}]},
            ],
        },
    }
    flattened = normalize_flync_datatype(value).flatten()
    paths = {path for path, _, _ in flattened}
    assert "root.element_type.member[position].member[x]" in paths


def test_nested_datatype_mutation_changes_structure():
    first = normalize_flync_datatype({"type": "struct", "name": "S", "members": [{"type": "uint8", "member_name": "value"}]})
    second = normalize_flync_datatype({"type": "struct", "name": "S", "members": [{"type": "uint32", "member_name": "value"}]})
    assert first.flatten() != second.flatten()


def test_added_and_removed_ecu_and_controller_change_inventory(tmp_path):
    minimal_flync(tmp_path)
    baseline = FlyncModel(tmp_path)
    assert len(baseline.by_category["ecu"]) == 1 and len(baseline.by_category["controller"]) == 1
    write_yaml(tmp_path, "ecus/node_beta/ecu_metadata.flync.yaml", {"author": "team"})
    write_yaml(tmp_path, "ecus/node_beta/controllers/controller_beta/controller_metadata.flync.yaml", {"controller_metadata": {"author": "team"}})
    changed = FlyncModel(tmp_path)
    assert len(changed.by_category["ecu"]) == 2 and len(changed.by_category["controller"]) == 2
    (tmp_path / "ecus/node_beta/ecu_metadata.flync.yaml").unlink()
    (tmp_path / "ecus/node_beta/controllers/controller_beta/controller_metadata.flync.yaml").unlink()
    restored = FlyncModel(tmp_path)
    assert len(restored.by_category["ecu"]) == 1 and len(restored.by_category["controller"]) == 1


def test_added_can_network_and_frame_change_inventory(tmp_path):
    minimal_flync(tmp_path)
    write_yaml(tmp_path, "communication/channels/can/network.flync.yaml", {"name": "NetworkA", "baud_rate": 125000, "frames": []})
    assert len(FlyncModel(tmp_path).by_category.get("can_frame", ())) == 0
    write_yaml(
        tmp_path,
        "communication/channels/can/network.flync.yaml",
        {"name": "NetworkA", "baud_rate": 125000, "frames": [{"name": "FrameA", "type": "can", "can_id": 17, "length": 4, "packed_pdus": []}]},
    )
    assert len(FlyncModel(tmp_path).by_category["can_frame"]) == 1


def test_all_pdu_variants_are_discovered(tmp_path):
    minimal_flync(tmp_path)
    write_yaml(tmp_path, "communication/channels/pdus/standard.flync.yaml", {"name": "P1", "type": "standard", "length": 1, "signals": []})
    write_yaml(
        tmp_path,
        "communication/channels/pdus/container.flync.yaml",
        {"name": "P2", "type": "container", "length": 4, "pdu_id": 2, "contained_pdus": [{"pdu_ref": "P1", "pdu_id": 1}]},
    )
    write_yaml(
        tmp_path,
        "communication/channels/pdus/mux.flync.yaml",
        {"name": "P3", "type": "multiplexed", "length": 2, "selector_signal": {"signal": {"name": "Selector", "type": "uint8"}}, "mux_groups": []},
    )
    model = FlyncModel(tmp_path)
    assert {item.properties.get("type") for item in model.by_category["pdu"]} == {"standard", "container", "multiplexed"}


def test_service_add_remove_and_id_mutation_are_discovered(tmp_path):
    minimal_flync(tmp_path)
    path = write_yaml(
        tmp_path,
        "communication/someip/services/a.flync.yaml",
        {"name": "ServiceA", "id": 10, "events": [], "fields": [], "methods": [], "eventgroups": []},
    )
    before = FlyncModel(tmp_path).by_category["someip_service"]
    write_yaml(
        tmp_path,
        "communication/someip/services/b.flync.yaml",
        {"name": "ServiceB", "id": 20, "events": [], "fields": [], "methods": [], "eventgroups": []},
    )
    after = FlyncModel(tmp_path).by_category["someip_service"]
    assert len(before) == 1 and len(after) == 2
    path.write_text(path.read_text().replace("id: 10", "id: 11"), encoding="utf-8")
    assert next(item for item in FlyncModel(tmp_path).by_category["someip_service"] if item.name == "ServiceA").properties["id"] == 11


def test_vlan_socket_and_topology_context_mutations_are_discovered(tmp_path):
    minimal_flync(tmp_path)
    interface = "ecus/node_alpha/controllers/controller_alpha/ethernet_interfaces/interface_alpha/interface_config.flync.yaml"
    write_yaml(
        tmp_path,
        interface,
        {"virtual_interfaces": [{"name": "v", "vlanid": 44, "addresses": [{"address": "192.0.2.1", "ipv4netmask": "255.255.255.0"}]}]},
    )
    sockets = "ecus/node_alpha/controllers/controller_alpha/ethernet_interfaces/interface_alpha/sockets/s.flync.yaml"
    write_yaml(tmp_path, sockets, {"vlan_id": 44, "sockets": [{"name": "s", "endpoint_address": "192.0.2.1", "port_no": 30000, "protocol": "udp"}]})
    write_yaml(
        tmp_path,
        "topology/system_topology.flync.yaml",
        {"connections": [{"type": "ecu_port_to_ecu_port", "id": "c", "ecu1_port": "a", "ecu2_port": "b"}]},
    )
    model = FlyncModel(tmp_path)
    assert model.by_category["socket"][0].context["vlan"] == 44
    assert model.by_category["topology"][0].properties["type"] == "ecu_port_to_ecu_port"
    assert model.by_category["vlan_interface"][0].properties["vlan_id"] == 44


def test_status_engine_never_maps_weak_name_only_evidence():
    evidence = (Evidence("short-name", MatchResult.SUPPORTING, False, "A", "A"),)
    status, _ = calculate_status(1, evidence)
    assert status == Status.PARTIAL


def test_output_is_byte_deterministic_for_real_task3(tmp_path):
    rows = run_mapping("all", ROOT / "examples/task_3_adas_gateway", ROOT / "Adaptive")
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    write_output(rows, first)
    write_output(rows, second)
    assert first.read_bytes() == second.read_bytes()


def test_same_engine_processes_distinct_workspace_shapes(tmp_path):
    flync = tmp_path / "flync"
    minimal_flync(flync)
    arxml = tmp_path / "arxml"
    write_arxml(
        arxml,
        "machine.arxml",
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><MACHINE-DESIGN><SHORT-NAME>node_alpha</SHORT-NAME></MACHINE-DESIGN></ELEMENTS></AR-PACKAGE></AR-PACKAGES>",  # noqa: E501
    )
    minimal_rows = run_mapping("all", flync, arxml)
    task3_rows = run_mapping("all", ROOT / "examples/task_3_adas_gateway", ROOT / "Adaptive")
    assert minimal_rows and task3_rows
    assert {(row.category, row.flync_element) for row in minimal_rows} != {(row.category, row.flync_element) for row in task3_rows}


def test_unrelated_arxml_does_not_change_existing_rows(tmp_path):
    flync = ROOT / "examples/task_3_adas_gateway"
    copied = tmp_path / "arxml"
    shutil.copytree(ROOT / "Adaptive", copied)
    before = run_mapping("adaptive", flync, copied)
    write_arxml(copied, "wrapper/unrelated.arxml", "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>Unrelated</SHORT-NAME></AR-PACKAGE></AR-PACKAGES>")
    after = run_mapping("adaptive", flync, copied)
    assert [(row.flync_key, row.status) for row in before] == [(row.flync_key, row.status) for row in after]


def test_provider_and_consumer_deployments_are_independently_discovered(tmp_path):
    minimal_flync(tmp_path)
    socket_file = "ecus/node_alpha/controllers/controller_alpha/ethernet_interfaces/interface_alpha/sockets/services.flync.yaml"
    write_yaml(
        tmp_path,
        socket_file,
        {
            "vlan_id": 8,
            "sockets": [
                {
                    "name": "provider",
                    "endpoint_address": "192.0.2.10",
                    "port_no": 32000,
                    "protocol": "udp",
                    "deployments": [{"deployment_type": "someip_provider", "service": 100, "instance_id": 1, "major_version": 1}],
                },
                {
                    "name": "consumer",
                    "endpoint_address": "192.0.2.11",
                    "port_no": 32000,
                    "protocol": "udp",
                    "deployments": [{"deployment_type": "someip_consumer", "service": 100, "instance_id": 1, "major_version": 1}],
                },
            ],
        },
    )
    deployments = FlyncModel(tmp_path).by_category["service_deployment"]
    assert {item.properties["deployment_type"] for item in deployments} == {"someip_provider", "someip_consumer"}
    assert {item.context["ip"] for item in deployments} == {"192.0.2.10", "192.0.2.11"}


def test_removing_provider_changes_discovered_deployments(tmp_path):
    minimal_flync(tmp_path)
    relative = "ecus/node_alpha/controllers/controller_alpha/ethernet_interfaces/interface_alpha/sockets/services.flync.yaml"
    path = write_yaml(
        tmp_path,
        relative,
        {
            "sockets": [
                {
                    "name": "provider",
                    "endpoint_address": "192.0.2.10",
                    "port_no": 32000,
                    "protocol": "udp",
                    "deployments": [{"deployment_type": "someip_provider", "service": 1}],
                },
                {
                    "name": "consumer",
                    "endpoint_address": "192.0.2.11",
                    "port_no": 32000,
                    "protocol": "udp",
                    "deployments": [{"deployment_type": "someip_consumer", "service": 1}],
                },
            ]
        },
    )
    assert len(FlyncModel(tmp_path).by_category["service_deployment"]) == 2
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["sockets"] = data["sockets"][1:]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert len(FlyncModel(tmp_path).by_category["service_deployment"]) == 1


def test_event_method_and_field_id_mutations_change_normalized_properties(tmp_path):
    minimal_flync(tmp_path)
    relative = "communication/someip/services/service.flync.yaml"
    original = {
        "name": "ServiceA",
        "id": 100,
        "events": [{"name": "EventA", "id": 101}],
        "methods": [{"name": "MethodA", "id": 102, "type": "fire_and_forget"}],
        "fields": [{"name": "FieldA", "getter_id": 103}],
        "eventgroups": [],
    }
    path = write_yaml(tmp_path, relative, original)
    before = FlyncModel(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["events"][0]["id"] = 201
    data["methods"][0]["id"] = 202
    data["fields"][0]["getter_id"] = 203
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    after = FlyncModel(tmp_path)
    assert before.by_category["someip_event"][0].properties["id"] == 101
    assert after.by_category["someip_event"][0].properties["id"] == 201
    assert before.by_category["someip_method"][0].properties["id"] == 102
    assert after.by_category["someip_method"][0].properties["id"] == 202
    assert before.by_category["someip_field"][0].properties["getter_id"] == 103
    assert after.by_category["someip_field"][0].properties["getter_id"] == 203


def test_duplicate_socket_port_requires_more_context(tmp_path):
    flync = tmp_path / "flync"
    minimal_flync(flync)
    write_yaml(
        flync,
        "ecus/node_alpha/controllers/controller_alpha/ethernet_interfaces/interface_alpha/sockets/s.flync.yaml",
        {"sockets": [{"name": "unknown", "endpoint_address": "192.0.2.99", "port_no": 30000, "protocol": "udp"}]},
    )
    arxml = tmp_path / "arxml"
    body = "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><SOCKET-ADDRESS><SHORT-NAME>A</SHORT-NAME><PORT-NUMBER>30000</PORT-NUMBER></SOCKET-ADDRESS><SOCKET-ADDRESS><SHORT-NAME>B</SHORT-NAME><PORT-NUMBER>30000</PORT-NUMBER></SOCKET-ADDRESS></ELEMENTS></AR-PACKAGE></AR-PACKAGES>"  # noqa: E501
    write_arxml(arxml, "a.arxml", body)
    row = next(item for item in run_mapping("adaptive", flync, arxml) if item.category == "socket")
    assert row.status == Status.NOT_MAPPED
    assert not row.arxml_elements


def test_removing_arxml_element_removes_credible_candidate(tmp_path):
    flync = tmp_path / "flync"
    minimal_flync(flync)
    arxml = tmp_path / "arxml"
    path = write_arxml(
        arxml,
        "machine.arxml",
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><MACHINE-DESIGN><SHORT-NAME>node_alpha</SHORT-NAME></MACHINE-DESIGN></ELEMENTS></AR-PACKAGE></AR-PACKAGES>",  # noqa: E501
    )
    before = next(item for item in run_mapping("all", flync, arxml) if item.category == "ecu")
    path.write_text('<AUTOSAR xmlns="urn:test:autosar"><AR-PACKAGES /></AUTOSAR>', encoding="utf-8")
    after = next(item for item in run_mapping("all", flync, arxml) if item.category == "ecu")
    assert before.status == Status.PARTIAL and after.status == Status.NOT_MAPPED


def test_flync_filenames_and_directories_do_not_define_semantics(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    documents = (
        ("system_metadata.flync.yaml", {"name": "PlatformA", "author": "team", "release": {"version": "1.0"}}),
        ("ecu_metadata.flync.yaml", {"name": "NodeA", "author": "team"}),
        ("controller_metadata.flync.yaml", {"name": "ControllerA", "controller_metadata": {"type": "embedded"}}),
        (
            "interface_config.flync.yaml",
            {"name": "InterfaceA", "mac_address": "02:00:00:00:00:01", "mii_config": {"type": "rmii"}, "virtual_interfaces": []},
        ),
    )
    for filename, data in documents:
        write_yaml(first, f"canonical/{filename}", data)
        write_yaml(second, f"arbitrary/wrapper/moved-{filename}", data)
    left = FlyncModel(first)
    right = FlyncModel(second)

    def semantic(model):
        return sorted((item.category, item.name, item.properties) for item in model.elements)

    assert semantic(left) == semantic(right)
    assert left.by_category["ethernet_interface"][0].name == "InterfaceA"


def test_same_name_requires_compatible_type_and_never_maps_without_semantics(tmp_path):
    flync = tmp_path / "flync"
    write_yaml(flync, "anything.flync.yaml", {"name": "SharedName", "author": "team"})
    wrong = tmp_path / "wrong"
    write_arxml(
        wrong,
        "renamed.arxml",
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><CAN-CLUSTER><SHORT-NAME>SharedName</SHORT-NAME></CAN-CLUSTER></ELEMENTS></AR-PACKAGE></AR-PACKAGES>",  # noqa: E501
    )  # noqa: E501
    assert run_mapping("all", flync, wrong)[0].status == Status.NOT_MAPPED
    compatible = tmp_path / "compatible"
    write_arxml(
        compatible,
        "renamed.arxml",
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><MACHINE-DESIGN><SHORT-NAME>SharedName</SHORT-NAME></MACHINE-DESIGN></ELEMENTS></AR-PACKAGE></AR-PACKAGES>",  # noqa: E501
    )  # noqa: E501
    assert run_mapping("all", flync, compatible)[0].status == Status.PARTIAL


def test_different_name_with_strong_frame_properties_can_map(tmp_path):
    flync = tmp_path / "flync"
    write_yaml(
        flync,
        "network.flync.yaml",
        {"name": "Bus", "baud_rate": 500000, "frames": [{"name": "FlyncFrame", "can_id": 17, "length": 8, "packed_pdus": []}]},
    )
    arxml = tmp_path / "arxml"
    write_arxml(
        arxml,
        "moved/evidence.arxml",
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><CAN-FRAME><SHORT-NAME>GeneratedFrame</SHORT-NAME><IDENTIFIER>17</IDENTIFIER><FRAME-LENGTH>8</FRAME-LENGTH></CAN-FRAME></ELEMENTS></AR-PACKAGE></AR-PACKAGES>",  # noqa: E501
    )  # noqa: E501
    row = next(item for item in run_mapping("classic", flync, arxml) if item.category == "can_frame")
    assert row.status == Status.MAPPED


def test_socket_requires_non_port_anchor_and_preserves_context_ambiguity(tmp_path):
    flync = tmp_path / "flync"
    write_yaml(
        flync,
        "sockets.flync.yaml",
        {"sockets": [{"name": "S", "endpoint_address": "192.0.2.1", "port_no": 30490, "protocol": "udp"}]},
    )
    matched = tmp_path / "matched"
    write_arxml(
        matched,
        "socket.arxml",
        "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><SOCKET-ADDRESS><SHORT-NAME>X</SHORT-NAME><PORT-NUMBER>30490</PORT-NUMBER><PROTOCOL>udp</PROTOCOL><IPV-4-ADDRESS>192.0.2.1</IPV-4-ADDRESS></SOCKET-ADDRESS></ELEMENTS></AR-PACKAGE></AR-PACKAGES>",  # noqa: E501
    )  # noqa: E501
    assert next(item for item in run_mapping("adaptive", flync, matched) if item.category == "socket").status == Status.MAPPED
    ambiguous = tmp_path / "ambiguous"
    body = "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><SOCKET-ADDRESS><SHORT-NAME>A</SHORT-NAME><PORT-NUMBER>30490</PORT-NUMBER><PROTOCOL>udp</PROTOCOL></SOCKET-ADDRESS><SOCKET-ADDRESS><SHORT-NAME>B</SHORT-NAME><PORT-NUMBER>30490</PORT-NUMBER><PROTOCOL>udp</PROTOCOL></SOCKET-ADDRESS></ELEMENTS></AR-PACKAGE></AR-PACKAGES>"  # noqa: E501
    write_arxml(ambiguous, "socket.arxml", body)
    row = next(item for item in run_mapping("adaptive", flync, ambiguous) if item.category == "socket")
    assert row.status == Status.PARTIAL
    assert any(item.result == MatchResult.AMBIGUOUS for item in row.evidence)


def test_schema_implied_ecu_and_controller_identities_are_preserved(tmp_path):
    ecu_document = tmp_path / "wrapper" / "arbitrary_ecu" / "renamed.flync.yaml"
    controller_document = tmp_path / "elsewhere" / "arbitrary_controller" / "different.flync.yaml"
    assert ECU_IDENTITY.strategy == ImpliedStrategy.FOLDER_NAME
    assert CONTROLLER_IDENTITY.strategy == ImpliedStrategy.FOLDER_NAME
    assert implied_semantic_identity(ECU_IDENTITY, ecu_document) == "arbitrary_ecu"
    assert implied_semantic_identity(CONTROLLER_IDENTITY, controller_document) == "arbitrary_controller"

    root = tmp_path / "workspace"
    write_yaml(root, "one/arbitrary_ecu/renamed.flync.yaml", {"author": "team"})
    write_yaml(root, "two/arbitrary_controller/different.flync.yaml", {"controller_metadata": {"type": "embedded"}})
    model = FlyncModel(root)
    assert model.by_category["ecu"][0].name == "arbitrary_ecu"
    assert model.by_category["controller"][0].name == "arbitrary_controller"
    assert model.by_category["ecu"][0].external_key == "arbitrary_ecu"
    assert model.by_category["controller"][0].external_key == "arbitrary_controller"


def test_schema_identity_matches_machine_and_controller_without_forcing_mapped(tmp_path):
    flync = tmp_path / "flync"
    write_yaml(flync, "wrapped/machine_alpha/anything.flync.yaml", {"author": "team"})
    write_yaml(flync, "wrapped/controller_alpha/anything-else.flync.yaml", {"controller_metadata": {"type": "embedded"}})
    arxml = tmp_path / "arxml"
    body = "<AR-PACKAGES><AR-PACKAGE><SHORT-NAME>P</SHORT-NAME><ELEMENTS><MACHINE-DESIGN><SHORT-NAME>machine_alpha</SHORT-NAME><COMMUNICATION-CONTROLLERS><ETHERNET-COMMUNICATION-CONTROLLER><SHORT-NAME>controller_alpha</SHORT-NAME></ETHERNET-COMMUNICATION-CONTROLLER></COMMUNICATION-CONTROLLERS></MACHINE-DESIGN></ELEMENTS></AR-PACKAGE></AR-PACKAGES>"  # noqa: E501
    write_arxml(arxml, "machine.arxml", body)
    rows = run_mapping("all", flync, arxml)
    assert next(row for row in rows if row.category == "ecu").status == Status.PARTIAL
    assert next(row for row in rows if row.category == "controller").status == Status.PARTIAL


def test_identical_unnamed_external_objects_keep_distinct_schema_keys(tmp_path):
    write_yaml(tmp_path, "a/first_controller/metadata-a.flync.yaml", {"controller_metadata": {"type": "embedded"}})
    write_yaml(tmp_path, "b/second_controller/metadata-b.flync.yaml", {"controller_metadata": {"type": "embedded"}})
    controllers = FlyncModel(tmp_path).by_category["controller"]
    assert {item.name for item in controllers} == {"first_controller", "second_controller"}
    assert len({item.key for item in controllers}) == 2
