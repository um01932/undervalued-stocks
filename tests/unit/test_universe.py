"""
tests/unit/test_universe.py — unit tests for src/universe.py.

All tests use mocks; no real HTTP calls or filesystem I/O outside of tmp_path.
"""

from __future__ import annotations

import csv
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from src.universe import (
    UniverseSource,
    _normalise,
    get_nasdaq100_tickers,
    get_sp500_tickers,
    get_tickers_from_csv,
    get_universe,
    get_world_tickers,
)


# ── _normalise ────────────────────────────────────────────────────────────────

class TestNormalise:
    def test_strips_whitespace(self):
        assert _normalise(["  AAPL  ", "MSFT"]) == ["AAPL", "MSFT"]

    def test_replaces_dot_with_hyphen(self):
        assert _normalise(["BRK.B"]) == ["BRK-B"]

    def test_deduplicates(self):
        result = _normalise(["AAPL", "AAPL", "MSFT"])
        assert result.count("AAPL") == 1

    def test_sorts(self):
        result = _normalise(["MSFT", "AAPL", "GOOG"])
        assert result == sorted(result)

    def test_filters_empty_strings(self):
        result = _normalise(["AAPL", "", "  "])
        assert "" not in result
        assert "  " not in result

    def test_empty_input(self):
        assert _normalise([]) == []


# ── get_sp500_tickers ─────────────────────────────────────────────────────────

class TestGetSp500Tickers:
    def _make_sp500_html(self) -> str:
        """Minimal HTML with the S&P 500 constituents table."""
        return (
            '<table id="constituents"><tr><th>Symbol</th><th>Security</th></tr>'
            '<tr><td>AAPL</td><td>Apple</td></tr>'
            '<tr><td>MSFT</td><td>Microsoft</td></tr>'
            '<tr><td>BRK.B</td><td>Berkshire</td></tr>'
            '<tr><td>GOOGL</td><td>Alphabet</td></tr>'
            '</table>'
        )

    @patch("src.universe._fetch_html")
    def test_returns_normalised_tickers(self, mock_fetch):
        mock_fetch.return_value = self._make_sp500_html()
        result = get_sp500_tickers()
        assert "AAPL" in result
        assert "BRK-B" in result   # dot normalised
        assert "BRK.B" not in result

    @patch("src.universe._fetch_html")
    def test_result_is_sorted(self, mock_fetch):
        mock_fetch.return_value = self._make_sp500_html()
        result = get_sp500_tickers()
        assert result == sorted(result)

    @patch("src.universe._fetch_html")
    def test_raises_on_parse_failure(self, mock_fetch):
        mock_fetch.side_effect = Exception("403 Forbidden")
        with pytest.raises(RuntimeError, match="Failed to fetch S&P 500"):
            get_sp500_tickers()

    @patch("src.universe._fetch_html")
    def test_drops_nan_symbols(self, mock_fetch):
        html = (
            '<table id="constituents"><tr><th>Symbol</th></tr>'
            '<tr><td>AAPL</td></tr><tr><td></td></tr><tr><td>MSFT</td></tr>'
            '</table>'
        )
        mock_fetch.return_value = html
        result = get_sp500_tickers()
        assert all(isinstance(t, str) and t for t in result)


# ── get_nasdaq100_tickers ─────────────────────────────────────────────────────

class TestGetNasdaq100Tickers:
    def _make_nasdaq_html(self) -> str:
        return (
            '<table><tr><th>Name</th></tr><tr><td>Noise</td></tr></table>'
            '<table><tr><th>Ticker</th><th>Company</th></tr>'
            '<tr><td>AAPL</td><td>Apple</td></tr>'
            '<tr><td>MSFT</td><td>Microsoft</td></tr>'
            '<tr><td>NVDA</td><td>NVIDIA</td></tr>'
            '</table>'
        )

    @patch("src.universe._fetch_html")
    def test_finds_ticker_column(self, mock_fetch):
        mock_fetch.return_value = self._make_nasdaq_html()
        result = get_nasdaq100_tickers()
        assert "AAPL" in result
        assert "NVDA" in result

    @patch("src.universe._fetch_html")
    def test_raises_when_no_ticker_column(self, mock_fetch):
        mock_fetch.return_value = '<table><tr><th>Name</th></tr><tr><td>foo</td></tr></table>'
        with pytest.raises(RuntimeError, match="Failed to fetch NASDAQ-100"):
            get_nasdaq100_tickers()

    @patch("src.universe._fetch_html")
    def test_raises_on_network_error(self, mock_fetch):
        mock_fetch.side_effect = Exception("timeout")
        with pytest.raises(RuntimeError, match="Failed to fetch NASDAQ-100"):
            get_nasdaq100_tickers()


# ── get_world_tickers ─────────────────────────────────────────────────────────

class TestGetWorldTickers:
    def test_loads_from_csv(self, tmp_path, monkeypatch):
        csv_file = tmp_path / "global_tickers.csv"
        csv_file.write_text("ticker,name,exchange,country\nAAPL,Apple,NASDAQ,US\nMSFT,Microsoft,NASDAQ,US\n")
        monkeypatch.setattr("src.universe._GLOBAL_CSV", csv_file)
        result = get_world_tickers()
        assert "AAPL" in result
        assert "MSFT" in result

    def test_raises_if_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.universe._GLOBAL_CSV", tmp_path / "missing.csv")
        with pytest.raises(FileNotFoundError):
            get_world_tickers()

    def test_raises_if_no_ticker_column(self, tmp_path, monkeypatch):
        csv_file = tmp_path / "bad.csv"
        csv_file.write_text("symbol,name\nAAPL,Apple\n")
        monkeypatch.setattr("src.universe._GLOBAL_CSV", csv_file)
        with pytest.raises(ValueError, match="'ticker' column not found"):
            get_world_tickers()

    def test_case_insensitive_column_name(self, tmp_path, monkeypatch):
        csv_file = tmp_path / "global_tickers.csv"
        csv_file.write_text("TICKER,name\nAAPL,Apple\n")
        monkeypatch.setattr("src.universe._GLOBAL_CSV", csv_file)
        result = get_world_tickers()
        assert "AAPL" in result

    def test_normalises_dots(self, tmp_path, monkeypatch):
        csv_file = tmp_path / "global_tickers.csv"
        csv_file.write_text("ticker,name\nBRK.B,Berkshire\n")
        monkeypatch.setattr("src.universe._GLOBAL_CSV", csv_file)
        result = get_world_tickers()
        assert "BRK-B" in result
        assert "BRK.B" not in result


# ── get_tickers_from_csv ──────────────────────────────────────────────────────

class TestGetTickersFromCsv:
    def test_loads_correctly(self, tmp_path):
        f = tmp_path / "custom.csv"
        f.write_text("ticker,sector\nAAPL,Tech\nMSFT,Tech\n")
        result = get_tickers_from_csv(str(f))
        assert "AAPL" in result
        assert "MSFT" in result

    def test_raises_if_file_missing(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            get_tickers_from_csv(str(tmp_path / "nope.csv"))

    def test_raises_if_no_ticker_column(self, tmp_path):
        f = tmp_path / "bad.csv"
        f.write_text("symbol,name\nAAPL,Apple\n")
        with pytest.raises(ValueError, match="'ticker' column not found"):
            get_tickers_from_csv(str(f))

    def test_case_insensitive_column(self, tmp_path):
        f = tmp_path / "custom.csv"
        f.write_text("Ticker,sector\nGOOG,Tech\n")
        result = get_tickers_from_csv(str(f))
        assert "GOOG" in result


# ── get_universe ──────────────────────────────────────────────────────────────

class TestGetUniverse:
    @patch("src.universe.get_sp500_tickers", return_value=["AAPL", "MSFT"])
    def test_sp500(self, mock_sp500):
        result = get_universe(UniverseSource.SP500)
        mock_sp500.assert_called_once()
        assert result == ["AAPL", "MSFT"]

    @patch("src.universe.get_nasdaq100_tickers", return_value=["NVDA", "QCOM"])
    def test_nasdaq100(self, mock_nq):
        result = get_universe(UniverseSource.NASDAQ100)
        mock_nq.assert_called_once()
        assert result == ["NVDA", "QCOM"]

    @patch("src.universe.get_world_tickers", return_value=["ASML-AS", "ROG-SW"])
    def test_world_default(self, mock_world):
        result = get_universe()  # default = WORLD
        mock_world.assert_called_once()
        assert result == ["ASML-AS", "ROG-SW"]

    def test_custom_requires_csv_path(self):
        with pytest.raises(ValueError, match="csv_path must be provided"):
            get_universe(UniverseSource.CUSTOM)

    def test_custom_with_path(self, tmp_path):
        f = tmp_path / "tickers.csv"
        f.write_text("ticker\nTSLA\n")
        result = get_universe(UniverseSource.CUSTOM, csv_path=str(f))
        assert "TSLA" in result

    @patch("src.universe.get_world_tickers", return_value=["AAPL"])
    def test_world_is_default_source(self, mock_world):
        get_universe()
        mock_world.assert_called_once()
