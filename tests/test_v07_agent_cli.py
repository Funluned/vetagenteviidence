from __future__ import annotations

import builtins
import importlib
import json
import os
from pathlib import Path

import pytest

from scripts import run_v07_agent_evaluation as cli
import vetevidence.deepseek_contract as deepseek_contract
from vetevidence.v07_agent_comparison import V07AgentComparisonReport


def _forbid_api_key_read() -> bool:
    raise AssertionError("the API-key environment must not be read")


def test_deepseek_dry_run_is_json_only_and_has_no_key_or_provider_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)
    real_import = builtins.__import__

    def forbid_provider_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "vetevidence.deepseek_provider":
            raise AssertionError("dry-run must not import the real provider module")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", forbid_provider_import)

    result = cli.main(
        [
            "--provider",
            "deepseek",
            "--model",
            "deepseek-v4-flash",
            "--case-id",
            "DIR-01",
            "--case-id",
            "DIR-02",
            "--max-cost-cny",
            "3.50",
            "--timeout-seconds",
            "75",
            "--dry-run",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "case_count": 2,
        "typical_model_calls_per_case": 3,
        "typical_model_calls_total": 6,
        "hard_max_model_calls_per_case": 7,
        "hard_max_model_calls_total": 14,
        "max_cost_cny": "3.50",
        "timeout_seconds": 75.0,
        "timeout_seconds_allowed_range": {
            "minimum": cli.DEEPSEEK_MIN_TIMEOUT_SECONDS,
            "maximum": cli.DEEPSEEK_MAX_TIMEOUT_SECONDS,
        },
        "max_http_retries_per_model_call": 0,
        "synthetic_boundary": cli.SYNTHETIC_BOUNDARY,
        "will_read_api_key": False,
        "will_construct_provider": False,
        "will_use_network": False,
    }


def test_timeout_contract_reload_has_no_key_or_provider_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def forbid_sensitive_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "vetevidence.deepseek_provider":
            raise AssertionError("timeout contract imported the real provider")
        return real_import(name, globals, locals, fromlist, level)

    def forbid_environment_read(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("timeout contract read an environment value")

    monkeypatch.setattr(builtins, "__import__", forbid_sensitive_import)
    monkeypatch.setattr(os.environ, "get", forbid_environment_read)

    reloaded = importlib.reload(deepseek_contract)

    assert reloaded.DEEPSEEK_DEFAULT_TIMEOUT_SECONDS == 120.0


@pytest.mark.parametrize("raw", ["29.9", "300.1", "NaN", "not-a-number"])
def test_deepseek_timeout_rejects_out_of_range_or_invalid_values_before_key_access(
    raw: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)

    assert (
        cli.main(
            [
                "--provider",
                "deepseek",
                "--case-id",
                "DIR-01",
                "--timeout-seconds",
                raw,
                "--dry-run",
            ]
        )
        == 2
    )
    assert "--timeout-seconds" in capsys.readouterr().err


def test_fake_path_rejects_deepseek_timeout_without_key_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)

    assert cli.main(["--provider", "fake", "--timeout-seconds", "120"]) == 2
    assert "only available with --provider deepseek" in capsys.readouterr().err


def test_real_cli_passes_bounded_timeout_without_enabling_http_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vetevidence.deepseek_provider as deepseek_module

    captured: dict[str, object] = {}

    class ProviderConstructionObserved(RuntimeError):
        pass

    def capture_provider(**kwargs: object) -> None:
        captured.update(kwargs)
        raise ProviderConstructionObserved

    monkeypatch.setattr(cli, "_api_key_configured", lambda: True)
    monkeypatch.setattr(deepseek_module, "DeepSeekProvider", capture_provider)

    with pytest.raises(ProviderConstructionObserved):
        cli.main(
            [
                "--provider",
                "deepseek",
                "--case-id",
                "DIR-01",
                "--confirm-paid-run",
                "--max-cost-cny",
                "1",
                "--timeout-seconds",
                "95.5",
            ]
        )

    assert captured["timeout_seconds"] == 95.5
    assert captured["max_retries"] == 0
    assert captured["model_name"] == "deepseek-v4-pro"
    assert captured["budget"].limit_cny == 1  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            [
                "--provider",
                "deepseek",
                "--case-id",
                "DIR-01",
                "--max-cost-cny",
                "1",
            ],
            "--confirm-paid-run",
        ),
        (
            [
                "--provider",
                "deepseek",
                "--case-id",
                "DIR-01",
                "--confirm-paid-run",
            ],
            "--max-cost-cny",
        ),
        (
            [
                "--provider",
                "deepseek",
                "--confirm-paid-run",
                "--max-cost-cny",
                "1",
            ],
            "--case-id",
        ),
    ],
)
def test_deepseek_gates_fail_before_key_access(
    arguments: list[str],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)

    assert cli.main(arguments) == 2
    assert message in capsys.readouterr().err


def test_missing_key_fails_before_real_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert (
        cli.main(
            [
                "--provider",
                "deepseek",
                "--case-id",
                "DIR-01",
                "--confirm-paid-run",
                "--max-cost-cny",
                "1",
            ]
        )
        == 2
    )
    assert "DEEPSEEK_API_KEY" in capsys.readouterr().err


def test_fake_single_case_writes_zero_cost_offline_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "agent-fake-one.json"
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)

    assert (
        cli.main(
            [
                "--provider",
                "fake",
                "--case-id",
                "DIR-01",
                "--json-output",
                str(output),
            ]
        )
        == 0
    )

    summary = json.loads(capsys.readouterr().out)
    report = V07AgentComparisonReport.model_validate_json(
        output.read_text(encoding="utf-8")
    )
    assert summary["case_count"] == 1
    assert summary["real_model_calls"] == 0
    assert summary["network_calls"] == 0
    assert all(value == "0" for value in summary["costs_by_currency"].values())
    assert report.execution_mode == "fake"
    assert len(report.cases) == 1
    assert report.actual_spend.total_actual.real_model_calls == 0
    assert report.actual_spend.total_actual.actual_http_attempts == 0
    assert all(
        amount == 0
        for amount in report.actual_spend.total_actual.costs_by_currency.values()
    )


def test_fake_full_baseline_check_matches_hash_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "agent-fake-baseline.json"
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)

    assert cli.main(["--json-output", str(output)]) == 0
    first_summary = json.loads(capsys.readouterr().out)
    before = output.read_bytes()
    assert first_summary["case_count"] == 27
    assert first_summary["real_model_calls"] == 0
    assert first_summary["network_calls"] == 0

    assert (
        cli.main(
            [
                "--check-baseline",
                "--json-output",
                str(output),
            ]
        )
        == 0
    )
    check = json.loads(capsys.readouterr().out)
    assert check["status"] == "match"
    assert check["result_sha256"] == first_summary["result_sha256"]
    assert output.read_bytes() == before


def test_fake_full_custom_output_refuses_to_overwrite_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "existing.txt"
    output.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)

    assert cli.main(["--json-output", str(output)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "keep me"


def test_check_baseline_rejects_subset_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(cli, "_api_key_configured", _forbid_api_key_read)

    assert (
        cli.main(
            [
                "--provider",
                "fake",
                "--case-id",
                "DIR-01",
                "--check-baseline",
                "--json-output",
                str(output),
            ]
        )
        == 2
    )
    assert "complete fake case set" in capsys.readouterr().err
    assert not output.exists()
