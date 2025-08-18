import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

# utils.api_handlers.exim을 임포트하기 전에 config를 모킹해야 할 수 있음
# 이 테스트에서는 config를 직접 사용하지 않으므로 간단하게 진행
from utils.api_handlers import exim

# 비동기 함수를 테스트하기 위해 pytest.mark.asyncio 사용
@pytest.mark.asyncio
async def test_get_exchange_rate_success(mocker):
    """
    API가 성공적으로 USD 환율 정보를 반환하는 경우를 테스트합니다.
    """
    # 모의 API 응답 데이터
    mock_api_response = [
        {"result": 1, "cur_unit": "USD", "cur_nm": "미국 달러", "deal_bas_r": "1,350.50"},
        {"result": 1, "cur_unit": "JPY", "cur_nm": "일본 옌", "deal_bas_r": "9.05"},
    ]

    # _fetch_exim_data 함수를 모킹
    mocker.patch.object(exim, '_fetch_exim_data', new_callable=AsyncMock, return_value=mock_api_response)

    # 테스트할 함수 호출
    result = await exim.get_exchange_rate("USD")

    # 결과 검증
    assert isinstance(result, dict)
    assert result.get("currency_code") == "USD"
    assert result.get("currency_name") == "미국 달러"
    assert result.get("rate") == 1350.50

    # 모킹된 함수가 올바른 인자와 함께 호출되었는지 확인
    exim._fetch_exim_data.assert_called_once_with("AP01")


@pytest.mark.asyncio
async def test_get_exchange_rate_not_found(mocker):
    """
    API 응답에 요청한 통화가 없는 경우를 테스트합니다.
    """
    # 모의 API 응답 데이터 (EUR 없음)
    mock_api_response = [
        {"result": 1, "cur_unit": "USD", "cur_nm": "미국 달러", "deal_bas_r": "1,350.50"},
        {"result": 1, "cur_unit": "JPY", "cur_nm": "일본 옌", "deal_bas_r": "9.05"},
    ]

    mocker.patch.object(exim, '_fetch_exim_data', new_callable=AsyncMock, return_value=mock_api_response)

    # 테스트할 함수 호출 (EUR)
    result = await exim.get_exchange_rate("EUR")

    # 결과 검증
    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "'EUR' 통화를 찾을 수 없습니다."


@pytest.mark.asyncio
async def test_get_exchange_rate_api_error(mocker):
    """
    _fetch_exim_data 함수가 API 오류를 반환하는 경우를 테스트합니다.
    """
    # 모의 API 오류 응답
    mock_api_error = {"error": "API 요청 또는 데이터 처리 중 오류 발생"}

    mocker.patch.object(exim, '_fetch_exim_data', new_callable=AsyncMock, return_value=mock_api_error)

    # 테스트할 함수 호출
    result = await exim.get_exchange_rate("USD")

    # 결과 검증
    assert result == mock_api_error
