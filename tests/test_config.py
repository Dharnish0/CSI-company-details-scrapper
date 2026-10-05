from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from corpdisc.config import Settings, load_settings


def make(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def test_defaults_match_architecture():
    s = make()
    assert s.start_url == "https://www.csisoftware.com/"
    assert s.root_company == "Constellation Software Inc."
    assert s.root_code == "CSI"
    assert (s.max_depth, s.max_pages_per_domain, s.max_total_pages) == (8, 40, 2000)
    assert s.request_timeout == 20
    assert s.request_delay_seconds == 2.0
    assert s.max_retries == 2
    assert s.respect_robots is True
    assert s.allow_insecure_ssl is False
    assert s.use_playwright == "auto"
    assert (s.confirm_threshold, s.review_threshold) == (0.75, 0.40)
    assert (s.low_confidence_threshold, s.fuzzy_match_threshold) == (0.60, 0.92)
    assert s.recurse_into_review is False
    assert s.max_links_per_entity == 12
    assert s.llm_provider == "" and s.llm_model == ""
    assert s.llm_api_key is None and s.search_api_key is None
    assert s.db_path == Path("./data/csi_hierarchy.db")
    assert s.cache_dir == Path("./.cache")
    assert s.output_xlsx == Path("./CSI_Company_Hierarchy.xlsx")
    assert s.log_dir == Path("./logs")
    assert s.log_level == "INFO"


def test_env_overrides(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ROOT_CODE", "ACME_1")
    monkeypatch.setenv("MAX_DEPTH", "3")
    monkeypatch.setenv("RESPECT_ROBOTS", "false")
    monkeypatch.setenv("USE_PLAYWRIGHT", "FALSE")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("DB_PATH", "somewhere/x.db")
    s = make()
    assert s.root_code == "ACME_1"
    assert s.max_depth == 3
    assert s.respect_robots is False
    assert s.use_playwright == "false"
    assert s.log_level == "DEBUG"
    assert s.db_path == Path("somewhere/x.db")


def test_load_from_env_file(tmp_path: Path):
    env = tmp_path / "test.env"
    env.write_text("ROOT_COMPANY=Example Holdings\nROOT_CODE=EX\nMAX_TOTAL_PAGES=10\nUNKNOWN_KEY=1\n")
    s = load_settings(env)
    assert s.root_company == "Example Holdings"
    assert s.root_code == "EX"
    assert s.max_total_pages == 10


def test_env_var_beats_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    env = tmp_path / "test.env"
    env.write_text("ROOT_CODE=FROMFILE\n")
    monkeypatch.setenv("ROOT_CODE", "FROMENV")
    assert load_settings(env).root_code == "FROMENV"


def test_load_settings_none_ignores_dotenv(tmp_path: Path):
    (tmp_path / ".env").write_text("ROOT_CODE=SHOULDNOTLOAD\n")  # cwd is tmp_path
    assert load_settings(None).root_code == "CSI"
    assert load_settings().root_code == "SHOULDNOTLOAD"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"root_company": "   "},
        {"start_url": "ftp://example.com/"},
        {"start_url": "not a url"},
        {"start_url": "https://"},
        {"root_code": "bad code"},
        {"root_code": "lower"},
        {"root_code": ""},
        {"confirm_threshold": 1.5},
        {"review_threshold": -0.1},
        {"fuzzy_match_threshold": 2},
        {"review_threshold": 0.7, "low_confidence_threshold": 0.6},
        {"low_confidence_threshold": 0.8, "confirm_threshold": 0.75},
        {"max_depth": 0},
        {"max_pages_per_domain": 0},
        {"max_total_pages": -5},
        {"max_links_per_entity": 0},
        {"request_timeout": 0},
        {"request_delay_seconds": -1},
        {"max_retries": -1},
        {"log_level": "LOUD"},
        {"use_playwright": "sometimes"},
    ],
)
def test_invalid_values_rejected(kwargs):
    with pytest.raises(ValidationError):
        make(**kwargs)


def test_invalid_env_value_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ROOT_CODE", "bad code")
    with pytest.raises(ValidationError) as info:
        make()
    assert "root_code" in str(info.value)


def test_secrets_masked():
    s = make(llm_api_key="llm-secret-value-1", search_api_key="search-secret-value-2")
    for text in (repr(s), str(s), s.model_dump_json()):
        assert "llm-secret-value-1" not in text
        assert "search-secret-value-2" not in text
    assert s.llm_api_key is not None
    assert s.llm_api_key.get_secret_value() == "llm-secret-value-1"
    assert sorted(s.secret_values()) == ["llm-secret-value-1", "search-secret-value-2"]


def test_blank_secret_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("SEARCH_API_KEY", "   ")
    s = make()
    assert s.llm_api_key is None and s.search_api_key is None
    assert s.secret_values() == []
