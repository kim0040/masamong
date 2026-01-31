
import aiohttp
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from urllib.parse import quote_plus
import config
from logger_config import logger
import xml.etree.ElementTree as ET

async def get_krx_stock_info(stock_name: str) -> Dict[str, Any]:
    """
    공공데이터포털 KRX 주식시세 API (/getStockPriceInfo)를 호출합니다.
    - stock_name: 종목명 (예: "삼성전자")
    - 인증키: config.KRX_API_KEY
    """
    if not config.KRX_API_KEY:
        return {"error": "API Key not configured"}

    # 공공데이터포털은 일반적인 URL 인코딩보다, Decoding 된 키를 쿼리 파라미터로 직접 보내야 하는 경우가 많음.
    # 하지만 aiohttp params에 넣으면 자동 인코딩됨. 
    # serviceKey는 인코딩 된 키가 주어질 때가 많으므로 주의. 
    # 사용자가 준 키 "6c..."는 Decoding 된 키로 추정됨 (URL safe 문자가 아님). 
    # 만약 에러나면 인코딩 시도 필요.
    
    # 기준일자 계산 (최근 영업일 찾기 - 안전하게 오늘부터 5일 전까지 조회)
    # API가 비영업일(주말)엔 데이터가 없으므로 범위를 주는게 안전.
    end_dt = datetime.now().strftime("%Y%m%d")
    start_dt = (datetime.now() - timedelta(days=5)).strftime("%Y%m%d")
    
    params = {
        "serviceKey": config.KRX_API_KEY, # aiohttp가 인코딩 함 (디코딩된 키라면 맞음)
        "numOfRows": 10,
        "pageNo": 1,
        "resultType": "json", # JSON 요청
        "itmsNm": stock_name, # 종목명 검색
        "beginBasDt": start_dt,
        "endBasDt": end_dt
    }

    url = f"{config.KRX_BASE_URL}/getStockPriceInfo"
    
    # 공공데이터포털 Key는 'Decoding' 된 키를 받을 경우, requests/aiohttp params에 넣으면 자동 인코딩 되므로 정상 동작함.
    # 하지만 'Encoding' 된 키를 받으면 params에 넣으면 이중 인코딩 됨.
    # 사용자 제공 키가 "6c..."로 시작하므로 Decoding Key일 확률이 높으나, 404/SERVICE_KEY_IS_NOT_REGISTERED 에러가 나면
    # 인코딩 방식을 바꿔봐야 함.
    # 404 Error: API not found -> URL 경로 문제일 가능성이 큼. 
    # Base URL: http://apis.data.go.kr/1160100/service/GetStockSecuritiesInfoService
    # Endpoint: /getStockPriceInfo
    
    try:
        async with aiohttp.ClientSession() as session:
            # Case 1: Standard params (Safe for Decoding Key)
            # 만약 404가 계속 뜨면 URL 오타 확인 필요. 
            # (config.KRX_BASE_URL에 /service가 포함되어 있는지 확인)
            
            # 수동 구성 (가장 확실)
            encoded_key = quote_plus(config.KRX_API_KEY) # 직접 인코딩
            # 주의: 사용자가 이미 인코딩된 키를 줬을 수도 있음. 
            # 일단 'Decoding' 키라고 가정하고 quote_plus.
            
            # 쿼리 스트링 수동 조립
            qs = f"?serviceKey={config.KRX_API_KEY}&resultType=json&itmsNm={quote_plus(stock_name)}&numOfRows=1&pageNo=1&beginBasDt={start_dt}&endBasDt={end_dt}"
            # config.KRX_API_KEY를 그대로 넣음 (이미 인코딩된 키일 경우 대비 or requests가 처리 안하도록)
            
            full_url = url + qs
            # logger.info(f"KRX Request URL: {full_url}") # 디버깅용 (키 노출 주의)
            
            async with session.get(full_url, timeout=10) as response:
                if response.status != 200:
                    text = await response.text()
                    logger.error(f"KRX API Error {response.status}: {text}")
                    return {"error": f"HTTP {response.status}"}
                
                try:
                    data = await response.json()
                except:
                    # JSON 실패 시 XML일 수 있음 (ServiceKey 에러 시 XML 리턴됨)
                    text = await response.text()
                    logger.error(f"KRX API JSON Parsing Error. Response: {text[:200]}")
                    return {"error": "Invalid Response Format"}

                items = data.get("response", {}).get("body", {}).get("items", {}).get("item", [])
                
                if not items:
                    return {"error": "No Data Found"}
                
                # 리스트가 아닐 수도 있음 (항목 1개면 dict)
                if isinstance(items, dict):
                    items = [items]
                    
                # basDt 기준 내림차순 정렬 (최신순)
                items.sort(key=lambda x: x.get("basDt", ""), reverse=True)
                latest = items[0]
                
                return {
                    "symbol": latest.get("srtnCd"), # 단축코드
                    "name": latest.get("itmsNm"),
                    "price": float(latest.get("clpr", 0)), # 종가
                    "change": float(latest.get("vs", 0)), # 대비
                    "change_percent": float(latest.get("fltRt", 0)), # 등락률
                    "date": latest.get("basDt"),
                    "market_cap": int(latest.get("mrktTotAmt", 0))
                }

    except Exception as e:
        logger.error(f"KRX Handler Error: {e}")
        return {"error": str(e)}
