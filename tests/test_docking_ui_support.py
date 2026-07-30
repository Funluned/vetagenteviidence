from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from vetevidence.docking_ui_support import (
    build_ligand_batch_items,
    fetch_rcsb_pdb,
    ligand_metadata_template,
    parse_vina_seeds,
    validate_docking_workload,
)


LIGAND_PDBQT = b"""ROOT
HETATM    1  C1  LIG L   1       1.000   2.000   3.000  0.00  0.00    +0.000 C
ENDROOT
TORSDOF 0
"""


def _metadata_csv(filename: str = "quercetin.pdbqt") -> bytes:
    return (
        "filename,ligand_id,compound_name,namespace,pubchem_cid,inchikey,"
        "user_namespace,user_accession,source_url,source_revision\n"
        f"{filename},quercetin,quercetin,pubchem,5280343,"
        "REFJWTPEDVJJIY-UHFFFAOYSA-N,,,"
        "https://pubchem.ncbi.nlm.nih.gov/compound/5280343,"
        "record-modified:2025-01-01\n"
    ).encode()


def test_template_and_metadata_bind_exact_file_hash() -> None:
    assert b"pubchem_cid" in ligand_metadata_template()

    items = build_ligand_batch_items(
        {"quercetin.pdbqt": LIGAND_PDBQT},
        _metadata_csv(),
    )

    assert len(items) == 1
    assert items[0].identity.canonical_accession.startswith("PubChem:CID5280343")
    assert items[0].identity.structure_sha256 == (
        "1c43805f24dd3a9c6599b45959aae529d3aff247849504e632963d5004f9597f"
    )


def test_metadata_rejects_unmapped_and_unsafe_files() -> None:
    with pytest.raises(ValueError, match="没有对应身份行"):
        build_ligand_batch_items(
            {
                "quercetin.pdbqt": LIGAND_PDBQT,
                "extra.pdbqt": LIGAND_PDBQT,
            },
            _metadata_csv(),
        )
    with pytest.raises(ValueError, match="文件名不安全"):
        build_ligand_batch_items(
            {"../quercetin.pdbqt": LIGAND_PDBQT},
            _metadata_csv("../quercetin.pdbqt"),
        )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42", (42,)),
        ("42, 137; -5", (42, 137, -5)),
        ("1，2；3", (1, 2, 3)),
    ],
)
def test_seed_parser(text: str, expected: tuple[int, ...]) -> None:
    assert parse_vina_seeds(text) == expected


def test_seed_parser_rejects_duplicates_and_unbounded_batches() -> None:
    with pytest.raises(ValueError, match="不能重复"):
        parse_vina_seeds("42,42")
    with pytest.raises(ValueError, match="最多允许"):
        parse_vina_seeds(",".join(map(str, range(13))))


def test_synchronous_workload_is_bounded() -> None:
    ligands = build_ligand_batch_items(
        {"quercetin.pdbqt": LIGAND_PDBQT},
        _metadata_csv(),
    )

    estimate = validate_docking_workload(
        ligands,
        (42, 137),
        exhaustiveness=8,
    )

    assert estimate.attempt_count == 2
    assert estimate.work_units == 16
    with pytest.raises(ValueError, match="最多 1 次"):
        validate_docking_workload(
            ligands,
            (42, 137),
            exhaustiveness=8,
            maximum_attempts=1,
        )
    with pytest.raises(ValueError, match="尝试数 × exhaustiveness"):
        validate_docking_workload(
            ligands,
            (42,),
            exhaustiveness=8,
            maximum_work_units=7,
        )


class _FakeClient:
    def __init__(self, response: httpx.Response):
        self.response = response
        self.urls: list[str] = []

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        self.urls.append(url)
        return self.response


def test_rcsb_download_is_bounded_and_traceable() -> None:
    request = httpx.Request("GET", "https://files.rcsb.org/download/1IEP.pdb")
    client = _FakeClient(
        httpx.Response(
            200,
            request=request,
            headers={"Last-Modified": "Wed, 29 Jul 2026 00:00:00 GMT"},
            content=b"HEADER TEST\nATOM      1  N   ALA A   1\nEND\n",
        )
    )
    accessed_at = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)

    result = fetch_rcsb_pdb("1iep", client=client, accessed_at=accessed_at)

    assert result.pdb_id == "1IEP"
    assert result.source_url == client.urls[0]
    assert result.source_revision.startswith("Wed, 29 Jul")
    assert result.accessed_at == accessed_at
    assert len(result.sha256) == 64


def test_rcsb_download_rejects_invalid_id() -> None:
    with pytest.raises(ValueError, match="PDB ID"):
        fetch_rcsb_pdb("../x")
