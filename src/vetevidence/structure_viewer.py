"""Streamlit CCv2 wrapper for a caller-supplied local 3Dmol.js 2.5.5 build."""

from __future__ import annotations

import base64
import hashlib
import io
import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol

import streamlit as st
from PIL import Image, UnidentifiedImageError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


_SUPPORTED_3DMOL_VERSION = "2.5.5"
_DEFAULT_3DMOL_SHA256 = (
    "95513f6494717cc82fb2ba4d264f29b7ef189a31d4ece36a38d1f9666bf6d427"
)
_DEFAULT_3DMOL_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "vendor"
    / "3dmol"
    / "3Dmol.es6-min.js"
)
_MAX_STRUCTURE_CHARACTERS = 25 * 1024 * 1024
_MAX_PNG_BYTES = 25 * 1024 * 1024
_MAX_PNG_PIXELS = 100_000_000
_PNG_PREFIX = "data:image/png;base64,"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SUPPORTED_FORMATS = frozenset({"cif", "mmcif", "mol2", "pdb", "sdf", "xyz"})
_PRODUCTION_COMPONENTS: dict[str, ComponentRenderer] = {}

_VIEWER_HTML = """
<section class="vetevidence-structure-viewer">
  <div class="viewer-toolbar">
    <span class="viewer-score"></span>
    <button class="viewer-export" type="button">导出当前视图 PNG</button>
  </div>
  <div class="viewer-canvas" aria-label="蛋白配体三维结构查看器"></div>
  <div class="viewer-message" role="status"></div>
</section>
""".strip()

_VIEWER_CSS = """
.vetevidence-structure-viewer {
  display: grid;
  gap: 0.5rem;
  color: var(--st-text-color);
}
.viewer-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  min-height: 2.25rem;
}
.viewer-score {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}
.viewer-export {
  border: 1px solid var(--st-border-color);
  border-radius: var(--st-button-border-radius);
  background: var(--st-secondary-background-color);
  color: var(--st-text-color);
  cursor: pointer;
  padding: 0.4rem 0.7rem;
}
.viewer-canvas {
  position: relative;
  width: 100%;
  height: 34rem;
  min-height: 22rem;
  overflow: hidden;
  border: 1px solid var(--st-border-color);
  border-radius: var(--st-border-radius);
  background: white;
}
.viewer-message {
  min-height: 1.25rem;
  color: var(--st-caption-color);
}
""".strip()

_VIEWER_WRAPPER_JS = r"""
export default function(component) {
  const {
    data,
    parentElement,
    setStateValue,
    setTriggerValue,
  } = component

  const canvas = parentElement.querySelector(".viewer-canvas")
  const exportButton = parentElement.querySelector(".viewer-export")
  const scoreElement = parentElement.querySelector(".viewer-score")
  const messageElement = parentElement.querySelector(".viewer-message")
  if (!canvas || !exportButton || !scoreElement || !messageElement) return

  const library = (
    typeof createViewer === "function" ? { createViewer } : null
  )
  if (!library || typeof library.createViewer !== "function") {
    const message = "本地 3Dmol.js 2.5.5 未加载，三维查看器不可用。"
    messageElement.textContent = message
    setStateValue("error", message)
    return
  }

  canvas.replaceChildren()
  messageElement.textContent = ""
  const score = data?.score
  scoreElement.textContent = score
    ? `${score.metric}: ${score.value_kcal_mol} kcal/mol (mode ${score.mode}, seed ${score.seed})`
    : ""

  let viewer
  try {
    viewer = library.createViewer(canvas, {
      antialias: true,
      backgroundColor: "white",
    })
    const model = viewer.addModel(
      data.structure_data,
      data.structure_format === "mmcif" ? "cif" : data.structure_format,
    )
    viewer.setStyle({}, { cartoon: { color: "spectrum" } })
    const ligandSelection = {
      resn: data.ligand_residue_name,
      chain: data.ligand_chain,
      resi: data.ligand_residue_number,
    }
    viewer.setStyle(
      ligandSelection,
      { stick: { colorscheme: "orangeCarbon", radius: 0.2 } },
    )
    viewer.setStyle(
      {
        within: {
          distance: data.pocket_distance_angstrom,
          sel: ligandSelection,
        },
      },
      { stick: { colorscheme: "cyanCarbon", radius: 0.14 } },
    )

    for (const interaction of data.interactions ?? []) {
      viewer.addCylinder({
        start: {
          x: interaction.protein_xyz[0],
          y: interaction.protein_xyz[1],
          z: interaction.protein_xyz[2],
        },
        end: {
          x: interaction.ligand_xyz[0],
          y: interaction.ligand_xyz[1],
          z: interaction.ligand_xyz[2],
        },
        radius: 0.07,
        color: interaction.color,
        dashed: true,
      })
    }

    model.setClickable({}, true, (atom) => {
      setTriggerValue("selected_atom", {
        atom: atom.atom ?? null,
        chain: atom.chain ?? null,
        residue_name: atom.resn ?? null,
        residue_number: atom.resi ?? null,
        serial: atom.serial ?? null,
      })
    })
    viewer.zoomTo(ligandSelection)
    viewer.zoom(0.8)
    viewer.render()
    if (data.current_error !== null && data.current_error !== undefined) {
      setStateValue("error", null)
    }
  } catch (error) {
    const message = `三维结构解析失败: ${String(error)}`
    messageElement.textContent = message
    setStateValue("error", message)
    return
  }

  exportButton.onclick = () => {
    try {
      const pngDataUri = viewer.pngURI()
      setTriggerValue("png_data_uri", pngDataUri)
      messageElement.textContent = "当前视图 PNG 已生成。"
    } catch (error) {
      const message = `PNG 导出失败: ${String(error)}`
      messageElement.textContent = message
      setStateValue("error", message)
    }
  }

  return () => {
    exportButton.onclick = null
    try {
      viewer.removeAllModels()
      viewer.removeAllShapes()
      viewer.removeAllLabels()
    } catch (_) {
      // Cleanup is best effort only.
    }
    canvas.replaceChildren()
  }
}
""".strip()


class StructureViewerModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class Local3DmolLibrary(BaseModel):
    """Exact local JavaScript bytes; never fetched from a CDN."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=False,
    )

    version: Literal["2.5.5"]
    source_bytes: bytes = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def hash_matches_source(self) -> Local3DmolLibrary:
        actual = hashlib.sha256(self.source_bytes).hexdigest()
        if actual != self.sha256:
            raise ValueError("本地 3Dmol.js SHA-256 不匹配。")
        try:
            self.source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("本地 3Dmol.js 必须是 UTF-8 JavaScript。") from exc
        return self

    @property
    def source(self) -> str:
        return self.source_bytes.decode("utf-8")


class ViewerInteraction(StructureViewerModel):
    interaction_type: str = Field(min_length=1)
    protein_xyz: tuple[float, float, float]
    ligand_xyz: tuple[float, float, float]
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")


class StructureViewerResult(StructureViewerModel):
    png_data_uri: str | None = None
    selected_atom: dict[str, object] | None = None
    error: str | None = None


class ComponentRenderer(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


class ComponentFactory(Protocol):
    def __call__(self, name: str, **kwargs: object) -> ComponentRenderer: ...


def load_local_3dmol_library(
    path: str | Path,
    *,
    expected_sha256: str,
    version: Literal["2.5.5"] = _SUPPORTED_3DMOL_VERSION,
) -> Local3DmolLibrary:
    """Load a pinned local asset and reject substitutions."""

    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("3Dmol.js 本地路径不是文件。")
    return Local3DmolLibrary(
        version=version,
        source_bytes=resolved.read_bytes(),
        sha256=expected_sha256,
    )


def load_default_3dmol_library() -> Local3DmolLibrary:
    """Load the repository-vendored ES6 build with its pinned upstream hash."""

    return load_local_3dmol_library(
        _DEFAULT_3DMOL_PATH,
        expected_sha256=_DEFAULT_3DMOL_SHA256,
    )


def create_structure_viewer_component(
    library: Local3DmolLibrary,
    *,
    component_factory: ComponentFactory | None = None,
) -> ComponentRenderer:
    """Register one CCv2 component using the pinned local ES module."""

    if (
        component_factory is None
        and library.sha256 != _DEFAULT_3DMOL_SHA256
    ):
        raise ValueError(
            "生产三维查看器只允许仓库固定 SHA-256 的 3Dmol.js 2.5.5。"
        )

    component_name = (
        "vetevidence_structure_viewer_"
        + library.version.replace(".", "_")
        + "_"
        + library.sha256[:12]
    )
    javascript = library.source + "\n\n" + _VIEWER_WRAPPER_JS
    if component_factory is None and component_name in _PRODUCTION_COMPONENTS:
        return _PRODUCTION_COMPONENTS[component_name]
    factory = component_factory or st.components.v2.component
    renderer = factory(
        component_name,
        html=_VIEWER_HTML,
        css=_VIEWER_CSS,
        js=javascript,
        isolate_styles=True,
    )
    if component_factory is None:
        _PRODUCTION_COMPONENTS[component_name] = renderer
    return renderer


def _validate_structure_data(
    structure_data: str,
    structure_format: str,
) -> str:
    if not structure_data.strip():
        raise ValueError("三维查看结构为空。")
    if len(structure_data) > _MAX_STRUCTURE_CHARACTERS:
        raise ValueError("三维查看结构超过 25 MB。")
    normalized_format = structure_format.casefold().removeprefix(".")
    if normalized_format not in _SUPPORTED_FORMATS:
        allowed = "、".join(sorted(_SUPPORTED_FORMATS))
        raise ValueError(f"三维查看器仅支持：{allowed}。")
    return normalized_format


def _finite_triplet(value: tuple[float, float, float]) -> bool:
    return len(value) == 3 and all(math.isfinite(item) for item in value)


def mount_structure_viewer(
    renderer: ComponentRenderer,
    *,
    structure_data: str,
    structure_format: str,
    vina_score_kcal_mol: float,
    pose_mode: int,
    seed: int,
    interactions: Sequence[ViewerInteraction] = (),
    ligand_residue_name: str = "LIG",
    ligand_chain: str,
    ligand_residue_number: int,
    pocket_distance_angstrom: float = 5.0,
    key: str | None = None,
    height: int = 620,
    on_png_data_uri_change: Callable[[], None] | None = None,
    on_selected_atom_change: Callable[[], None] | None = None,
    on_error_change: Callable[[], None] | None = None,
) -> StructureViewerResult:
    """Mount with molecular text exclusively in ``data``, never HTML/JS."""

    normalized_format = _validate_structure_data(
        structure_data,
        structure_format,
    )
    if not math.isfinite(vina_score_kcal_mol):
        raise ValueError("Vina 预测评分必须是有限数值。")
    if pose_mode < 1:
        raise ValueError("Vina pose mode 必须大于等于 1。")
    if not 1 <= len(ligand_residue_name) <= 3 or not ligand_residue_name.isalnum():
        raise ValueError("配体残基名必须是 1 到 3 个字母或数字。")
    if (
        len(ligand_chain) != 1
        or not ligand_chain.isascii()
        or not ligand_chain.isalnum()
    ):
        raise ValueError("配体链必须是单个 ASCII 字母或数字。")
    if not 1 <= ligand_residue_number <= 9999:
        raise ValueError("配体残基编号必须在 1 到 9999 之间。")
    if not 1.0 <= pocket_distance_angstrom <= 15.0:
        raise ValueError("口袋显示距离必须在 1 到 15 Å 之间。")
    if not 300 <= height <= 1600:
        raise ValueError("三维查看器高度必须在 300 到 1600 像素之间。")
    for interaction in interactions:
        if not _finite_triplet(interaction.protein_xyz) or not _finite_triplet(
            interaction.ligand_xyz
        ):
            raise ValueError("相互作用坐标必须是有限三维坐标。")

    current_error: object = None
    if key is not None:
        try:
            current_state = st.session_state.get(key)
        except Exception:
            current_state = None
        if isinstance(current_state, Mapping):
            current_error = current_state.get("error")
        elif current_state is not None:
            current_error = getattr(current_state, "error", None)

    result = renderer(
        key=key,
        data={
            "structure_data": structure_data,
            "structure_format": normalized_format,
            "ligand_residue_name": ligand_residue_name.upper(),
            "ligand_chain": ligand_chain,
            "ligand_residue_number": ligand_residue_number,
            "pocket_distance_angstrom": pocket_distance_angstrom,
            "current_error": current_error,
            "interactions": [
                item.model_dump(mode="json") for item in interactions
            ],
            "score": {
                "metric": "Vina 预测评分",
                "value_kcal_mol": vina_score_kcal_mol,
                "mode": pose_mode,
                "seed": seed,
            },
        },
        default={"error": None},
        on_png_data_uri_change=on_png_data_uri_change or (lambda: None),
        on_selected_atom_change=on_selected_atom_change or (lambda: None),
        on_error_change=on_error_change or (lambda: None),
        height=height,
        width="stretch",
    )
    return StructureViewerResult(
        png_data_uri=getattr(result, "png_data_uri", None),
        selected_atom=getattr(result, "selected_atom", None),
        error=getattr(result, "error", None),
    )


def decode_viewer_png_data_uri(value: str) -> bytes:
    """Validate the browser callback before storing or downloading a PNG."""

    if not isinstance(value, str) or not value.startswith(_PNG_PREFIX):
        raise ValueError("三维查看器返回的不是 PNG data URI。")
    encoded = value[len(_PNG_PREFIX) :]
    if len(encoded) > (_MAX_PNG_BYTES * 4 // 3) + 16:
        raise ValueError("三维查看器 PNG 超过 25 MB。")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("三维查看器 PNG 的 base64 无效。") from exc
    if not payload.startswith(_PNG_MAGIC):
        raise ValueError("三维查看器返回的内容不是有效 PNG。")
    if len(payload) > _MAX_PNG_BYTES:
        raise ValueError("三维查看器 PNG 超过 25 MB。")
    try:
        with Image.open(io.BytesIO(payload)) as image:
            if image.format != "PNG":
                raise ValueError("三维查看器返回的内容不是 PNG。")
            width, height = image.size
            if width < 1 or height < 1 or width * height > _MAX_PNG_PIXELS:
                raise ValueError("三维查看器 PNG 的像素尺寸无效或过大。")
            image.verify()
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("三维查看器返回的 PNG 已损坏。") from exc
    return payload


__all__ = [
    "Local3DmolLibrary",
    "StructureViewerResult",
    "ViewerInteraction",
    "create_structure_viewer_component",
    "decode_viewer_png_data_uri",
    "load_default_3dmol_library",
    "load_local_3dmol_library",
    "mount_structure_viewer",
]
