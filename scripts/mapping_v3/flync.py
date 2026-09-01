"""FLYNC YAML discovery and normalization into instance-independent elements."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml

from .datatypes import normalize_flync_datatype
from .model import Domain, FlyncElement, FlyncReference, SourceLocation
from .schema_identity import CONTROLLER_IDENTITY, ECU_IDENTITY, implied_semantic_identity


class FlyncError(RuntimeError):
    """Raised for invalid FLYNC input."""


def _join(path: tuple[str | int, ...]) -> str:
    return "$" + "".join(f"[{value}]" if isinstance(value, int) else f".{value}" for value in path)


class FlyncModel:
    """Deterministically discovers supported schema objects from a FLYNC workspace."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise FlyncError(f"FLYNC root is not a directory: {root}")
        self.files = tuple(sorted(self.root.rglob("*.flync.yaml"), key=lambda value: value.as_posix().lower()))
        if not self.files:
            raise FlyncError(f"No .flync.yaml files found below: {root}")
        self.documents: dict[str, Any] = {}
        elements: list[FlyncElement] = []
        for path in self.files:
            relative = path.relative_to(self.root).as_posix()
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except yaml.YAMLError as exc:
                raise FlyncError(f"Invalid YAML in {relative}: {exc}") from exc
            if not isinstance(data, dict):
                raise FlyncError(f"FLYNC document must contain a mapping: {relative}")
            self.documents[relative] = data
            elements.extend(self._normalize(relative, data))
        keys: set[str] = set()
        for element in elements:
            if element.key in keys:
                raise FlyncError(f"Duplicate normalized FLYNC key: {element.key}")
            keys.add(element.key)
        self.elements = tuple(sorted(elements, key=lambda value: value.key))
        self.by_key = {element.key: element for element in self.elements}
        grouped: dict[str, list[FlyncElement]] = defaultdict(list)
        for element in self.elements:
            grouped[element.category].append(element)
        self.by_category = {key: tuple(value) for key, value in grouped.items()}

    def _element(
        self,
        file: str,
        yaml_path: tuple[str | int, ...],
        category: str,
        name: Any,
        domains: Iterable[Domain],
        *,
        parent: str | None = None,
        context: dict[str, Any] | None = None,
        properties: dict[str, Any] | None = None,
        references: Iterable[FlyncReference] = (),
        datatype=None,
        external_key: str | None = None,
    ) -> FlyncElement:
        source = SourceLocation(file, _join(yaml_path))
        return FlyncElement(
            key=f"{file}:{source.path}:{category}",
            category=category,
            domains=frozenset(domains),
            name=str(name),
            source=source,
            external_key=external_key,
            parent=parent,
            context=context or {},
            properties=properties or {},
            references=tuple(references),
            datatype=datatype,
        )

    def _normalize(self, file: str, data: dict[str, Any]) -> list[FlyncElement]:  # noqa: C901
        output: list[FlyncElement] = []
        context = {key: data[key] for key in ("ecu", "controller", "interface") if data.get(key) is not None}
        shared = (Domain.SHARED,)
        classic = (Domain.CLASSIC,)
        adaptive = (Domain.ADAPTIVE,)

        if _document_kind(data) == "system":
            output.append(self._element(file, (), "system", data.get("name", "System"), shared, properties=data))
        if _document_kind(data) == "ecu":
            external_key = implied_semantic_identity(ECU_IDENTITY, self.root / file)
            ecu_name = data.get("name") or external_key or _content_identity("ecu", data)
            output.append(
                self._element(
                    file,
                    (),
                    "ecu",
                    ecu_name,
                    shared,
                    context={"ecu": ecu_name},
                    properties=data,
                    external_key=external_key,
                )
            )
        if "controller_metadata" in data:
            metadata = data["controller_metadata"] or {}
            external_key = implied_semantic_identity(CONTROLLER_IDENTITY, self.root / file)
            controller_name = data.get("name") or metadata.get("name") or external_key or _content_identity("controller", metadata)
            output.append(
                self._element(
                    file,
                    ("controller_metadata",),
                    "controller",
                    controller_name,
                    shared,
                    context=context | {"controller": controller_name},
                    properties=metadata,
                    external_key=external_key,
                )
            )
        if "ports" in data and not {"vlans", "name"} <= set(data):
            for index, port in enumerate(data.get("ports") or []):
                output.append(self._element(file, ("ports", index), "ecu_port", port.get("name", index), adaptive, context=context, properties=port))
        if "baud_rate" in data and isinstance(data.get("frames"), list):
            bus = data.get("name") or _content_identity("can-bus", data)
            bus_key = f"{file}:$.:can_bus"
            output.append(
                self._element(file, (), "can_bus", bus, classic, properties={key: value for key, value in data.items() if key != "frames"})
            )
            for index, frame in enumerate(data["frames"]):
                refs = tuple(FlyncReference("pdu_ref", item.get("pdu_ref"), "pdu") for item in frame.get("packed_pdus", []))
                output.append(
                    self._element(
                        file,
                        ("frames", index),
                        "can_frame",
                        frame.get("name", index),
                        classic,
                        parent=bus_key,
                        context={"network": bus},
                        properties={key: value for key, value in frame.items() if key not in {"packed_pdus", "timing"}},
                        references=refs,
                    )
                )
        if data.get("type") in {"standard", "multiplexed", "container"}:
            output.extend(self._pdu(file, (), data, None))
        if "bus_ref" in data and ("sender_frames" in data or "receiver_frames" in data):
            refs = []
            for direction in ("sender_frames", "receiver_frames"):
                refs.extend(FlyncReference(direction, item.get("frame_ref"), "can_frame") for item in data.get(direction, []))
            output.append(
                self._element(
                    file,
                    (),
                    "can_interface",
                    data.get("name") or _content_identity("can-interface", data),
                    classic,
                    context=context | {"network": data.get("bus_ref")},
                    properties={"bus_ref": data.get("bus_ref")},
                    references=refs,
                )
            )
        if _document_kind(data) == "ethernet_interface":
            interface = data.get("name") or _content_identity("ethernet-interface", data)
            interface_context = context | {"interface": interface}
            interface_key = f"{file}:$.:ethernet_interface"
            output.append(
                self._element(
                    file,
                    (),
                    "ethernet_interface",
                    interface,
                    adaptive,
                    context=interface_context,
                    properties={key: value for key, value in data.items() if key != "virtual_interfaces"},
                )
            )
            for index, virtual in enumerate(data.get("virtual_interfaces") or []):
                virtual_context = interface_context | {"vlan": virtual.get("vlanid")}
                output.append(
                    self._element(
                        file,
                        ("virtual_interfaces", index),
                        "vlan_interface",
                        virtual.get("name", index),
                        adaptive,
                        parent=interface_key,
                        context=virtual_context,
                        properties={"vlan_id": virtual.get("vlanid")},
                    )
                )
                for address_index, address in enumerate(virtual.get("addresses") or []):
                    output.append(
                        self._element(
                            file,
                            ("virtual_interfaces", index, "addresses", address_index),
                            "network_endpoint",
                            address.get("address"),
                            adaptive,
                            parent=interface_key,
                            context=virtual_context,
                            properties=address,
                        )
                    )
                for multicast_index, address in enumerate(virtual.get("multicast") or []):
                    output.append(
                        self._element(
                            file,
                            ("virtual_interfaces", index, "multicast", multicast_index),
                            "multicast",
                            address,
                            adaptive,
                            parent=interface_key,
                            context=virtual_context,
                            properties={"address": address},
                        )
                    )
        if "sockets" in data:
            for index, socket in enumerate(data.get("sockets") or []):
                deployments = tuple(socket.get("deployments") or [])
                refs = tuple(
                    FlyncReference(item.get("deployment_type", "deployment"), item.get("service") or item.get("pdu_ref")) for item in deployments
                )
                socket_context = context | {
                    "vlan": data.get("vlan_id"),
                    "ip": socket.get("endpoint_address"),
                    "protocol": socket.get("protocol"),
                    "port": socket.get("port_no"),
                }
                output.append(
                    self._element(
                        file,
                        ("sockets", index),
                        "socket",
                        socket.get("name", index),
                        adaptive,
                        context=socket_context,
                        properties={key: value for key, value in socket.items() if key != "deployments"},
                        references=refs,
                    )
                )
                for deployment_index, deployment in enumerate(deployments):
                    name = f"{socket.get('name', index)}:{deployment.get('deployment_type', deployment_index)}"
                    output.append(
                        self._element(
                            file,
                            ("sockets", index, "deployments", deployment_index),
                            "service_deployment",
                            name,
                            adaptive,
                            context=socket_context,
                            properties=deployment,
                        )
                    )
        if {"id", "events", "fields", "methods", "eventgroups"} <= set(data):
            output.extend(self._service(file, data))
        if "tcp_profiles" in data:
            for index, profile in enumerate(data.get("tcp_profiles") or []):
                output.append(
                    self._element(file, ("tcp_profiles", index), "tcp_profile", profile.get("tcp_profile_id", index), adaptive, properties=profile)
                )
        if "sd_timings" in data and "ip_address" in data:
            output.append(
                self._element(
                    file,
                    (),
                    "service_discovery",
                    data["ip_address"],
                    adaptive,
                    properties={key: value for key, value in data.items() if key != "sd_timings"},
                )
            )
            for index, profile in enumerate(data.get("sd_timings") or []):
                output.append(
                    self._element(file, ("sd_timings", index), "sd_timing", profile.get("profile_id", index), adaptive, properties=profile)
                )
        if "defaults" in data and "profiles" in data:
            for collection in ("defaults", "profiles"):
                for index, profile in enumerate(data.get(collection) or []):
                    output.append(
                        self._element(file, (collection, index), "someip_timing", profile.get("profile_id", index), adaptive, properties=profile)
                    )
        if "ports" in data and "vlans" in data and "name" in data:
            switch_name = data["name"]
            output.append(
                self._element(
                    file,
                    (),
                    "switch",
                    switch_name,
                    adaptive,
                    context=context,
                    properties={key: value for key, value in data.items() if key not in {"ports", "vlans"}},
                )
            )
            for index, port in enumerate(data.get("ports") or []):
                output.append(
                    self._element(
                        file,
                        ("ports", index),
                        "switch_port",
                        port.get("name", index),
                        adaptive,
                        context=context | {"switch": switch_name},
                        properties=port,
                    )
                )
            for index, vlan in enumerate(data.get("vlans") or []):
                refs = tuple(FlyncReference("switch_port", value, "switch_port") for value in vlan.get("ports", []))
                output.append(
                    self._element(
                        file,
                        ("vlans", index),
                        "vlan",
                        vlan.get("name", index),
                        adaptive,
                        context=context | {"switch": switch_name, "vlan": vlan.get("id")},
                        properties=vlan,
                        references=refs,
                    )
                )
        if "connections" in data:
            for index, connection in enumerate(data.get("connections") or []):
                refs = tuple(FlyncReference(key, value) for key, value in connection.items() if key not in {"type", "id"})
                output.append(
                    self._element(
                        file,
                        ("connections", index),
                        "topology",
                        connection.get("id", index),
                        shared,
                        context=context,
                        properties={"type": connection.get("type")},
                        references=refs,
                    )
                )
        return output

    def _pdu(self, file: str, path: tuple[str | int, ...], data: dict[str, Any], parent: str | None) -> list[FlyncElement]:
        output: list[FlyncElement] = []
        pdu_type = data["type"]
        element = self._element(
            file,
            path,
            "pdu",
            data.get("name", _join(path)),
            (Domain.CLASSIC,),
            parent=parent,
            properties={
                key: value
                for key, value in data.items()
                if key not in {"signals", "signal_groups", "selector_signal", "static_group", "mux_groups", "contained_pdus"}
            },
            references=tuple(FlyncReference("contained_pdu", item.get("pdu_ref"), "pdu") for item in data.get("contained_pdus", [])),
        )
        output.append(element)
        if pdu_type == "standard":
            for index, signal_instance in enumerate(data.get("signals") or []):
                signal = signal_instance.get("signal") or {}
                properties = signal | {key: value for key, value in signal_instance.items() if key != "signal"}
                output.append(
                    self._element(
                        file,
                        (*path, "signals", index),
                        "signal",
                        signal.get("name", index),
                        (Domain.CLASSIC,),
                        parent=element.key,
                        properties=properties,
                    )
                )
            for index, group in enumerate(data.get("signal_groups") or []):
                reference = group.get("signal_group")
                name = reference.get("name", index) if isinstance(reference, dict) else reference or index
                output.append(
                    self._element(
                        file,
                        (*path, "signal_groups", index),
                        "signal_group",
                        name,
                        (Domain.CLASSIC,),
                        parent=element.key,
                        properties={key: value for key, value in group.items() if key != "signal_group"},
                        references=(FlyncReference("signal_group", name, "signal_group"),),
                    )
                )
        elif pdu_type == "multiplexed":
            selector = data.get("selector_signal") or {}
            signal = selector.get("signal") or {}
            output.append(
                self._element(
                    file,
                    (*path, "selector_signal"),
                    "signal",
                    signal.get("name", "selector"),
                    (Domain.CLASSIC,),
                    parent=element.key,
                    properties=signal | {key: value for key, value in selector.items() if key != "signal"},
                )
            )
            for index, group in enumerate(data.get("mux_groups") or []):
                nested = group.get("pdu") or {}
                output.extend(self._pdu(file, (*path, "mux_groups", index, "pdu"), nested, element.key))
            static = data.get("static_group")
            if isinstance(static, dict):
                output.extend(self._pdu(file, (*path, "static_group"), static, element.key))
        elif pdu_type == "container":
            for index, contained in enumerate(data.get("contained_pdus") or []):
                output.append(
                    self._element(
                        file,
                        (*path, "contained_pdus", index),
                        "pdu_containment",
                        contained.get("pdu_ref", index),
                        (Domain.ADAPTIVE,),
                        parent=element.key,
                        properties=contained,
                        references=(FlyncReference("contained_pdu", contained.get("pdu_ref"), "pdu"),),
                    )
                )
        return output

    def _service(self, file: str, data: dict[str, Any]) -> list[FlyncElement]:
        output: list[FlyncElement] = []
        service = data.get("name") or _content_identity("someip-service", data)
        service_element = self._element(
            file,
            (),
            "someip_service",
            service,
            (Domain.ADAPTIVE,),
            properties={key: value for key, value in data.items() if key not in {"events", "fields", "methods", "eventgroups", "meta"}},
        )
        output.append(service_element)
        for collection, category in (("events", "someip_event"), ("fields", "someip_field"), ("methods", "someip_method")):
            for index, member in enumerate(data.get(collection) or []):
                member_element = self._element(
                    file,
                    (collection, index),
                    category,
                    member.get("name", index),
                    (Domain.ADAPTIVE,),
                    parent=service_element.key,
                    context={"service": service, "service_id": data.get("id")},
                    properties={key: value for key, value in member.items() if key not in {"parameters", "input_parameters", "output_parameters"}},
                )
                output.append(member_element)
                for parameter_collection in ("parameters", "input_parameters", "output_parameters"):
                    for parameter_index, parameter in enumerate(member.get(parameter_collection) or []):
                        datatype = normalize_flync_datatype(parameter.get("datatype"))
                        output.append(
                            self._element(
                                file,
                                (collection, index, parameter_collection, parameter_index),
                                "datatype",
                                parameter.get("name", parameter_index),
                                (Domain.ADAPTIVE,),
                                parent=member_element.key,
                                context={"service": service, "member": member.get("name"), "direction": parameter_collection},
                                properties={"parameter": parameter.get("name")},
                                datatype=datatype,
                            )
                        )
        for index, group in enumerate(data.get("eventgroups") or []):
            member_names = tuple(item.get("name") if isinstance(item, dict) else item for item in group.get("events", []))
            output.append(
                self._element(
                    file,
                    ("eventgroups", index),
                    "eventgroup",
                    group.get("name", index),
                    (Domain.ADAPTIVE,),
                    parent=service_element.key,
                    context={"service": service, "service_id": data.get("id")},
                    properties={"id": group.get("id"), "members": member_names},
                )
            )
        return output


def _content_identity(category: str, data: dict[str, Any]) -> str:
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{category}:{digest}"


def _document_kind(data: dict[str, Any]) -> str | None:
    keys = set(data)
    metadata_keys = {"name", "author", "compatible_flync_version", "release", "oem", "platform", "target_system", "type"}
    if "release" in data or keys & {"oem", "platform"}:
        return "system"
    if keys and keys <= metadata_keys and "author" in data:
        return "ecu"
    if "virtual_interfaces" in data or ("mac_address" in data and keys & {"mii_config", "compute_nodes"}):
        return "ethernet_interface"
    return None
