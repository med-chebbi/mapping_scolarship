# FLYNC-AUTOSAR Mapping

This repository contains the Task 3 ADAS Gateway FLYNC model, its corresponding AUTOSAR ARXML, the V3 forward mapper, and the V4 reverse mapper.

## Installation

```bash
python -m pip install pyyaml==6.0.3 "pytest>=8,<9"
```

## V3

```bash
python scripts/map_v3.py --domain all --flync examples/task_3_adas_gateway --arxml Adaptive --output v3_mapping.json
```

## V4

```bash
python scripts/map_reverse.py --arxml Adaptive --domain all --concept all --output v4_mapping.json
```

## Tests

```bash
python -m pytest tests/test_task3_generic_mapper.py
python -m pytest tests/test_reverse_mapper.py
```
