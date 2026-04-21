#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Parse model_spec.json into a typed catalog with family grouping."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

_FAMILY_STOP_WORDS = {
    "instruct", "chat", "base", "distill", "v0", "v1", "v2", "v3", "b", "it",
    "hf", "bf16", "fp16", "int4", "int8", "gptq", "awq", "gguf", "preview",
    "diffusers", "t2v", "i2v", "a14b",
}


def extract_family(model_name: str) -> str:
    """Strip HF namespace, version/size tokens to get the model family."""
    name = model_name.split("/")[-1]
    parts = re.split(r"[-.]", name)
    for part in parts:
        p = part.lower()
        if p and p not in _FAMILY_STOP_WORDS and not re.fullmatch(r"\d+(\.\d+)?[bBmMkK]?", part):
            # Strip trailing digits (e.g. "Wan2" → "Wan", "Qwen3" → "Qwen")
            return re.sub(r"\d+$", "", part) or part
    return parts[0] if parts else name


@dataclass
class ModelEntry:
    model_id: str
    model_name: str       # key in model_spec (often an internal ID, use hf_model_repo for display)
    display_name: str     # short name for UI
    hf_model_repo: str    # HF repo path e.g. "meta-llama/Llama-3.3-70B-Instruct"
    model_type: str       # "LLM", "VLM", "IMAGE", "VIDEO", etc.
    family: str           # extracted: "Llama", "Qwen", etc.
    device_type: str      # "T3K", "N150", "P300X2", etc.
    inference_engine: str # "vllm", "media", "forge"
    docker_image: str
    status: str
    param_count: Optional[float]
    min_disk_gb: Optional[float]
    min_ram_gb: Optional[float]


class ModelCatalog:
    def __init__(self, entries: List[ModelEntry]):
        self._entries = entries

    @classmethod
    def load(cls, spec_path: Path) -> "ModelCatalog":
        data = json.loads(Path(spec_path).read_text())
        entries = []
        for model_key, device_map in data.get("model_specs", {}).items():
            if not isinstance(device_map, dict):
                continue
            for device_type, engine_map in device_map.items():
                if not isinstance(engine_map, dict):
                    continue
                for engine_name, impl_map in engine_map.items():
                    if not isinstance(impl_map, dict):
                        continue
                    # impl_map is {impl_key: {fields...}} — take first impl
                    impl = {}
                    for v in impl_map.values():
                        if isinstance(v, dict):
                            impl = v
                            break
                    if not impl:
                        continue

                    hf_repo = impl.get("hf_model_repo") or model_key
                    display = hf_repo.split("/")[-1] if "/" in hf_repo else hf_repo
                    entries.append(ModelEntry(
                        model_id=impl.get("model_id", model_key),
                        model_name=model_key,
                        display_name=display,
                        hf_model_repo=hf_repo,
                        model_type=impl.get("model_type", "LLM"),
                        family=extract_family(hf_repo),
                        device_type=device_type,
                        inference_engine=engine_name.lower(),
                        docker_image=impl.get("docker_image", ""),
                        status=impl.get("status", "EXPERIMENTAL"),
                        param_count=impl.get("param_count"),
                        min_disk_gb=impl.get("min_disk_gb"),
                        min_ram_gb=impl.get("min_ram_gb"),
                    ))
        return cls(entries)

    def get_tree(self) -> Dict[str, Dict[str, List[ModelEntry]]]:
        """Return {model_type: {family: [ModelEntry]}} sorted."""
        tree: Dict[str, Dict[str, List[ModelEntry]]] = {}
        for e in self._entries:
            tree.setdefault(e.model_type, {}).setdefault(e.family, []).append(e)
        return tree

    def get_compatible(self, device_types: List[str]) -> "ModelCatalog":
        upper = [d.upper() for d in device_types]
        return ModelCatalog([e for e in self._entries if e.device_type.upper() in upper])

    def get_entry(self, model_name: str, device: str) -> Optional[ModelEntry]:
        for e in self._entries:
            if e.model_name == model_name and e.device_type.upper() == device.upper():
                return e
        return None

    def all_device_types(self) -> List[str]:
        return sorted(set(e.device_type for e in self._entries))

    def all_entries(self) -> List[ModelEntry]:
        return list(self._entries)
