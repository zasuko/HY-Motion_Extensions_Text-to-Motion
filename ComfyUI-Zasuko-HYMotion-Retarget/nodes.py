import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import folder_paths


NODE_DIR = Path(__file__).resolve().parent
BLENDER_SCRIPT = NODE_DIR / "blender_retarget_humanoid.py"
DEFAULT_BLENDER = Path(r"C:\Program Files\Blender Foundation\Blender 5.1\blender.exe")


def _resolve_source(path_text: str) -> Path:
    path = Path(path_text.strip().strip('"'))
    if path.is_absolute():
        return path
    normalized = path_text.replace("\\", "/").lstrip("./")
    if normalized.startswith("output/"):
        normalized = normalized[len("output/") :]
    return Path(folder_paths.get_output_directory()) / Path(normalized)


def _resolve_target(path_text: str) -> Path:
    path = Path(path_text.strip().strip('"'))
    if path.is_absolute():
        return path
    normalized = path_text.replace("\\", "/").lstrip("./")
    if normalized.startswith("input/"):
        normalized = normalized[len("input/") :]
    return Path(folder_paths.get_input_directory()) / Path(normalized)


class ZasukoHYMotionRetarget:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source_fbx_paths": ("STRING", {"forceInput": True}),
                "target_fbx_path": (
                    "STRING",
                    {"default": "3d/T-Pose.fbx", "multiline": False},
                ),
                "output_dir": ("STRING", {"default": "hymotion_fbx/zasuko"}),
                "filename_suffix": ("STRING", {"default": "Zasuko"}),
            },
            "optional": {
                "blender_path": (
                    "STRING",
                    {"default": str(DEFAULT_BLENDER), "multiline": False},
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("fbx_paths",)
    FUNCTION = "retarget"
    CATEGORY = "HY-Motion/Zasuko"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def retarget(
        self,
        source_fbx_paths,
        target_fbx_path,
        output_dir,
        filename_suffix,
        blender_path=str(DEFAULT_BLENDER),
    ):
        blender = Path(blender_path.strip().strip('"'))
        target = _resolve_target(target_fbx_path)
        if not blender.is_file():
            raise RuntimeError(f"Blender not found: {blender}")
        if not BLENDER_SCRIPT.is_file():
            raise RuntimeError(f"Retarget script not found: {BLENDER_SCRIPT}")
        if not target.is_file():
            raise RuntimeError(f"Target FBX not found: {target}")

        sources = [
            _resolve_source(line)
            for line in str(source_fbx_paths).splitlines()
            if line.strip().lower().endswith(".fbx")
        ]
        if not sources:
            raise RuntimeError("No source FBX path was received from HY-Motion Export FBX")

        output_root = Path(folder_paths.get_output_directory()) / output_dir
        output_root.mkdir(parents=True, exist_ok=True)
        results = []
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        for source in sources:
            if not source.is_file():
                raise RuntimeError(f"Source FBX not found: {source}")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            safe_suffix = "".join(c if c.isalnum() or c in "-_" else "_" for c in filename_suffix)
            output = output_root / f"{source.stem}_{safe_suffix}_{stamp}_{uuid.uuid4().hex[:6]}.fbx"
            preview = output.with_name(f"{output.stem}_preview.glb")
            command = [
                str(blender),
                "--background",
                "--python",
                str(BLENDER_SCRIPT),
                "--",
                str(source),
                str(target),
                str(output),
                str(preview),
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=300,
                creationflags=creationflags,
            )
            combined_log = (completed.stdout or "") + "\n" + (completed.stderr or "")
            if completed.returncode != 0 or not output.is_file() or not preview.is_file():
                raise RuntimeError(
                    "Blender retarget failed. Last log lines:\n"
                    + "\n".join(combined_log.splitlines()[-20:])
                )
            print(f"[Zasuko Retarget] Saved: {output}")
            print(f"[Zasuko Retarget] In-place GLB preview: {preview}")
            results.append(os.path.relpath(preview, folder_paths.get_output_directory()))

        return ("\n".join(results),)


NODE_CLASS_MAPPINGS = {"ZasukoHYMotionRetarget": ZasukoHYMotionRetarget}
NODE_DISPLAY_NAME_MAPPINGS = {
    "ZasukoHYMotionRetarget": "HY-Motion Retarget to Zasuko (Blender)"
}
