from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from vetevidence.structure_viewer import (
    Local3DmolLibrary,
    ViewerInteraction,
    create_structure_viewer_component,
    decode_viewer_png_data_uri,
    load_default_3dmol_library,
    load_local_3dmol_library,
    mount_structure_viewer,
)


LOCAL_LIBRARY = b"export function createViewer() { return {}; }"


def _valid_png() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (3, 2), color=(255, 255, 255, 0)).save(
        output,
        format="PNG",
    )
    return output.getvalue()


def _library() -> Local3DmolLibrary:
    return Local3DmolLibrary(
        version="2.5.5",
        source_bytes=LOCAL_LIBRARY,
        sha256=hashlib.sha256(LOCAL_LIBRARY).hexdigest(),
    )


def test_default_3dmol_es_module_is_vendored_and_pinned() -> None:
    library = load_default_3dmol_library()

    assert library.version == "2.5.5"
    assert (
        library.sha256
        == "95513f6494717cc82fb2ba4d264f29b7ef189a31d4ece36a38d1f9666bf6d427"
    )
    assert b"createViewer" in library.source_bytes
    assert b"export{" in library.source_bytes


def test_local_3dmol_asset_rejects_hash_substitution(tmp_path: Path) -> None:
    asset = tmp_path / "3Dmol.es6-min.js"
    asset.write_bytes(LOCAL_LIBRARY)
    digest = hashlib.sha256(LOCAL_LIBRARY).hexdigest()

    library = load_local_3dmol_library(asset, expected_sha256=digest)
    assert library.sha256 == digest

    with pytest.raises(ValueError, match="SHA-256"):
        load_local_3dmol_library(asset, expected_sha256="0" * 64)


def test_ccv2_registration_uses_named_es_export_and_exact_ligand_selector() -> None:
    captured: dict[str, object] = {}

    def factory(name: str, **kwargs: object):
        captured["name"] = name
        captured.update(kwargs)
        return lambda **mount_kwargs: SimpleNamespace(**mount_kwargs)

    create_structure_viewer_component(
        _library(),
        component_factory=factory,
    )

    assert str(captured["name"]).startswith(
        "vetevidence_structure_viewer_2_5_5_"
    )
    javascript = str(captured["js"])
    assert javascript.startswith(LOCAL_LIBRARY.decode())
    assert 'typeof createViewer === "function"' in javascript
    assert "$3Dmol" not in javascript
    assert "chain: data.ligand_chain" in javascript
    assert "resi: data.ligand_residue_number" in javascript
    assert "viewer.zoomTo(ligandSelection)" in javascript
    assert "viewer.pngURI()" in javascript
    assert 'setTriggerValue("png_data_uri"' in javascript
    assert 'setStateValue("error", null)' in javascript
    assert "return () =>" in javascript
    assert "canvas.replaceChildren()" in javascript
    assert captured["isolate_styles"] is True


def test_structure_text_and_exact_selection_are_passed_only_as_component_data() -> None:
    registration: dict[str, object] = {}
    mounts: list[dict[str, object]] = []

    def factory(name: str, **kwargs: object):
        registration["name"] = name
        registration.update(kwargs)

        def renderer(**mount_kwargs: object):
            mounts.append(dict(mount_kwargs))
            return SimpleNamespace(
                png_data_uri=None,
                selected_atom={"chain": "A", "residue_number": 1},
                error=None,
            )

        return renderer

    renderer = create_structure_viewer_component(
        _library(),
        component_factory=factory,
    )
    structure = (
        "ATOM      1  N   ALA A   1       1.000   2.000   3.000"
        "</script><script>unsafe()</script>"
    )
    result = mount_structure_viewer(
        renderer,
        structure_data=structure,
        structure_format="pdb",
        vina_score_kcal_mol=-8.1,
        pose_mode=1,
        seed=42,
        ligand_residue_name="LIG",
        ligand_chain="Z",
        ligand_residue_number=9999,
        interactions=[
            ViewerInteraction(
                interaction_type="hydrogen_bond",
                protein_xyz=(1.0, 2.0, 3.0),
                ligand_xyz=(2.0, 3.0, 4.0),
                color="#0000FF",
            )
        ],
    )

    assert structure not in str(registration["html"])
    assert structure not in str(registration["js"])
    assert mounts[0]["data"]["structure_data"] == structure
    assert mounts[0]["data"]["ligand_residue_name"] == "LIG"
    assert mounts[0]["data"]["ligand_chain"] == "Z"
    assert mounts[0]["data"]["ligand_residue_number"] == 9999
    assert mounts[0]["data"]["score"]["metric"] == "Vina 预测评分"
    assert mounts[0]["width"] == "stretch"
    assert result.selected_atom == {"chain": "A", "residue_number": 1}


@pytest.mark.parametrize(
    ("chain", "residue_number"),
    [("AB", 9999), ("!", 9999), ("Z", 0), ("Z", 10_000)],
)
def test_viewer_rejects_ambiguous_ligand_selection(
    chain: str,
    residue_number: int,
) -> None:
    def renderer(**kwargs: object):
        return SimpleNamespace(
            png_data_uri=None,
            selected_atom=None,
            error=None,
        )

    with pytest.raises(ValueError, match="配体"):
        mount_structure_viewer(
            renderer,
            structure_data="ATOM",
            structure_format="pdb",
            vina_score_kcal_mol=-8.1,
            pose_mode=1,
            seed=42,
            ligand_chain=chain,
            ligand_residue_number=residue_number,
        )


def test_png_callback_requires_a_fully_decodable_png() -> None:
    png = _valid_png()
    uri = "data:image/png;base64," + base64.b64encode(png).decode()
    assert decode_viewer_png_data_uri(uri) == png

    with pytest.raises(ValueError, match="PNG"):
        decode_viewer_png_data_uri(
            "data:image/png;base64,"
            + base64.b64encode(b"not-png").decode()
        )
    with pytest.raises(ValueError, match="PNG"):
        decode_viewer_png_data_uri(
            "data:image/png;base64,"
            + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
        )
    with pytest.raises(ValueError, match="base64"):
        decode_viewer_png_data_uri("data:image/png;base64,%%%")
