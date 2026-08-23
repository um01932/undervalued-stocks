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
    _BET_TICKERS,
    _EUROSTOXX50_FALLBACK,
    _RUSSELL2000_FALLBACK,
    _normalise,
    get_bet_tickers,
    get_eurostoxx50_tickers,
    get_multi_universe,
    get_nasdaq100_tickers,
    get_russell2000_tickers,
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


# ── get_russell2000_tickers ───────────────────────────────────────────────────

class TestGetRussell2000Tickers:
    """Tests for get_russell2000_tickers — always use fallback path (no HTTP)."""

    @patch("src.universe._fetch_html", side_effect=Exception("network error"))
    def test_falls_back_to_hardcoded_list(self, mock_fetch):
        """When Wikipedia scrape fails, fallback list is returned."""
        result = get_russell2000_tickers()
        assert len(result) > 0
        assert result == sorted(result)

    @patch("src.universe._fetch_html", side_effect=Exception("network error"))
    def test_fallback_list_is_normalised(self, mock_fetch):
        """Fallback tickers must be normalised (no dots, no whitespace)."""
        result = get_russell2000_tickers()
        for ticker in result:
            assert "." not in ticker
            assert ticker == ticker.strip()

    @patch("src.universe._fetch_html", side_effect=Exception("timeout"))
    def test_fallback_matches_constant(self, mock_fetch):
        """Fallback result should equal _RUSSELL2000_FALLBACK after normalisation."""
        result = get_russell2000_tickers()
        expected = sorted({t.strip().replace(".", "-") for t in _RUSSELL2000_FALLBACK if t.strip()})
        assert result == expected

    def test_wikipedia_parse_success(self):
        """When Wikipedia returns a big table with 'Ticker' column, it is used."""
        rows = "".join(f"<tr><td>T{i:04d}</td><td>Company {i}</td></tr>" for i in range(110))
        html = f'<table><tr><th>Ticker</th><th>Company</th></tr>{rows}</table>'
        with patch("src.universe._fetch_html", return_value=html):
            result = get_russell2000_tickers()
        assert len(result) >= 100

    @patch("src.universe.get_russell2000_tickers", return_value=["BOOT", "CALM", "FIVE"])
    def test_get_universe_russell2000(self, mock_r2k):
        result = get_universe(UniverseSource.RUSSELL2000)
        mock_r2k.assert_called_once()
        assert "BOOT" in result


# ── get_eurostoxx50_tickers ───────────────────────────────────────────────────

class TestGetEuroStoxx50Tickers:
    """Tests for get_eurostoxx50_tickers — always use fallback path (no HTTP)."""

    @patch("src.universe._fetch_html", side_effect=Exception("network error"))
    def test_falls_back_to_hardcoded_list(self, mock_fetch):
        result = get_eurostoxx50_tickers()
        assert len(result) > 0
        assert result == sorted(result)

    @patch("src.universe._fetch_html", side_effect=Exception("timeout"))
    def test_fallback_list_is_normalised(self, mock_fetch):
        result = get_eurostoxx50_tickers()
        for ticker in result:
            assert ticker == ticker.strip()

    @patch("src.universe._fetch_html", side_effect=Exception("timeout"))
    def test_fallback_matches_constant(self, mock_fetch):
        result = get_eurostoxx50_tickers()
        # EssilorLuxottica.PA dot → hyphen normalisation
        assert "EssilorLuxottica-PA" in result or any("EssilorLuxottica" in t for t in result)

    def test_wikipedia_parse_success(self):
        """When Wikipedia returns a ~50-row Ticker table, it is used."""
        rows = "".join(f"<tr><td>T{i:02d}.PA</td><td>Co {i}</td></tr>" for i in range(50))
        html = f'<table><tr><th>Ticker</th><th>Company</th></tr>{rows}</table>'
        with patch("src.universe._fetch_html", return_value=html):
            result = get_eurostoxx50_tickers()
        assert len(result) == 50

    @patch("src.universe.get_eurostoxx50_tickers", return_value=["ASML-AS", "TTE-PA"])
    def test_get_universe_eurostoxx50(self, mock_es50):
        result = get_universe(UniverseSource.EUROSTOXX50)
        mock_es50.assert_called_once()
        assert "ASML-AS" in result


# ── get_bet_tickers ───────────────────────────────────────────────────────────

class TestGetBetTickers:
    """Tests for get_bet_tickers — pure static list, no network."""

    def test_returns_list(self):
        result = get_bet_tickers()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_result_is_sorted(self):
        result = get_bet_tickers()
        assert result == sorted(result)

    def test_tickers_have_ro_suffix(self):
        result = get_bet_tickers()
        # After normalisation dots become hyphens: BRD.RO → BRD-RO
        for ticker in result:
            assert ticker.endswith("-RO"), f"Expected -RO suffix: {ticker}"

    def test_no_dots_after_normalisation(self):
        result = get_bet_tickers()
        for ticker in result:
            assert "." not in ticker

    def test_matches_static_constant(self):
        result = get_bet_tickers()
        expected = sorted({t.strip().replace(".", "-") for t in _BET_TICKERS if t.strip()})
        assert result == expected

    @patch("src.universe.get_bet_tickers", return_value=["BRD-RO", "TLV-RO"])
    def test_get_universe_bet(self, mock_bet):
        result = get_universe(UniverseSource.BET)
        mock_bet.assert_called_once()
        assert "BRD-RO" in result


# ── get_multi_universe ────────────────────────────────────────────────────────

class TestGetMultiUniverse:
    """Tests for get_multi_universe — combines all major universes."""

    @patch("src.universe.get_sp500_tickers", return_value=["AAPL", "MSFT"])
    @patch("src.universe.get_nasdaq100_tickers", return_value=["NVDA", "MSFT"])
    @patch("src.universe.get_russell2000_tickers", return_value=["BOOT", "CALM"])
    @patch("src.universe.get_eurostoxx50_tickers", return_value=["ASML-AS"])
    @patch("src.universe.get_bet_tickers", return_value=["BRD-RO"])
    def test_combines_all_sources(self, mock_bet, mock_es50, mock_r2k, mock_nq, mock_sp):
        result = get_multi_universe()
        # All unique tickers from all sources should be present
        assert "AAPL" in result
        assert "NVDA" in result
        assert "BOOT" in result
        assert "ASML-AS" in result
        assert "BRD-RO" in result

    @patch("src.universe.get_sp500_tickers", return_value=["AAPL", "MSFT"])
    @patch("src.universe.get_nasdaq100_tickers", return_value=["NVDA", "MSFT"])
    @patch("src.universe.get_russell2000_tickers", return_value=["BOOT"])
    @patch("src.universe.get_eurostoxx50_tickers", return_value=["TTE-PA"])
    @patch("src.universe.get_bet_tickers", return_value=["TLV-RO"])
    def test_deduplicates(self, mock_bet, mock_es50, mock_r2k, mock_nq, mock_sp):
        result = get_multi_universe()
        # MSFT appears in both sp500 and nasdaq100 — should appear only once
        assert result.count("MSFT") == 1

    @patch("src.universe.get_sp500_tickers", return_value=["AAPL"])
    @patch("src.universe.get_nasdaq100_tickers", return_value=["NVDA"])
    @patch("src.universe.get_russell2000_tickers", return_value=["BOOT"])
    @patch("src.universe.get_eurostoxx50_tickers", return_value=["TTE-PA"])
    @patch("src.universe.get_bet_tickers", return_value=["TLV-RO"])
    def test_result_is_sorted(self, mock_bet, mock_es50, mock_r2k, mock_nq, mock_sp):
        result = get_multi_universe()
        assert result == sorted(result)

    @patch("src.universe.get_sp500_tickers", side_effect=Exception("network"))
    @patch("src.universe.get_nasdaq100_tickers", return_value=["NVDA"])
    @patch("src.universe.get_russell2000_tickers", return_value=["BOOT"])
    @patch("src.universe.get_eurostoxx50_tickers", return_value=["TTE-PA"])
    @patch("src.universe.get_bet_tickers", return_value=["TLV-RO"])
    def test_tolerates_partial_failure(self, mock_bet, mock_es50, mock_r2k, mock_nq, mock_sp):
        """If one source fails, the rest still contribute."""
        result = get_multi_universe()
        assert "NVDA" in result
        assert "BOOT" in result
        # sp500 failed, AAPL not present
        assert "AAPL" not in result

    @patch("src.universe.get_multi_universe", return_value=["AAPL", "ASML-AS", "BRD-RO"])
    def test_get_universe_multi(self, mock_multi):
        result = get_universe(UniverseSource.MULTI)
        mock_multi.assert_called_once()
        assert "AAPL" in result
        assert "ASML-AS" in result
