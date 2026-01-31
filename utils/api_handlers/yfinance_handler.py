
# -*- coding: utf-8 -*-
import yfinance as yf
import asyncio
from typing import Optional, Dict, Any
from logger_config import logger

async def get_stock_info(ticker: str) -> Dict[str, Any]:
    """
    yfinance를 사용하여 주식/암호화폐 정보를 조회합니다.
    """
    try:
        # Run synchronous yfinance call in a thread to verify non-blocking behavior
        def _fetch():
            stock = yf.Ticker(ticker)
            # Try to get fast info first
            info = {}
            try:
                info = stock.info
            except:
                pass
                
            price = None
            currency = info.get('currency', 'USD')
            
            # Fetch Price
            try:
                price = stock.fast_info.last_price
            except:
                # Fallback to history
                hist = stock.history(period="1d")
                if not hist.empty:
                    price = hist['Close'].iloc[-1]
            
            # Calculate Change (approximate if fast_info)
            change_p = None
            try:
                prev_close = stock.fast_info.previous_close
                if price and prev_close:
                    change_p = ((price - prev_close) / prev_close) * 100
            except:
                pass

            return {
                "symbol": ticker,
                "name": info.get('shortName') or info.get('longName') or ticker,
                "price": price,
                "currency": currency,
                "change_percent": change_p,
                "market_cap": info.get('marketCap'),
                "industry": info.get('industry'),
                "summary": info.get('longBusinessSummary') or info.get('description'),
                "website": info.get('website')
            }

        data = await asyncio.to_thread(_fetch)
        
        if data['price'] is None:
            return {"error": f"'{ticker}'에 대한 시세 정보를 가져올 수 없습니다."}
            
        return data

    except Exception as e:
        logger.error(f"yfinance 조회 실패 ({ticker}): {e}")
        return {"error": "주식 정보를 가져오는 중 오류가 발생했습니다."}
